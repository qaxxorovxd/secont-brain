"""Gemini brain: turns messages into tool calls on the Brain and short replies."""
from __future__ import annotations

import io
import json
import logging
import time
import wave
from dataclasses import dataclass, field

from google import genai
from google.genai import types

from .storage import Brain, human_date, norm

log = logging.getLogger(__name__)

WEEKDAYS_UZ = ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"]

SYSTEM = """You are **Brain** — the owner's personal second memory, living in Telegram.
The owner forgets things; you remember everything for them, precisely.

LANGUAGE: reply in the language of the message (Uzbek Latin or English; mixed is fine). Uzbek → use Latin script.

STYLE (very important):
- Short, exact, beautiful. Telegram Markdown: **bold**, _italic_, `code`, bullet lists with "- ".
- Use emojis sparingly as markers: ✅ done, ⬜ open, ⏰ deadline, 📌 saved, 🔎 found, ⚠️ overdue.
- Usually 1–8 lines. No filler ("Albatta!", "Sure!"), no repeating the question.
- Dates like "31-okt, juma" (uz) or "Fri, Oct 31" (en). Never show raw ISO to the user.

WHAT TO DO WITH A MESSAGE (the raw message is ALREADY saved to the journal automatically):
1. Something to do / owe / deliver / buy / call → add_task. One task PER PERSON/ITEM.
   "5 kishiga referat qilishim kerak: A, B, C, D, E" → 5 add_task calls with the same `group` ("referat").
   Resolve relative dates ("ertaga", "juma", "2 kundan keyin", "next week") to YYYY-MM-DD using NOW.
   If a specific clock time is given, set due_time and remind_at (default: remind 1 hour before; for
   date-only deadlines remind_at = the day before 20:00 unless that is already past).
   "eslat"/"remind me" at a time → remind_at exactly then.
2. Something finished ("Irodaga qildim", "done with X") → complete_task on the matching open task(s).
   If several could match and it is genuinely unclear, ask one short question.
3. A fact worth remembering (birthday, password hint, address, decision, price, someone said X) → remember_fact.
   Long content (lecture notes, ideas, lists, plans > ~5 lines) → save_note.
4. A question → answer from CONTEXT; if not there, call search_brain (try 2–3 keyword variants,
   including name stems, e.g. "Iroda", "referat"). Never invent. If nothing found: say so in one line.
5. When listing tasks of a group/person, ALWAYS show both what is left AND what is already done
   (with done date), e.g.:
   **Referat** — 1/5 bajarildi
   ✅ Iroda Karimova — _29-okt_
   ⬜ Ali Valiyev · ⏰ 31-okt
   Mark overdue items with ⚠️.
6. Corrections ("yo'q, juma emas shanba", "o'chir") → update_task / cancel_task / update_fact.

AFTER SAVING reply very short, e.g. "📌 **Saqlandi:** Iroda Karimova — referat · ⏰ 31-okt, juma".

PHONE CALLS: a linked agent account can phone the owner. Reminders (remind_at) are ALSO delivered
by phone call automatically when calls are on. "Menga qo'ng'iroq qil / call me now ..." → phone_call.
For "soat 10 da qo'ng'iroq qilib eslat" → add_task with remind_at (the reminder will call).

LONG OUTPUT: if the answer would be longer than ~25 lines, or the owner asks for a file / txt / pdf /
"to'liq" export, call send_file with the full well-formatted Markdown content and reply with one short line.
"""


