"""User-agent: a second Telegram *user* account that phones the owner,
plays a spoken reminder and repeats it while the owner says "ha".

Uses Telethon (MTProto login) + py-tgcalls (private voice calls).
The session string is kept in .state/agent.json — never in the brain repo.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import wave
from pathlib import Path

from .ai import pcm_to_wav

log = logging.getLogger(__name__)

try:  # optional: the bot still works without call support
    from telethon import TelegramClient, functions, types as tl
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


class Agent:
    def __init__(self, api_id: int, api_hash: str, phone: str, state_dir: Path, ai):
        self.api_id, self.api_hash, self.default_phone = api_id, api_hash, phone
        self.ai = ai  # Assistant: tts() / yes_no()
        self.file = state_dir / "agent.json"
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
            log.info("agent ready as %s", self.me)
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

        @self.calls.on_update(fl.stream_frame(Direction.INCOMING, Device.MICROPHONE))
        async def _frames(_, u):
            buf = self._rec.get(u.chat_id)
            if buf is not None:
                for f in u.frames:
                    buf.extend(f.frame)

        @self.calls.on_update(fl.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def _left(_, u):
            self._gone.add(u.chat_id)
            fut = self._ended.get(u.chat_id)
            if fut and not fut.done():
                fut.set_result(False)

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

    async def _play_and_wait(self, uid: int, path: Path, seconds: float) -> bool:
        """Play a file; True when it finished, False if the owner hung up."""
        fut = self._ended[uid] = asyncio.get_running_loop().create_future()
        await self.calls.play(uid, MediaStream(str(path), AudioQuality.HIGH,
                                               video_flags=MediaStream.Flags.IGNORE))
        try:
            return await asyncio.wait_for(fut, timeout=seconds + 15)
        except asyncio.TimeoutError:
            return uid not in self._gone

    async def _listen(self, uid: int) -> str:
        self._rec[uid] = bytearray()
        for _ in range(LISTEN_SECONDS * 10):
            if uid in self._gone:
                break
            await asyncio.sleep(0.1)
        pcm = bytes(self._rec.pop(uid, b""))
        if uid in self._gone or len(pcm) < REC_RATE * REC_CHANNELS * 2 // 2:  # < 0.5 s
            return "unclear"
        return await asyncio.to_thread(self.ai.yes_no, pcm_to_wav(pcm, REC_RATE, REC_CHANNELS))

    @staticmethod
    def _duration(path: Path) -> float:
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()

    async def call(self, uid: int, text: str) -> str:
        """Phone the owner, read `text`, then 'Qaytaraymi?' → repeat on 'ha'.
        Returns a short status for the chat."""
        if not self.ready:
            return "agent ulanmagan"
        async with self.lock:
            if not await self._owner_ready(uid):
                return "owner_unknown"
            msg = await self._audio(text.rstrip(".!?") + ". Qaytaraymi?", f"msg-{int(time.time())}")
            bye = self.audio_dir / "bye.wav"
            if not bye.exists():
                bye = await self._audio("Xo'p. Omad!", "bye")
            self._gone.discard(uid)
            length = self._duration(msg)
            fut = self._ended[uid] = asyncio.get_running_loop().create_future()
            try:
                await self.calls.play(uid, MediaStream(str(msg), AudioQuality.HIGH,
                                                       video_flags=MediaStream.Flags.IGNORE),
                                      CallConfig(timeout=45))
            except TimedOutAnswer:
                return "javob berilmadi"
            except (CallDeclined, CallBusy, CallDiscarded) as e:
                return f"qabul qilinmadi ({type(e).__name__})"
            heard = []
            try:
                await self.calls.record(uid, RecordStream(True, AudioQuality.HIGH))
                try:
                    finished = await asyncio.wait_for(fut, timeout=length + 15)
                except asyncio.TimeoutError:
                    finished = uid not in self._gone
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
            finally:
                try:
                    await self.calls.leave_call(uid)
                except Exception:
                    pass
                self._ended.pop(uid, None)
                msg.unlink(missing_ok=True)
            repeats = heard.count("yes")
            return "gaplashildi" + (f", {repeats} marta qaytarildi" if repeats else "")
