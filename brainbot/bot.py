"""Telegram front-end."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, time as dtime, timedelta
from functools import wraps

from telegram import (BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup, ReplyKeyboardRemove, Update)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from .agent import IMPORT_ERROR as AGENT_IMPORT_ERROR, Agent
from .ai import Assistant, Reply
from .config import Config
from .export import write_file
from .gitsync import GitSync
from .storage import Brain, human_date as fmt_date
from .tgformat import md_to_html

log = logging.getLogger(__name__)

HELP = """🧠 **Marin** — sizning shaxsiy yordamchingiz, janob

Men hammasini eslab qolaman. Menga **matn** yoki **ovozli xabar** yuboring — vaqt tartibida saqlayman, keyin so'rasangiz topib beraman.

**Misollar**
- _Iroda Karimovaga juma kunigacha referat qilishim kerak_
- _Irodaga qildim_
- _Kimlarga referat qilishim kerak?_
- _Ertaga 15:00 da eslat: dorixona_
- _Menga doim janob deb murojat qil_ (qoida sifatida saqlayman)

**Buyruqlar**
/tasks — ochiq vazifalar
/today — bugun va muddati o'tganlar
/export — hamma vazifalar fayl qilib
/sync — git sync (brain repo)
/new — suhbat kontekstini tozalash
/agent — 🤖 agent hisobi: qo'ng'iroq qilib eslatadi"""


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
        self.ai = Assistant(self.brain, cfg.gemini_key, cfg.models, cfg.tts_voice, cfg.owner_name)
        self.agent = Agent(cfg.tg_api_id, cfg.tg_api_hash, cfg.agent_phone, cfg.state_dir, self.ai)
        self.login_step: str | None = None  # phone | code | password
        self._call_tasks: set[asyncio.Task] = set()
        self._held: dict[str, dict] = {}  # outreach waiting for the owner's approval
        self.app: Application | None = None
        self.agent.on_reply = self.on_person_reply
        self.ai.address_book = self.agent.book_names
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
        for text in reply.calls:
            self.start_call(ctx, text)
        for o in reply.outreach:
            self._background(self.outreach(ctx, o))
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
        if self.login_step:
            await self.login_input(update, ctx, update.effective_message.text)
            return
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

    # ---------- agent (phone calls) ----------
    def agent_menu(self) -> tuple[str, InlineKeyboardMarkup]:
        a = self.agent
        if not a.available:
            why = AGENT_IMPORT_ERROR or "TG_API_ID / TG_API_HASH .env da yo'q"
            return f"🤖 **Agent**\n\n⚠️ Ishlamaydi: `{why[:200]}`", InlineKeyboardMarkup([])
        if not a.linked:
            return ("🤖 **Agent** — ikkinchi Telegram hisobi\n\n"
                    "Eslatma vaqti kelganda sizga **qo'ng'iroq qiladi**, ovozli xabarni o'qiydi va "
                    "_\"Qaytaraymi?\"_ deb so'raydi. **Ha** desangiz — yana qaytaradi.\n\n"
                    "Holat: ❌ ulanmagan",
                    InlineKeyboardMarkup([[Btn("🔗 Hisobni ulash", callback_data="ag:link")]]))
        on = a.calls_enabled
        status = "✅ tayyor" if a.ready else "⚠️ ulangan, lekin ishga tushmadi (loglarni ko'ring)"
        return (f"🤖 **Agent**\n\n👤 {a.me}\n📡 Holat: {status}\n"
                f"🔔 Eslatmalarda qo'ng'iroq: **{'yoqilgan' if on else 'o‘chirilgan'}**",
                InlineKeyboardMarkup([
                    [Btn("📞 Test qo'ng'iroq", callback_data="ag:test")],
                    [Btn(f"🔔 Qo'ng'iroq: {'ON ✅' if on else 'OFF ❌'}", callback_data="ag:toggle")],
                    [Btn("🔌 Hisobni uzish", callback_data="ag:unlink")],
                ]))

    @guarded
    async def on_agent(self, update: Update, ctx):
        text, kb = self.agent_menu()
        await update.effective_message.reply_text(md_to_html(text), parse_mode=ParseMode.HTML, reply_markup=kb)

    @guarded
    async def on_agent_button(self, update: Update, ctx):
        q = update.callback_query
        await q.answer()
        action = q.data.split(":", 1)[1]
        if action == "link":
            self.login_step = "phone"
            kb = [[Btn(f"✅ {self.cfg.agent_phone}", callback_data="ag:phone")]] if self.cfg.agent_phone else []
            kb.append([Btn("✖️ Bekor qilish", callback_data="ag:cancel")])
            await q.message.reply_text(md_to_html(
                "📱 Agent hisobining **telefon raqami**?\nTugmani bosing yoki raqamni yozing (`+998…`)."),
                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
            return
        if action == "phone":
            await self.login_input(update, ctx, self.cfg.agent_phone)
            return
        if action == "cancel":
            self.login_step = None
            await self.agent.login_cancel()
            await q.message.reply_text("✖️ Bekor qilindi.")
            return
        if action == "test":
            ok = self.start_call(ctx, "Assalomu alaykum, janob! Men Marin, sizning yordamchingiz. "
                                      "Bu test qo'ng'irog'i, hammasi joyida ishlayapti")
            await q.message.reply_text("📞 Qo'ng'iroq qilinyapti…" if ok
                                       else "⏳ Qo'ng'iroq allaqachon ketyapti, tugashini kuting.")
            return
        if action == "toggle":
            self.agent.set_calls(not self.agent.calls_enabled)
        elif action == "unlink":
            await self.agent.unlink()
            await q.message.reply_text("🔌 Agent hisobi uzildi.")
        text, kb = self.agent_menu()
        try:
            await q.edit_message_text(md_to_html(text), parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest:
            pass

    async def login_input(self, update: Update, ctx, text: str) -> None:
        chat = update.effective_chat.id
        msg = update.effective_message
        step = self.login_step
        try:
            if step == "phone":
                phone = "+" + re.sub(r"\D", "", text)
                await self.agent.login_phone(phone)
                self.login_step = "code"
                await self.send_md(chat, ctx,
                    f"🔐 Kod **{phone}** hisobiga (Telegram ilovasiga) yuborildi.\n\n"
                    "Kodni **bo'shliq bilan** yozing, masalan: `1 2 3 4 5`\n"
                    "_(kodni bitta raqam qilib yuborsangiz, Telegram uni bekor qilishi mumkin)_")
            elif step == "code":
                await self._delete(msg)
                if await self.agent.login_code(re.sub(r"\D", "", text)) == "password":
                    self.login_step = "password"
                    await self.send_md(chat, ctx, "🔑 Hisobda **2 bosqichli parol** bor. Parolni yuboring "
                                                  "_(xabaringiz darhol o'chiriladi)_.")
                    return
                await self.login_done(update, ctx)
            elif step == "password":
                await self._delete(msg)
                await self.agent.login_password(text.strip())
                await self.login_done(update, ctx)
        except Exception as e:
            log.warning("agent login (%s) failed: %s", step, e)
            await self.send_md(chat, ctx, f"⚠️ {type(e).__name__}: `{str(e)[:200]}`\n"
                                          "Qayta yozing yoki /agent → 🔗 Hisobni ulash.")
            if step == "phone" or "expired" in str(e).lower():
                self.login_step = None
                await self.agent.login_cancel()

    async def login_done(self, update: Update, ctx) -> None:
        self.login_step = None
        chat = update.effective_chat.id
        user = update.effective_user
        ok = await self.agent.resolve_owner(user.id, user.username or "")
        await self.send_md(chat, ctx, f"✅ **Ulandi:** {self.agent.me}")
        if not ok:
            await self.ask_contact(chat, ctx)
            return
        await self.send_md(chat, ctx,
            "📞 Endi /agent → **Test qo'ng'iroq** bosing.\n\n"
            "_Qo'ng'iroq kelmasa: Sozlamalar → Maxfiylik → Qo'ng'iroqlar — agent raqamiga ruxsat bering "
            "(yoki uni kontaktlarga qo'shing)._")

    async def ask_contact(self, chat: int, ctx) -> None:
        kb = ReplyKeyboardMarkup([[KeyboardButton("📱 Raqamimni yuborish", request_contact=True)]],
                                 resize_keyboard=True, one_time_keyboard=True)
        await ctx.bot.send_message(chat, md_to_html(
            "📇 Agent sizni topa olmadi. Qo'ng'iroq qila olishi uchun pastdagi tugma bilan "
            "**raqamingizni yuboring** (faqat agent kontaktiga qo'shiladi)."),
            parse_mode=ParseMode.HTML, reply_markup=kb)

    @guarded
    async def on_contact(self, update: Update, ctx):
        contact = update.effective_message.contact
        if contact.user_id != update.effective_user.id:
            await update.effective_message.reply_text("Faqat o'zingizning raqamingizni yuboring.")
            return
        ok = await self.agent.resolve_owner(contact.user_id, update.effective_user.username or "",
                                            contact.phone_number)
        await update.effective_message.reply_text(
            "✅ Topildi. /agent → 📞 Test qo'ng'iroq" if ok
            else "⚠️ Baribir topilmadi. Agent hisobiga bitta xabar yozing va qayta urining.",
            reply_markup=ReplyKeyboardRemove())

    @staticmethod
    async def _delete(msg) -> None:
        try:
            await msg.delete()
        except Exception:
            pass

    def _background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._call_tasks.add(task)  # keep a reference so the task isn't garbage-collected
        task.add_done_callback(self._call_tasks.discard)

    # ---------- reaching other people ----------
    async def outreach(self, ctx, o: dict, approved: bool = False) -> None:
        """Privacy-check, then message/call the person. Held messages wait for the owner's button."""
        chat = self.owner_chat
        try:
            if not self.agent.ready:
                await self.send_md(chat, ctx, "⚠️ Agent hisobi ulanmagan — /agent orqali ulang.")
                return
            if not approved:
                check = await asyncio.to_thread(self.ai.privacy_check, o["command"], o["message"])
                if not check["ok"]:
                    await self.hold(ctx, o, check)
                    return
            res = await self.agent.reach(o["target"], self.ai.intro(), o["message"], o["call"])
        except Exception as e:
            log.exception("outreach failed")
            res = {"who": o["target"], "steps": [f"⚠️ xatolik: {str(e)[:150]}"]}
        await self.send_md(chat, ctx, f"📨 **{res['who']}**\n" + "\n".join(f"- {s}" for s in res["steps"]))
        self.brain.log(f"[outreach] {res['who']}: {o['message']}\n→ {'; '.join(res['steps'])}", "outreach")
        if self.git.dirty:
            await asyncio.to_thread(self.git.push)

    async def hold(self, ctx, o: dict, check: dict) -> None:
        key = f"{int(time.time() * 1000) % 10**9}"
        self._held[key] = o
        why = "\n".join(f"- {r}" for r in check["reasons"])
        text = (f"🛡 **Xabar to'xtatildi** — {o['target']}\n{why}\n\n_Yubormoqchi bo'lgan matnim:_\n{o['message']}")
        buttons = [Btn("❌ Bekor qilish", callback_data=f"ox:no:{key}")]
        if not check["hard"]:
            buttons.insert(0, Btn("✅ Baribir yuborish", callback_data=f"ox:yes:{key}"))
        else:
            text += "\n\n⛔ Maxfiy kalit/parol bor — bunday xabar yuborilmaydi."
        await ctx.bot.send_message(self.owner_chat, md_to_html(text), parse_mode=ParseMode.HTML,
                                   reply_markup=InlineKeyboardMarkup([buttons]))

    @guarded
    async def on_outreach_button(self, update: Update, ctx):
        q = update.callback_query
        _, action, key = q.data.split(":", 2)
        o = self._held.pop(key, None)
        await q.answer()
        await q.edit_message_reply_markup(None)
        if not o:
            await self.send_md(self.owner_chat, ctx, "_Bu so'rov eskirgan._")
        elif action == "yes":
            await self.send_md(self.owner_chat, ctx, "📨 _Yuborilmoqda…_")
            self._background(self.outreach(ctx, o, approved=True))
        else:
            await self.send_md(self.owner_chat, ctx, "❌ _Bekor qilindi._")

    async def on_person_reply(self, contact: dict, text: str) -> None:
        """Someone Marin reached wrote back to the agent account → tell the owner."""
        who = contact["name"] + (f" (@{contact['username']})" if contact["username"] else "")
        self.brain.log(f"[reply] {who}: {text}", "reply")
        if self.owner_chat and self.app:
            md = f"💬 **{who}** javob yozdi:\n{text}"
            try:
                await self.app.bot.send_message(self.owner_chat, md_to_html(md), parse_mode=ParseMode.HTML)
            except BadRequest:
                await self.app.bot.send_message(self.owner_chat, md)

    def start_call(self, ctx, text: str) -> bool:
        """Queue a call in the background. At most one call waits behind the running one."""
        if not (self.agent.ready and self.owner_chat) or self.agent.busy >= 2:
            return False

        async def run():
            try:
                status = await self.agent.call(self.owner_chat, text)
            except Exception as e:
                log.exception("call failed")
                status = f"xatolik: {e}"
            finally:
                self.agent.busy -= 1
            if status == "owner_unknown":
                await self.ask_contact(self.owner_chat, ctx)
                return
            await self.send_md(self.owner_chat, ctx, f"📞 _Qo'ng'iroq: {status}_")
            self.brain.log(f"[call] {text}\n→ {status}", "call")

        self.agent.busy += 1
        task = asyncio.create_task(run())
        self._call_tasks.add(task)  # keep a reference so the task isn't garbage-collected
        task.add_done_callback(self._call_tasks.discard)
        return True

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
            if self.agent.calls_enabled:
                spoken = f"Assalomu alaykum, janob! Men Marin. Sizda eslatma bor: {name}."
                if t.get("time"):
                    spoken += f" Vaqti: soat {t['time']}."
                if t.get("details"):
                    spoken += f" {t['details']}."
                self.start_call(ctx, spoken)

    async def job_sync(self, ctx: ContextTypes.DEFAULT_TYPE):
        await asyncio.to_thread(self.git.push)
        await asyncio.to_thread(self.git.pull)

    async def post_init(self, app: Application):
        self.app = app
        await self.agent.start()
        await app.bot.set_my_commands([
            BotCommand("agent", "🤖 Agent: qo'ng'iroq / hisob ulash"),
            BotCommand("tasks", "Ochiq vazifalar"), BotCommand("today", "Bugungi reja"),
            BotCommand("export", "Hammasini faylga"), BotCommand("sync", "Git sync"),
            BotCommand("new", "Kontekstni tozalash"), BotCommand("help", "Yordam"),
        ])

    async def post_shutdown(self, app: Application):
        await self.agent.stop()

    # ---------- run ----------
    def run(self):
        if self.git.enabled:
            log.info("git sync: %s", self.git.pull())
        app = (Application.builder().token(self.cfg.telegram_token)
               .post_init(self.post_init).post_shutdown(self.post_shutdown).build())
        app.add_handler(CommandHandler(["start", "help"], self.on_start))
        app.add_handler(CommandHandler("tasks", self.on_tasks))
        app.add_handler(CommandHandler("today", self.on_today))
        app.add_handler(CommandHandler("export", self.on_export))
        app.add_handler(CommandHandler("sync", self.on_sync))
        app.add_handler(CommandHandler("new", self.on_new))
        app.add_handler(CommandHandler("agent", self.on_agent))
        app.add_handler(CallbackQueryHandler(self.on_agent_button, pattern=r"^ag:"))
        app.add_handler(CallbackQueryHandler(self.on_outreach_button, pattern=r"^ox:"))
        app.add_handler(MessageHandler(filters.CONTACT, self.on_contact))
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