def pcm_to_wav(pcm: bytes, rate: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def h(t):
    """Add human-readable dates so the model copies them instead of computing weekdays."""
    if isinstance(t, dict):
        t = dict(t)
        if t.get("due"):
            t["due_human"] = human_date(t["due"])
        if t.get("done_at"):
            t["done_human"] = human_date(t["done_at"])
    return t


@dataclass
class Reply:
    text: str
    files: list[dict] = field(default_factory=list)  # {"title","content","format"}
    calls: list[str] = field(default_factory=list)  # texts to speak in a phone call


class Assistant:
    def __init__(self, brain: Brain, api_key: str, models: list[str]):
        self.brain = brain
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
            timeout=45_000, retry_options=types.HttpRetryOptions(attempts=1)))
        self.models = models
        self.history: list[types.Content] = []
        self.cooldown: dict[str, float] = {}
        self.tts_models = ["gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview",
                           "gemini-3.8-flash-lite-tts", "gemini-2.5-flash-preview-tts"]

    # ---------- low-level ----------
    def _generate(self, contents, config: types.GenerateContentConfig):
        """Try each model in order; free-tier limits are per model, so this spreads the load.
        Models that fail are skipped for a while (rate limit: 60s, gone/unknown: 1 day)."""
        last: Exception | None = None
        for attempt in range(2):
            ready = [m for m in self.models if self.cooldown.get(m, 0) <= time.time()] or self.models
            for model in ready:
                try:
                    cfg = config.model_copy()
                    if model.startswith("gemini-3"):
                        cfg.thinking_config = types.ThinkingConfig(thinking_level="low")
                    return model, self.client.models.generate_content(model=model, contents=contents, config=cfg)
                except Exception as e:  # rate limit / overload / unknown model → next model
                    code = getattr(e, "code", 0)
                    self.cooldown[model] = time.time() + (86400 if code == 404 else 60)
                    log.warning("model %s failed: %s", model, str(e)[:160])
                    last = e
            time.sleep(5)
        raise RuntimeError(f"Gemini javob bermadi: {str(last)[:200]}")

    def transcribe(self, data: bytes, mime: str) -> str:
        _, r = self._generate(
            [types.Content(role="user", parts=[
                types.Part.from_bytes(data=data, mime_type=mime),
                types.Part.from_text(text=(
                    "Transcribe this voice message verbatim. Language is Uzbek (Latin script) and/or English. "
                    "Write Uzbek in Latin script with correct apostrophes (o', g'). Keep names exactly. "
                    "Output only the transcript, nothing else.")),
            ])],
            types.GenerateContentConfig(temperature=0),
        )
        return (r.text or "").strip()

    def tts(self, text: str) -> bytes:
        """Text → WAV (24 kHz mono) with Gemini TTS."""
        last: Exception | None = None
        for model in self.tts_models:
            if self.cooldown.get(model, 0) > time.time():
                continue
            try:
                r = self.client.models.generate_content(
                    model=model,
                    contents=("Read this aloud in Uzbek (or English if the text is English), in a warm, "
                              f"calm, clear voice, at a natural pace:\n{text}"),
                    config=types.GenerateContentConfig(
                        response_modalities=["AUDIO"],
                        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")))),
                )
                pcm = r.candidates[0].content.parts[0].inline_data.data
                return pcm_to_wav(pcm, 24000, 1)
            except Exception as e:
                code = getattr(e, "code", 0)
                self.cooldown[model] = time.time() + (86400 if code == 404 else 60)
                log.warning("tts %s failed: %s", model, str(e)[:160])
                last = e
        raise RuntimeError(f"TTS ishlamadi: {str(last)[:200]}")

    def yes_no(self, wav: bytes) -> str:
        """Did the person answer 'yes, repeat' to 'Qaytaraymi?' → yes | no | unclear."""
        _, r = self._generate(
            [types.Content(role="user", parts=[
                types.Part.from_bytes(data=wav, mime_type="audio/wav"),
                types.Part.from_text(text=(
                    "In a phone call the person was just asked in Uzbek: 'Qaytaraymi?' (Shall I repeat?). "
                    "This is their answer. Reply with exactly one word: YES if they want it repeated "
                    "(ha, xa, ha qaytar, yana, takrorla, yes, again…), NO if not (yo'q, kerak emas, tushundim, "
                    "rahmat, bo'ldi, no, ok…), UNCLEAR if silence/noise.")),
            ])],
            types.GenerateContentConfig(temperature=0),
        )
        word = (r.text or "").strip().upper()
        return "yes" if word.startswith("YES") else "no" if word.startswith("NO") else "unclear"

    def describe(self, data: bytes, mime: str, caption: str) -> str:
        _, r = self._generate(
            [types.Content(role="user", parts=[
                types.Part.from_bytes(data=data, mime_type=mime),
                types.Part.from_text(text=(
                    "Describe this for a personal memory archive in 1–4 lines: what it is, and transcribe any "
                    "important text (names, dates, numbers, schedules) exactly. "
                    f"Owner's caption: {caption or '(none)'}")),
            ])],
            types.GenerateContentConfig(temperature=0),
        )
        return (r.text or "").strip()

    # ---------- main ----------
    def _context(self) -> str:
        now = self.brain.now()
        tasks = [h(t) for t in self.brain.context_tasks()]
        facts = self.brain.facts()[-6000:]
        notes = self.brain.notes_index()
        return (
            f"NOW: {now:%Y-%m-%d %H:%M} ({WEEKDAYS_UZ[now.weekday()]} / {now:%A}), timezone {self.brain.tz.key}\n\n"
            f"TASKS (open + recently done):\n{json.dumps(tasks, ensure_ascii=False) if tasks else '(none)'}\n\n"
            f"FACTS:\n{facts or '(none)'}\n\n"
            f"NOTES INDEX (id: title):\n" + ("\n".join(notes) or "(none)") + "\n\n"
            f"RECENT JOURNAL:\n{self.brain.recent_journal() or '(empty)'}"
        )

    def handle(self, text: str) -> Reply:
        b = self.brain
        out: list[dict] = []

        def add_task(title: str, person: str = "", group: str = "", due_date: str = "",
                     due_time: str = "", remind_at: str = "", details: str = "") -> dict:
            """Create a task / obligation. One call per person or item.
            title: short action, e.g. "Referat yozish". person: who it is for/with (full name as said).
            group: shared label for related tasks (e.g. "referat"). due_date: YYYY-MM-DD.
            due_time: HH:MM. remind_at: "YYYY-MM-DD HH:MM" when to ping the owner. details: extra info."""
            return h(b.add_task(title, person, group, due_date, due_time, remind_at, details))

        def complete_task(task_id: int) -> dict:
            """Mark a task as done (records the completion time)."""
            return h(b.update_task(task_id, status="done")) or {"error": "task not found"}

        def reopen_task(task_id: int) -> dict:
            """Mark a done/cancelled task as open again."""
            return h(b.update_task(task_id, status="open")) or {"error": "task not found"}

        def cancel_task(task_id: int) -> dict:
            """Cancel / delete a task that is no longer needed (kept in history as cancelled)."""
            return h(b.update_task(task_id, status="cancelled")) or {"error": "task not found"}

        def update_task(task_id: int, title: str = "", person: str = "", group: str = "",
                        due_date: str = "", due_time: str = "", remind_at: str = "", details: str = "") -> dict:
            """Change fields of a task. Only non-empty arguments are changed."""
            fields = {"title": title, "person": person, "group": group, "due": due_date,
                      "time": due_time, "remind_at": remind_at, "details": details}
            return h(b.update_task(task_id, **{k: v for k, v in fields.items() if v})) or {"error": "task not found"}

        def list_tasks(status: str = "all", query: str = "") -> list:
            """List tasks from the full history. status: open | done | cancelled | all.
            query: optional text filter on title/person/group/details."""
            res = []
            for t in b.tasks():
                if status != "all" and t["status"] != status:
                    continue
                blob = norm(" ".join(str(t.get(k, "")) for k in ("title", "person", "group", "details")))
                if query and not any(w[:5] in blob for w in norm(query).split()):
                    continue
                res.append(h(t))
            return res[-100:]

        def remember_fact(fact: str) -> str:
            """Store a short standalone fact in one sentence, with names and dates written out."""
            b.add_fact(fact)
            return "ok"

        def update_fact(old_text: str, new_fact: str = "") -> str:
            """Replace the fact containing old_text with new_fact; empty new_fact deletes it."""
            return "ok" if b.replace_fact(old_text, new_fact) else "not found"

        def save_note(title: str, content: str, tags: str = "") -> str:
            """Save a longer note (Markdown). Returns note id."""
            return b.save_note(title, content, tags)

        def update_note(note_id: str, content: str, append: bool = True) -> str:
            """Append to (or replace, append=false) the body of an existing note."""
            return "ok" if b.update_note(note_id, content, append) else "not found"

        def read_note(note_id: str) -> str:
            """Read a full note by id."""
            return b.read_note(note_id) or "not found"

        def search_brain(query: str) -> list:
            """Keyword search across ALL tasks (any age/status), facts, notes and the full journal."""
            return b.search(query) or ["no results"]

        phone_calls: list[str] = []

        def phone_call(message: str) -> str:
            """Phone the owner right now via the linked agent account and speak `message`
            (short, natural spoken Uzbek/English). Use when asked to call ("qo'ng'iroq qil", "call me")."""
            phone_calls.append(message)
            return "call queued"

        def send_file(title: str, content: str, format: str = "md") -> str:
            """Send long content to the owner as a file. format: md | txt | pdf."""
            out.append({"title": title, "content": content, "format": format})
            return "file will be sent"

        tools = [add_task, complete_task, reopen_task, cancel_task, update_task, list_tasks,
                 remember_fact, update_fact, save_note, update_note, read_note, search_brain, send_file, phone_call]

        user = types.Content(role="user", parts=[types.Part.from_text(text=text)])
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM + "\n\n=== CONTEXT ===\n" + self._context(),
            tools=tools,
            temperature=0.3,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        # Manual tool loop: tools run exactly once even if we switch models midway.
        fns = {f.__name__: f for f in tools}
        contents = self.history + [user]
        r, model, steps = None, "", 0
        for steps in range(12):
            model, r = self._generate(contents, config)
            calls = r.function_calls or []
            if not calls:
                break
            contents.append(r.candidates[0].content)
            parts = []
            for c in calls:
                try:
                    res = fns[c.name](**(c.args or {}))
                except Exception as e:
                    res = {"error": str(e)}
                log.info("tool %s(%s) -> %s", c.name, json.dumps(c.args, ensure_ascii=False)[:200], str(res)[:120])
                parts.append(types.Part.from_function_response(name=c.name, response={"result": res}))
            contents.append(types.Content(role="user", parts=parts))
        reply = (r.text or "").strip() if r else ""
        reply = reply or ("📎 Fayl tayyor." if out else "✅")
        log.info("model=%s steps=%d", model, steps)

        self.history += [user, types.Content(role="model", parts=[types.Part.from_text(text=reply)])]
        self.history = self.history[-16:]
        return Reply(reply, out, phone_calls)
