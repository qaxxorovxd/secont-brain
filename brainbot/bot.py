"""Telegram front-end."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time as dtime, timedelta
from functools import wraps

from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .ai import Assistant, Reply
from .config import Config
from .export import write_file
from .gitsync import GitSync
from .storage import Brain, human_date as fmt_date
from .tgformat import md_to_html

log = logging.getLogger(__name__)

HELP = """🧠 **Brain** — ikkinchi xotirang

Menga **matn** yoki **ovozli xabar** yubor — hammasini vaqt tartibida saqlayman, keyin so'rasang topib beraman.

**Misollar**
- _Iroda Karimovaga juma kunigacha referat qilishim kerak_
- _Irodaga qildim_
- _Kimlarga referat qilishim kerak?_
- _Ertaga 15:00 da eslat: dorixona_
- _Alining tug'ilgan kuni 5-mart_

**Buyruqlar**
/tasks — ochiq vazifalar
/today — bugun va muddati o'tganlar
/export — hamma vazifalar fayl qilib
/sync — git sync (brain repo)
/new — suhbat kontekstini tozalash"""


def render_tasks(tasks: list[dict], today: str, title: str) -> str:
    if not tasks:
        return f"**{title}**\n\n✨ Hech narsa yo'q."
    groups: dict[str, list[dict]] = {}
    for t in tasks:
        groups.setdefault(t.get("group") or "", []).append(t)
    lines = [f"**{title}**"]
    for g, items in sorted(groups.items(), key=lambda kv: (kv[0] == "", kv[0].lower())):
        items.sort(key=lambda t: (t.get("due") or "9999", t["id"]))
        lines.append("")
        if g:
            lines.append(f"📂 **{g.capitalize()}**")
        for t in items:
            mark = "✅" if t["status"] == "done" else "⬜"
            name = f"{t['person']} — {t['title']}" if t.get("person") else t["title"]
            extra = ""
            if t["status"] == "done":
                extra = f" · _{fmt_date(t['done_at'][:10])}_"
            elif t.get("due"):
                warn = "⚠️ " if t["due"] < today else ""
                extra = f" · {warn}⏰ {fmt_date(t['due'])}" + (f" {t['time']}" if t.get("time") else "")
            lines.append(f"{mark} {name}{extra} `#{t['id']}`")
    return "\n".join(lines)


class BrainBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.brain = Brain(cfg.brain_dir, cfg.tz)
        self.git = GitSync(cfg.brain_dir, cfg.git_sync, self.brain.lock)
        self.brain.on_change = self.git.mark_dirty
        self.ai = Assistant(self.brain, cfg.gemini_key, cfg.models)
        self.owner_file = cfg.state_dir / "owner.json"
        self.owner_chat: int | None = None
        if self.owner_file.exists():
            self.owner_chat = json.loads(self.owner_file.read_text())["chat_id"]
        elif len(cfg.allowed_ids) == 1:  # private chat id == user id, so reminders work before first message
            self.owner_chat = next(iter(cfg.allowed_ids))

    # ---------- access ----------
    def _allowed(self, update: Update) -> bool:
        uid = update.effective_user.id if update.effective_user else None
        if uid is None:
            return False
        if self.cfg.allowed_ids:
            ok = uid in self.cfg.allowed_ids
        else:  # no allow-list: the first person to talk to the bot becomes the owner
            if self.owner_chat is None:
                self._set_owner(update.effective_chat.id)
            ok = uid == self.owner_chat
        if ok and self.owner_chat != update.effective_chat.id and update.effective_chat.type == "private":
            self._set_owner(update.effective_chat.id)
        return ok

    def _set_owner(self, chat_id: int) -> None:
        self.owner_chat = chat_id
        self.owner_file.write_text(json.dumps({"chat_id": chat_id}))

    @staticmethod
    def guarded(fn):
        @wraps(fn)
        async def wrapper(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not self._allowed(update):
                if update.effective_message:
                    await update.effective_message.reply_text("⛔ Bu shaxsiy bot.")
                return
            try:
                await fn(self, update, ctx)
            except Exception as e:
                log.exception("handler error")
                await update.effective_message.reply_text(f"⚠️ Xatolik: {str(e)[:300]}")
        return wrapper

    # ---------- sending ----------
    async def send_md(self, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, md: str, reply_to=None) -> None:
        html = md_to_html(md)
        if len(html) > 4000:  # too long for one message → file
            p = write_file(self.cfg.state_dir / "out", "javob", md, "md")
            await ctx.bot.send_document(chat_id, p.open("rb"), filename=p.name, caption="📎 To'liq javob")
            return
        try:
            await ctx.bot.send_message(chat_id, html, parse_mode=ParseMode.HTML,
                                       reply_to_message_id=reply_to, disable_web_page_preview=True)
        except BadRequest:
            await ctx.bot.send_message(chat_id, md, reply_to_message_id=reply_to)

    async def deliver(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, reply: Reply, prefix: str = "") -> None:
        chat = update.effective_chat.id
        await self.send_md(chat, ctx, prefix + reply.text, update.effective_message.message_id)
        for f in reply.files:
            p = write_file(self.cfg.state_dir / "out", f["title"], f["content"], f["format"])
            with p.open("rb") as fh:
                await ctx.bot.send_document(chat, fh, filename=p.name, caption=f"📎 {f['title']}")
        if self.git.dirty:
            asyncio.create_task(asyncio.to_thread(self.git.push))

    async def think(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str, kind: str, prefix: str = ""):
        await asyncio.to_thread(self.brain.log, text, kind)
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        reply = await asyncio.to_thread(self.ai.handle, text)
        await self.deliver(update, ctx, reply, prefix)

    # ---------- handlers ----------
    @guarded
    async def on_start(self, update: Update, ctx):
        await self.send_md(update.effective_chat.id, ctx, HELP)

    @guarded
    async def on_text(self, update: Update, ctx):
        await self.think(update, ctx, update.effective_message.text, "text")

    @guarded
    async def on_voice(self, update: Update, ctx):
        msg = update.effective_message
        media = msg.voice or msg.audio or msg.video_note
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        data = bytes(await (await media.get_file()).download_as_bytearray())
        mime = getattr(media, "mime_type", None) or ("video/mp4" if msg.video_note else "audio/ogg")
        text = await asyncio.to_thread(self.ai.transcribe, data, mime)
        if not text:
            await msg.reply_text("🤷 Ovozni tushunolmadim, qayta yubor.")
            return
        await self.think(update, ctx, text, "voice", prefix=f"🎙 _{text}_\n\n")

    @guarded
    async def on_photo(self, update: Update, ctx):
        msg = update.effective_message
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        data = bytes(await (await msg.photo[-1].get_file()).download_as_bytearray())
        rel = await asyncio.to_thread(self.brain.save_file, data, "photo.jpg")
        desc = await asyncio.to_thread(self.ai.describe, data, "image/jpeg", msg.caption or "")
        text = f"[photo: {rel}] {msg.caption or ''}\n{desc}".strip()
        await self.think(update, ctx, text, "photo")

    @guarded
    async def on_document(self, update: Update, ctx):
        msg = update.effective_message
        doc = msg.document
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await msg.reply_text("⚠️ Fayl 20 MB dan katta.")
            return
        data = bytes(await (await doc.get_file()).download_as_bytearray())
        rel = await asyncio.to_thread(self.brain.save_file, data, doc.file_name or "file")
        mime = doc.mime_type or ""
        if mime.startswith("text/") or (doc.file_name or "").endswith((".md", ".txt", ".csv", ".json")):
            body = data.decode("utf-8", "replace")[:20000]
            text = f"[file: {rel}] {msg.caption or ''}\n\n{body}"
        elif mime.startswith(("image/", "audio/")) or mime == "application/pdf":
            desc = await asyncio.to_thread(self.ai.describe, data, mime, msg.caption or "")
            text = f"[file: {rel}] {msg.caption or ''}\n{desc}"
        else:
            text = f"[file: {rel}] {msg.caption or ''}"
        await self.think(update, ctx, text.strip(), "file")

    @guarded
    async def on_tasks(self, update: Update, ctx):
        tasks = self.brain.context_tasks(done_days=3)
        today = self.brain.now().strftime("%Y-%m-%d")
        await self.send_md(update.effective_chat.id, ctx, render_tasks(tasks, today, "📋 Vazifalar"))

    @guarded
    async def on_today(self, update: Update, ctx):
        await self.send_md(update.effective_chat.id, ctx, self.digest_text() or "✨ Bugun uchun hech narsa yo'q.")

    @guarded
    async def on_export(self, update: Update, ctx):
        today = self.brain.now().strftime("%Y-%m-%d")
        tasks = [t for t in self.brain.tasks() if t["status"] != "cancelled"]
        md = render_tasks(tasks, today, "Barcha vazifalar").replace("`", "")
        facts = self.brain.facts()
        if facts:
            md += "\n\n" + facts.replace("# Facts", "## Faktlar")
        p = write_file(self.cfg.state_dir / "out", f"brain-export-{today}", md, "md")
        with p.open("rb") as fh:
            await ctx.bot.send_document(update.effective_chat.id, fh, filename=p.name, caption="📦 Eksport")

    @guarded
    async def on_sync(self, update: Update, ctx):
        if not self.git.enabled:
            await update.effective_message.reply_text("ℹ️ brain/ git repo emas. README → \"Sync\" bo'limi.")
            return
        pulled = await asyncio.to_thread(self.git.pull)
        pushed = await asyncio.to_thread(self.git.push, True)
        await self.send_md(update.effective_chat.id, ctx, f"🔄 **Sync**\n- pull: `{pulled[:200]}`\n- push: `{pushed}`")

    @guarded
    async def on_new(self, update: Update, ctx):
        self.ai.history.clear()
        await update.effective_message.reply_text("🧹 Kontekst tozalandi (xotira joyida).")

    # ---------- jobs ----------
    def digest_text(self) -> str:
        now = self.brain.now()
        today = now.strftime("%Y-%m-%d")
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        open_ = [t for t in self.brain.tasks() if t["status"] == "open" and t.get("due")]
        overdue = [t for t in open_ if t["due"] < today]
        due_today = [t for t in open_ if t["due"] == today]
        due_tom = [t for t in open_ if t["due"] == tomorrow]
        if not (overdue or due_today or due_tom):
            return ""
        parts = [f"☀️ **{fmt_date(today)}**"]
        for label, items in (("⚠️ Muddati o'tgan", overdue), ("📌 Bugun", due_today), ("🗓 Ertaga", due_tom)):
            if items:
                parts.append(f"\n**{label}**")
                for t in items:
                    name = f"{t['person']} — {t['title']}" if t.get("person") else t["title"]
                    when = fmt_date(t["due"]) if label.startswith("⚠️") else (t.get("time") or "")
                    parts.append(f"⬜ {name}" + (f" · {when}" if when else ""))
        return "\n".join(parts)

    async def job_digest(self, ctx: ContextTypes.DEFAULT_TYPE):
        if self.owner_chat and (text := self.digest_text()):
            await self.send_md(self.owner_chat, ctx, text)

    async def job_reminders(self, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.owner_chat:
            return
        for t in self.brain.due_reminders():
            name = f"{t['person']} — {t['title']}" if t.get("person") else t["title"]
            when = f"\n⏰ {fmt_date(t['due'])}" + (f" {t['time']}" if t.get("time") else "") if t.get("due") else ""
            details = f"\n_{t['details']}_" if t.get("details") else ""
            await self.send_md(self.owner_chat, ctx, f"🔔 **Eslatma**\n{name}{when}{details}")
            self.brain.mark_reminded(t["id"])

    async def job_sync(self, ctx: ContextTypes.DEFAULT_TYPE):
        await asyncio.to_thread(self.git.push)
        await asyncio.to_thread(self.git.pull)

    async def post_init(self, app: Application):
        await app.bot.set_my_commands([
            BotCommand("tasks", "Ochiq vazifalar"), BotCommand("today", "Bugungi reja"),
            BotCommand("export", "Hammasini faylga"), BotCommand("sync", "Git sync"),
            BotCommand("new", "Kontekstni tozalash"), BotCommand("help", "Yordam"),
        ])

    # ---------- run ----------
    def run(self):
        if self.git.enabled:
            log.info("git sync: %s", self.git.pull())
        app = Application.builder().token(self.cfg.telegram_token).post_init(self.post_init).build()
        app.add_handler(CommandHandler(["start", "help"], self.on_start))
        app.add_handler(CommandHandler("tasks", self.on_tasks))
        app.add_handler(CommandHandler("today", self.on_today))
        app.add_handler(CommandHandler("export", self.on_export))
        app.add_handler(CommandHandler("sync", self.on_sync))
        app.add_handler(CommandHandler("new", self.on_new))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        app.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED, self.on_text))

        jq = app.job_queue
        jq.run_repeating(self.job_reminders, interval=60, first=10)
        if self.git.enabled:
            jq.run_repeating(self.job_sync, interval=300, first=300)
        if self.cfg.digest_time:
            h, m = map(int, self.cfg.digest_time.split(":"))
            jq.run_daily(self.job_digest, time=dtime(h, m, tzinfo=self.cfg.tz))

        log.info("Brain bot running. brain=%s models=%s", self.cfg.brain_dir, self.cfg.models)
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
