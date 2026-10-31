"""User-agent: a second Telegram *user* account that phones the owner,
plays a spoken reminder and repeats it while the owner says "ha".
On the owner's order it also writes to / phones other people, and forwards their replies.

Uses Telethon (MTProto login) + py-tgcalls (private voice calls).
The session string is kept in .state/agent.json — never in the brain repo.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import io
import shutil
import time
import unicodedata
import wave
from pathlib import Path

from .ai import pcm_to_wav

log = logging.getLogger(__name__)

try:  # optional: the bot still works without call support
    from telethon import TelegramClient, events, functions, types as tl, utils as tlutils
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession
    from pytgcalls import PyTgCalls, filters as fl
    from pytgcalls.exceptions import CallBusy, CallDeclined, CallDiscarded, TimedOutAnswer
    from pytgcalls.types import (AudioQuality, CallConfig, ChatUpdate, Device, Direction,
                                 MediaStream, RecordStream, StreamEnded)
    IMPORT_ERROR = ""
except Exception as e:  # pragma: no cover
    IMPORT_ERROR = str(e)

REC_RATE, REC_CHANNELS = AudioQuality.HIGH.value if not IMPORT_ERROR else (48000, 2)
LISTEN_SECONDS = 6
MAX_REPEATS = 3
DAILY_OUTREACH_LIMIT = 30  # protects the agent account from Telegram spam limits

# family words the owner uses → words a contact may be saved under (uz / en / ru)
RELATIONS = {
    "mother": ["ona", "onam", "onajon", "onamga", "oyi", "oyim", "oyijon", "aya", "ayam", "ayajon",
               "mama", "mamam", "mom", "mum", "mother", "мама", "мамочка", "мать", "ойи", "она"],
    "father": ["ota", "otam", "otajon", "dada", "dadam", "dadajon", "papa", "dad", "father",
               "папа", "отец", "ота", "дада"],
    "brother": ["aka", "akam", "akajon", "uka", "ukam", "brother", "bro", "брат", "ака", "ука"],
    "sister": ["opa", "opam", "opajon", "singil", "singlim", "sister", "sis", "сестра", "опа"],
    "wife": ["xotin", "xotinim", "ayolim", "rafiqam", "wife", "жена"],
    "husband": ["er", "erim", "turmush o'rtog'im", "husband", "муж"],
    "grandmother": ["buvi", "buvim", "buvijon", "momo", "momom", "grandma", "grandmother", "бабушка"],
    "grandfather": ["bobo", "bobom", "bobojon", "grandpa", "grandfather", "дедушка"],
}


def _low(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").lower().replace("‘", "'").replace("’", "'").replace("ʻ", "'").strip()


def match_name(query: str, names: list[str]) -> int:
    """Index of the contact name matching `query` (name, part of it, or a family word), or -1."""
    q = _low(query).lstrip("@")
    q = q[:-2] if q.endswith(("ga", "ka", "qa")) and len(q) > 4 else q  # "onamga" → "onam"
    low = [_low(n) for n in names]
    for i, n in enumerate(low):  # exact, then contains
        if q == n:
            return i
    for i, n in enumerate(low):  # a whole word of the name ("sardor" → "Sardor Aliyev")
        if q and q in n.split():
            return i
    for i, n in enumerate(low):  # start of a word, 4+ letters (never "Alisher" → "Ali")
        if len(q) >= 4 and any(w.startswith(q) for w in n.split()):
            return i
    for words in RELATIONS.values():
        if q in words:
            for i, n in enumerate(low):
                if any(w in n.split() or w == n for w in words) or any(w in n for w in words if len(w) >= 4):
                    return i
    return -1


class Agent:
    def __init__(self, api_id: int, api_hash: str, phone: str, state_dir: Path, ai):
        self.api_id, self.api_hash, self.default_phone = api_id, api_hash, phone
        self.ai = ai  # Assistant: tts() / yes_no()
        self.file = state_dir / "agent.json"
        self.contacts_file = state_dir / "contacts.json"  # people Marin reached for the owner
        self.on_reply = None  # async (contact: dict, text: str) -> None, set by the bot
        self.book: list = []  # the agent account's own Telegram contacts (tl.User)
        self.audio_dir = state_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.client: "TelegramClient | None" = None
        self.calls: "PyTgCalls | None" = None
        self.ready = False
        self.lock = asyncio.Lock()  # one call at a time
        self._login: dict = {}
        self._ended: dict[int, asyncio.Future] = {}
        self._rec: dict[int, bytearray] = {}
        self._gone: set[int] = set()
        self._frames_seen: dict[int, int] = {}
        self.busy = 0  # calls running or waiting for the lock (counted by the bot)

    # ---------- state ----------
    @property
    def available(self) -> bool:
        return not IMPORT_ERROR and bool(self.api_id and self.api_hash)

    def _load(self) -> dict:
        return json.loads(self.file.read_text()) if self.file.exists() else {}

    def _save(self, **kv) -> None:
        data = {**self._load(), **kv}
        self.file.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        try:
            os.chmod(self.file, 0o600)
        except OSError:
            pass

    @property
    def linked(self) -> bool:
        return bool(self._load().get("session"))

    @property
    def calls_enabled(self) -> bool:
        return self._load().get("calls", True)

    def set_calls(self, on: bool) -> None:
        self._save(calls=on)

    @property
    def me(self) -> str:
        return self._load().get("me", "")

    @property
    def owner_peer(self) -> dict | None:
        return self._load().get("owner")

    # ---------- lifecycle ----------
    async def start(self) -> None:
        if not self.available or not self.linked:
            return
        try:
            self.client = TelegramClient(StringSession(self._load()["session"]), self.api_id, self.api_hash)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                log.warning("agent session expired")
                self._save(session="")
                return
            await self._start_calls()
            await self.refresh_book()
            log.info("agent ready as %s, %d contacts", self.me, len(self.book))
        except Exception:
            log.exception("agent start failed")

    async def _start_calls(self) -> None:
        self.calls = PyTgCalls(self.client)
        await self.calls.start()

        @self.calls.on_update(fl.stream_end(StreamEnded.Type.AUDIO))
        async def _ended(_, u):
            fut = self._ended.get(u.chat_id)
            if fut and not fut.done():
                fut.set_result(True)

        @self.calls.on_update(fl.stream_frame(Direction.INCOMING, Device.MICROPHONE | Device.SPEAKER))
        async def _frames(_, u):
            self._frames_seen[u.chat_id] = self._frames_seen.get(u.chat_id, 0) + len(u.frames)
            buf = self._rec.get(u.chat_id)
            if buf is not None:
                for f in u.frames:
                    buf.extend(f.frame)

        @self.calls.on_update(fl.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def _left(_, u):
            log.info("call %s left/discarded: %s", u.chat_id, u.status)
            self._gone.add(u.chat_id)
            fut = self._ended.get(u.chat_id)
            if fut and not fut.done():
                fut.set_result(False)

        @self.client.on(events.NewMessage(incoming=True))
        async def _incoming(ev):
            try:
                await self._forward_reply(ev)
            except Exception:
                log.exception("forwarding reply failed")

        self.ready = True

    async def stop(self) -> None:
        self.ready = False
        if self.client:
            await self.client.disconnect()

    # ---------- login (driven from the bot menu) ----------
    async def login_phone(self, phone: str) -> None:
        client = TelegramClient(StringSession(), self.api_id, self.api_hash)
        await client.connect()
        sent = await client.send_code_request(phone)
        self._login = {"client": client, "phone": phone, "hash": sent.phone_code_hash}

    async def login_code(self, code: str) -> str:
        """Returns 'ok' or 'password' (2FA needed)."""
        c = self._login["client"]
        try:
            await c.sign_in(self._login["phone"], code, phone_code_hash=self._login["hash"])
        except SessionPasswordNeededError:
            return "password"
        await self._finish_login()
        return "ok"

    async def login_password(self, password: str) -> None:
        await self._login["client"].sign_in(password=password)
        await self._finish_login()

    async def _finish_login(self) -> None:
        c: TelegramClient = self._login.pop("client")
        me = await c.get_me()
        name = " ".join(x for x in (me.first_name, me.last_name) if x)
        self._save(session=c.session.save(), me=f"{name} (+{me.phone})", owner=None)
        self._login = {}
        await self.stop()
        self.client = c
        await self._start_calls()
        await self.refresh_book()

    def login_pending(self) -> bool:
        return bool(self._login)

    async def login_cancel(self) -> None:
        c = self._login.pop("client", None)
        self._login = {}
        if c:
            await c.disconnect()

    async def unlink(self) -> None:
        if self.client:
            try:
                await self.client.log_out()
            except Exception:
                pass
        await self.stop()
        self.client = self.calls = None
        self._save(session="", me="", owner=None)

    # ---------- reaching the owner ----------
    async def resolve_owner(self, user_id: int, username: str = "", phone: str = "") -> bool:
        """Make sure the agent account can address the owner (needs an access hash)."""
        c = self.client
        if not c:
            return False
        ent = None
        for how in ("id", "username", "phone"):
            try:
                if how == "id":
                    ent = await c.get_entity(user_id)
                elif how == "username" and username:
                    ent = await c.get_entity(username)
                elif how == "phone" and phone:
                    r = await c(functions.contacts.ImportContactsRequest([tl.InputPhoneContact(
                        client_id=0, phone=phone, first_name="Owner", last_name="")]))
                    ent = r.users[0] if r.users else None
            except Exception as e:
                log.info("resolve owner via %s failed: %s", how, e)
            if ent and getattr(ent, "id", None) == user_id:
                self._save(owner={"id": ent.id, "hash": ent.access_hash})
                return True
        return False

    async def _owner_ready(self, user_id: int) -> bool:
        peer = self.owner_peer
        if peer and peer["id"] == user_id:
            # warm Telethon's entity cache from the stored access hash
            self.client.session.process_entities(tl.contacts.ResolvedPeer(
                peer=tl.PeerUser(user_id), chats=[],
                users=[tl.User(id=user_id, access_hash=peer["hash"])]))
            return True
        return await self.resolve_owner(user_id)

    # ---------- the call ----------
    async def _audio(self, text: str, name: str) -> Path:
        path = self.audio_dir / f"{name}.wav"
        wav = await asyncio.to_thread(self.ai.tts, text)
        path.write_bytes(wav)
        return path

    @staticmethod
    def _stream(path: Path) -> "MediaStream":
        return MediaStream(str(path), AudioQuality.HIGH, video_flags=MediaStream.Flags.IGNORE)

    async def _wait_end(self, uid: int, fut: asyncio.Future, seconds: float) -> bool:
        """True when the clip finished playing, False if the owner hung up."""
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=seconds + 10)
        except asyncio.TimeoutError:  # no StreamEnded event: assume it finished
            return uid not in self._gone

    async def _play_and_wait(self, uid: int, path: Path, seconds: float) -> bool:
        fut = self._ended[uid] = asyncio.get_running_loop().create_future()
        await asyncio.wait_for(self.calls.play(uid, self._stream(path)), timeout=15)
        return await self._wait_end(uid, fut, seconds)

    async def _listen(self, uid: int) -> str:
        self._rec[uid] = bytearray()
        for _ in range(LISTEN_SECONDS * 10):
            if uid in self._gone:
                break
            await asyncio.sleep(0.1)
        pcm = bytes(self._rec.pop(uid, b""))
        log.info("call %s: listened %.1fs of audio (frames so far %d)", uid,
                 len(pcm) / (REC_RATE * REC_CHANNELS * 2), self._frames_seen.get(uid, 0))
        if uid in self._gone:
            return "hung_up"
        if len(pcm) < REC_RATE * REC_CHANNELS * 2 // 2:  # < 0.5 s: recording not working
            return "no_audio"
        answer = await asyncio.to_thread(self.ai.yes_no, pcm_to_wav(pcm, REC_RATE, REC_CHANNELS))
        log.info("call %s: answer=%s", uid, answer)
        return answer

    @staticmethod
    def _duration(path: Path) -> float:
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()

    async def _hangup(self, uid: int) -> None:
        """Leave the call and make sure py-tgcalls forgets it, whatever state it is in."""
        try:
            await asyncio.wait_for(self.calls.leave_call(uid), timeout=10)
        except Exception as e:
            log.info("call %s: leave_call: %s", uid, type(e).__name__)
        try:  # private API, best effort: drop half-open connections that leave_call can't see
            await asyncio.wait_for(self.calls._clear_call(uid), timeout=5)
        except Exception:
            pass
        await asyncio.sleep(3)  # let Telegram release the line, otherwise the next call is "busy"

    async def _dial(self, uid: int, path: Path) -> str:
        """Ring the owner and start playing. Returns 'ok' or a failure status."""
        for attempt in range(2):
            try:
                await asyncio.wait_for(self.calls.play(uid, self._stream(path), CallConfig(timeout=45)),
                                       timeout=75)
                return "ok"
            except TimedOutAnswer:
                return "javob berilmadi"
            except CallDeclined:
                return "rad etildi"
            except CallBusy:
                log.info("call %s: busy (attempt %d)", uid, attempt + 1)
                await self._hangup(uid)
                await asyncio.sleep(10)
            except (CallDiscarded, asyncio.TimeoutError) as e:
                log.warning("call %s: dial failed: %s", uid, type(e).__name__)
                await self._hangup(uid)
                return "ulanib bo'lmadi"
        return "band (boshqa qo'ng'iroqda)"

    async def call(self, uid: int, text: str, owner: bool = True) -> str:
        """Phone the owner (or, owner=False, an already resolved user), read `text`, then
        'Qaytaraymi?' → repeat on 'ha'. Returns a short status for the chat.
        Never hangs: the whole call is capped at 5 min."""
        if not self.ready:
            return "agent ulanmagan"
        async with self.lock:
            try:
                return await asyncio.wait_for(self._call(uid, text, owner), timeout=300)
            except asyncio.TimeoutError:
                log.warning("call %s: timed out, hanging up", uid)
                await self._hangup(uid)
                return "vaqt tugadi"

    async def _call(self, uid: int, text: str, owner: bool = True) -> str:
        if owner and not await self._owner_ready(uid):
            return "owner_unknown"
        msg = await self._audio(text.rstrip(".!?") + ". Qaytaraymi?", f"msg-{int(time.time())}")
        # v1 was made with the instruction-reading prompt; a full sentence is also read more reliably
        bye = self.audio_dir / "bye-v2.wav"
        if not bye.exists():
            bye = await self._audio("Yaxshi, tushunarli. Omad tilayman!", "bye-v2")
        length = self._duration(msg)
        self._gone.discard(uid)
        self._frames_seen[uid] = 0
        fut = self._ended[uid] = asyncio.get_running_loop().create_future()
        log.info("call %s: dialing (%.1fs message)", uid, length)
        status = await self._dial(uid, msg)
        if status != "ok":
            self._ended.pop(uid, None)
            msg.unlink(missing_ok=True)
            log.info("call %s: %s", uid, status)
            return status
        log.info("call %s: answered", uid)
        heard: list[str] = []
        try:
            try:
                await asyncio.wait_for(self.calls.record(uid, RecordStream(True, AudioQuality.HIGH)), timeout=15)
            except Exception as e:
                log.warning("call %s: record failed: %s", uid, e)
            finished = await self._wait_end(uid, fut, length)
            for _ in range(MAX_REPEATS):
                if not finished:
                    break
                answer = await self._listen(uid)
                heard.append(answer)
                if answer != "yes":
                    break
                finished = await self._play_and_wait(uid, msg, length)
            if uid not in self._gone:
                await self._play_and_wait(uid, bye, self._duration(bye))
        except Exception as e:
            log.exception("call %s: failed mid-call", uid)
            heard.append(f"xato: {type(e).__name__}")
        finally:
            await self._hangup(uid)
            self._ended.pop(uid, None)
            msg.unlink(missing_ok=True)
        log.info("call %s: done, heard=%s frames=%d", uid, heard, self._frames_seen.get(uid, 0))
        repeats = heard.count("yes")
        last = heard[-1] if heard else ("siz qo'yib qo'ydingiz" if uid in self._gone else "")
        if owner:
            note = {"no": "kerak emas dedingiz", "unclear": "javobingiz tushunilmadi",
                    "no_audio": "⚠️ ovozingiz eshitilmadi", "hung_up": "siz qo'yib qo'ydingiz"}.get(last, last)
        else:
            note = {"no": "qaytarish shart emas dedi", "unclear": "javobi tushunilmadi",
                    "no_audio": "ovozi eshitilmadi", "hung_up": "o'zi qo'yib qo'ydi"}.get(last, last)
        return "gaplashildi" + (f", {repeats} marta qaytarildi" if repeats else "") + (f" · {note}" if note else "")

    # ---------- reaching other people (on the owner's order) ----------
    def contacts(self) -> dict:
        return json.loads(self.contacts_file.read_text(encoding="utf-8")) if self.contacts_file.exists() else {}

    def _save_contacts(self, data: dict) -> None:
        self.contacts_file.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(self.contacts_file, 0o600)
        except OSError:
            pass

    def _remember(self, user, via: str, alias: str = "") -> dict:
        data = self.contacts()
        name = " ".join(x for x in (user.first_name, user.last_name) if x) or via
        aliases = data.get(str(user.id), {}).get("alias", [])
        if alias and alias not in aliases:
            aliases = aliases + [alias]
        c = {"id": user.id, "hash": user.access_hash, "name": name,
             "username": user.username or "", "phone": user.phone or "", "via": via, "alias": aliases}
        data[str(user.id)] = c
        self._save_contacts(data)
        return c

    def _count_today(self) -> int:
        """Increment and return today's outreach counter."""
        today = time.strftime("%Y-%m-%d")
        st = self._load().get("outreach", {})
        n = (st.get("n", 0) if st.get("day") == today else 0) + 1
        self._save(outreach={"day": today, "n": n})
        return n

    async def resolve(self, target: str):
        """@username / t.me link / +phone / name of someone reached before → Telegram user (or None)."""
        c, t = self.client, target.strip()
        m = re.search(r"(?:t\.me/|@)([A-Za-z0-9_]{4,32})", t)
        digits = re.sub(r"[^\d+]", "", t)
        # a bare word is a username only if it looks like one (has _ or a digit); a plain name like
        # "Sardor" must never resolve to some random public @Sardor
        bare_user = re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", t) and re.search(r"[_\d]", t)
        try:
            if m or bare_user:
                ent = await c.get_entity(m.group(1) if m else t)
                return ent if isinstance(ent, tl.User) and not ent.bot else None
            if len(digits.lstrip("+")) >= 9:
                r = await c(functions.contacts.ImportContactsRequest([tl.InputPhoneContact(
                    client_id=0, phone=digits, first_name=t, last_name="")]))
                return r.users[0] if r.users else None
        except Exception as e:
            log.info("resolve %s failed: %s", target, e)
            return None
        # a name or family word ("onam" → "Mother"): the agent's contacts, then people reached before
        for attempt in range(2):
            i = match_name(t, [self._name(u) for u in self.book])
            if i >= 0:
                return self.book[i]
            if attempt == 0:
                await self.refresh_book()  # maybe saved just now
        reached = list(self.contacts().values())
        i = match_name(t, [cc["name"] for cc in reached])
        if i < 0:
            i = next((k for k, cc in enumerate(reached) if cc["username"] and _low(t).lstrip("@") == _low(cc["username"])), -1)
        if i >= 0:
            cc = reached[i]
            self.client.session.process_entities(tl.contacts.ResolvedPeer(
                peer=tl.PeerUser(cc["id"]), chats=[],
                users=[tl.User(id=cc["id"], access_hash=cc["hash"])]))
            return await c.get_entity(cc["id"])
        return None

    @staticmethod
    def _name(u) -> str:
        return " ".join(x for x in (u.first_name, u.last_name) if x) or (u.username or "")

    async def refresh_book(self) -> None:
        """Load the agent account's saved Telegram contacts (names Marin can be told to reach)."""
        try:
            r = await self.client(functions.contacts.GetContactsRequest(hash=0))
            self.book = [u for u in getattr(r, "users", []) if not u.bot and not u.deleted]
        except Exception as e:
            log.warning("contacts load failed: %s", e)

    def book_names(self) -> list[str]:
        """Names only (no phone numbers) for Marin's context, with the owner's aliases and 🔕 for muted."""
        reached = self.contacts()
        out, seen = [], set()
        for u in self.book:
            cc = reached.get(str(u.id))
            out.append(self._label(cc) if cc else self._name(u))
            seen.add(str(u.id))
        out += [self._label(cc) for k, cc in reached.items() if k not in seen]
        return [n for n in out if n][:200]

    def _label(self, cc: dict) -> str:
        s = cc["name"]
        if cc.get("username"):
            s += f" @{cc['username']}"
        if cc.get("alias"):
            s += f" (janob: {', '.join(cc['alias'])})"
        return s + ("" if self.notify_allowed(cc["id"]) else " 🔕")

    # ---------- which replies reach the owner ----------
    def _filter(self) -> dict:
        f = self._load().get("reply_filter") or {}
        return {"mode": f.get("mode", "all"), "allow": f.get("allow", []), "mute": f.get("mute", [])}

    def notify_allowed(self, uid: int) -> bool:
        f = self._filter()
        if f["mode"] == "only":
            return uid in f["allow"]
        return uid not in f["mute"]

    def find_people(self, names: list[str]) -> tuple[list[dict], list[str]]:
        """Match names / aliases / @usernames / family words to known people → (found, unknown)."""
        people = {str(u.id): {"id": u.id, "name": self._name(u), "username": u.username or "", "alias": []}
                  for u in self.book}
        people.update(self.contacts())
        plist = list(people.values())
        keys = [" ".join([p["name"], p.get("username", "")] + p.get("alias", [])) for p in plist]
        found, unknown = [], []
        for n in names:
            n = n.strip()
            if not n:
                continue
            i = match_name(n, keys)
            if i < 0:  # alias given as a whole phrase, e.g. "opam" saved as alias
                i = next((k for k, p in enumerate(plist) if _low(n) in [_low(a) for a in p.get("alias", [])]), -1)
            (found.append(plist[i]) if i >= 0 else unknown.append(n))
        return found, unknown

    def set_notify(self, names: list[str], on: bool, only: bool = False) -> dict:
        """on=False: mute these people. on=True: unmute; with only=True: notify ONLY these.
        names empty + on + not only: back to "everyone"."""
        f = self._filter()
        found, unknown = self.find_people(names)
        ids = [p["id"] for p in found]
        if not names and on and not only:
            f = {"mode": "all", "allow": [], "mute": []}
        elif only:
            f = {"mode": "only", "allow": ids, "mute": []}
        elif on:
            f["mute"] = [x for x in f["mute"] if x not in ids]
            f["allow"] = sorted(set(f["allow"]) | set(ids))
        else:
            f["mute"] = sorted(set(f["mute"]) | set(ids))
            f["allow"] = [x for x in f["allow"] if x not in ids]
        self._save(reply_filter=f)
        return {"mode": "faqat ro'yxatdagilar" if f["mode"] == "only" else "hammasi (jimlardan tashqari)",
                "changed": [p["name"] for p in found], "not_found": unknown}

    async def reach(self, target: str, intro: str, message: str, mode: str = "call", alias: str = "") -> dict:
        """Write `intro` to the person, phone them and speak intro+message; if the call does not
        go through, send it as a voice note (text if that fails). Returns {"who", "steps"}."""
        if not self.ready:
            return {"who": target, "steps": ["⚠️ agent ulanmagan (/agent)"]}
        if self._count_today() > DAILY_OUTREACH_LIMIT:
            return {"who": target, "steps": [f"⚠️ kunlik limit ({DAILY_OUTREACH_LIMIT} ta) tugadi"]}
        user = await self.resolve(target)
        if not user:
            return {"who": target, "steps": ["⚠️ Telegramda topilmadi (username yoki raqamni tekshiring)"]}
        contact = self._remember(user, target, alias)
        who = contact["name"] + (f" (@{contact['username']})" if contact["username"] else "")
        steps: list[str] = []
        if mode == "text":
            await self.client.send_message(user, f"{intro}\n\n{message}")
            return {"who": who, "steps": ["✉️ xabar yozildi"], "contact": contact}
        if mode == "voice":
            if await self.send_voice(user, f"{intro} {message}"):
                return {"who": who, "steps": ["🎙 ovozli xabar yuborildi"], "contact": contact}
            await self.client.send_message(user, f"{intro}\n\n{message}")
            return {"who": who, "steps": ["⚠️ ovoz yaratilmadi", "✉️ matn qilib yozildi"], "contact": contact}
        await self.client.send_message(user, intro + "\nHozir sizga qo'ng'iroq qilib, xabarni aytib beraman.")
        steps.append("✉️ tanishtiruv yozildi")
        try:
            status = await self.call(user.id, f"{intro} {message}", owner=False)
        except Exception as e:
            log.exception("outreach call failed")
            status = f"qo'ng'iroq imkoni yo'q ({type(e).__name__})"
        steps.append(f"📞 {status}")
        if not status.startswith("gaplashildi"):
            caption = f"Qo'ng'iroq qila olmadim, shuning uchun ovozli xabar qoldiraman:\n\n{message}"
            if await self.send_voice(user, f"{intro} {message}", caption):
                steps.append("🎙 ovozli xabar yuborildi (matni bilan)")
            else:
                await self.client.send_message(
                    user, f"Qo'ng'iroq qila olmadim, shuning uchun xabarni yozib qoldiraman:\n\n{message}")
                steps.append("✉️ xabar matn qilib yuborildi")
        return {"who": who, "steps": steps, "contact": contact}

    # ---------- group live (voice chat / live stream) ----------
    async def find_group(self, name: str):
        """A group/channel the agent is in, by title (or @username / t.me link)."""
        m = re.search(r"(?:t\.me/|@)([A-Za-z0-9_]{4,32})", name)
        if m:
            try:
                return await self.client.get_entity(m.group(1))
            except Exception as e:
                log.info("group %s not found: %s", name, e)
                return None
        groups = [d async for d in self.client.iter_dialogs(limit=500) if d.is_group or d.is_channel]
        titles = [_low(d.name) for d in groups]
        # "TATU 2-kurs guruhi" / "Dasturchilar chatidagi" → drop the generic word
        q = re.sub(r"\b(guruh|gruppa|group|chat|kanal|channel|efir)\w*", " ", _low(name))
        q = re.sub(r"\s+", " ", q).strip(" '\"")
        for cand in (_low(name), q):
            if cand in titles:
                return groups[titles.index(cand)].entity
        i = match_name(q or name, [d.name for d in groups])
        if i < 0 and q:  # every word of the query starts a word of the title ("IT Park" → "IT Park Uzbekistan")
            words = q.split()
            hits = [k for k, t in enumerate(titles) if all(any(w2.startswith(w) for w2 in t.split()) for w in words)]
            i = hits[0] if len(hits) == 1 else -1
        if i < 0:  # a title written inside a longer phrase; longest title wins
            hits = [k for k, t in enumerate(titles) if len(t) >= 4 and t in _low(name)]
            i = max(hits, key=lambda k: len(titles[k])) if hits else -1
        return groups[i].entity if i >= 0 else None

    async def _live_now(self, ent) -> bool:
        if isinstance(ent, tl.Channel):
            full = await self.client(functions.channels.GetFullChannelRequest(ent))
        else:
            full = await self.client(functions.messages.GetFullChatRequest(ent.id))
        return getattr(full.full_chat, "call", None) is not None

    @staticmethod
    def _with_silence(wav: bytes, seconds: float) -> bytes:
        """Prepend `seconds` of silence to a WAV (so the speech starts after joining)."""
        with wave.open(io.BytesIO(wav)) as w:
            params, frames = w.getparams(), w.readframes(w.getnframes())
        pad = b"\x00" * int(params.framerate * seconds) * params.sampwidth * params.nchannels
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setparams(params)
            w.writeframes(pad + frames)
        return out.getvalue()

    async def live(self, group: str, text: str, delay: float = 3.0) -> dict:
        """Join the group's live voice chat, wait `delay` s, say `text`, leave. Returns {who, steps}."""
        if not self.ready:
            return {"who": group, "steps": ["⚠️ agent ulanmagan (/agent)"]}
        ent = await self.find_group(group)
        if not ent:
            return {"who": group, "steps": ["⚠️ guruh topilmadi — agent a'zo bo'lgan guruh nomini yozing"]}
        title = getattr(ent, "title", group)
        try:
            if not await self._live_now(ent):
                return {"who": title, "steps": ["📴 hozir jonli efir yo'q"]}
        except Exception as e:
            log.info("live check %s: %s", title, e)
        cid = tlutils.get_peer_id(ent)
        async with self.lock:
            try:
                steps = await asyncio.wait_for(self._live(cid, text, delay), timeout=180)
            except asyncio.TimeoutError:
                await self._hangup(cid)
                steps = ["⚠️ vaqt tugadi, chiqib ketdi"]
        return {"who": f"{title} (jonli efir)", "steps": steps}

    async def _live(self, cid: int, text: str, delay: float) -> list[str]:
        path = self.audio_dir / f"live-{int(time.time() * 1000)}.wav"
        try:
            wav = await asyncio.to_thread(self.ai.tts, text)
        except Exception as e:
            return [f"⚠️ ovoz yaratilmadi ({str(e)[:80]})"]
        path.write_bytes(self._with_silence(wav, delay))
        length = self._duration(path)
        self._gone.discard(cid)
        fut = self._ended[cid] = asyncio.get_running_loop().create_future()
        steps: list[str] = []
        try:
            try:
                await asyncio.wait_for(self.calls.play(cid, self._stream(path)), timeout=30)
            except Exception as e:
                log.warning("live %s: join failed: %s", cid, e)
                return [f"⚠️ efirga qo'shilib bo'lmadi ({type(e).__name__})"]
            steps.append("🎧 efirga qo'shildi")
            try:  # best effort: is the agent allowed to speak?
                me = (await self.client.get_me()).id
                for p in await self.calls.get_participants(cid):
                    if p.user_id == me and p.muted_by_admin:
                        steps.append("⚠️ admin ovozni o'chirgan — eshitilmagan bo'lishi mumkin")
            except Exception:
                pass
            finished = await self._wait_end(cid, fut, length)
            steps.append(f"🗣 {delay:g} soniyadan so'ng gap aytildi" if finished
                         else "⚠️ efirdan chiqarib yuborildi")
        finally:
            await self._hangup(cid)
            self._ended.pop(cid, None)
            path.unlink(missing_ok=True)
        steps.append("👋 chiqib ketdi")
        return steps

    async def send_voice(self, user, text: str, caption: str = "") -> bool:
        """Speak `text` with Marin's voice and send it as a Telegram voice note. False if it fails."""
        stem = self.audio_dir / f"note-{int(time.time() * 1000)}"
        wav, ogg = stem.with_suffix(".wav"), stem.with_suffix(".ogg")
        try:
            wav.write_bytes(await asyncio.to_thread(self.ai.tts, text))
            ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
            proc = await asyncio.create_subprocess_exec(
                ffmpeg, "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "libopus", "-b:a", "32k",
                "-ac", "1", "-ar", "48000", str(ogg),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0 or not ogg.exists():
                raise RuntimeError(err.decode(errors="replace")[-200:])
            await self.client.send_file(user, str(ogg), voice_note=True, caption=caption[:1000])
            return True
        except Exception as e:
            log.warning("voice note failed: %s", e)
            return False
        finally:
            wav.unlink(missing_ok=True)
            ogg.unlink(missing_ok=True)

    async def _forward_reply(self, ev) -> None:
        """A person Marin reached writes back → hand it to the owner. Marin never chats with them herself."""
        if not ev.is_private or not self.on_reply:
            return
        c = self.contacts().get(str(ev.sender_id))
        if not c:
            return
        text = ev.raw_text or ""
        if ev.voice or ev.audio:
            data = await ev.download_media(bytes)
            heard = await asyncio.to_thread(self.ai.transcribe, data, "audio/ogg")
            text = f"🎙 {heard}" if heard else "🎙 (ovozli xabar)"
        elif ev.media and not text:
            text = "📎 (fayl yoki rasm yubordi)"
        if text:
            await self.on_reply(c, text, self.notify_allowed(c["id"]))
