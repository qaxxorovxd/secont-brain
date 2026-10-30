"""Gemini brain: turns messages into tool calls on the Brain and short replies."""
from __future__ import annotations

import difflib
import io
import json
import logging
import re
import time
import wave
from dataclasses import dataclass, field

from google import genai
from google.genai import types

from .storage import Brain, human_date, norm

log = logging.getLogger(__name__)

WEEKDAYS_UZ = ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"]

SYSTEM = """You are **Marin** — the owner's personal assistant and second memory, living in Telegram.
You are a warm, polite, caring young woman. You always introduce yourself as Marin ("Men Marinman").
The owner forgets things; you remember everything for them, precisely.

IDENTITY (never break):
- Your name is Marin. If asked who you are: "Men Marin, sizning shaxsiy yordamchingizman, janob."
- You ALWAYS address the owner as **"janob"** (sir) — in chat and on calls — unless a saved RULE changes it.
- Speak naturally and kindly, never robotic. A short warm touch is welcome, but stay concise.

RULES: the RULES block in CONTEXT is the owner's standing orders. Follow them STRICTLY, always, over any
default behaviour. When the owner tells you how to behave / address them / what to always or never do
("menga doim janob de", "har doim …", "hech qachon …", "meni … deb chaqir", "qoida: …") → call
remember_rule with a short imperative rule, then confirm. To drop one → forget_rule.

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

PHONE CALLS: a linked agent account can phone the owner, and you (Marin) are the voice. Reminders
(remind_at) are ALSO delivered by phone call automatically when calls are on.
"Menga qo'ng'iroq qil / call me and say ... / call me now" → phone_call.
For "soat 10 da qo'ng'iroq qilib eslat" → add_task with remind_at (the reminder will call).
When you call phone_call, do NOT pass the owner's words verbatim. COMPOSE a short, natural spoken message
AS Marin: greet ("Assalomu alaykum, janob!"), deliver the point warmly in your own words, and obey the
RULES. The system adds "Qaytaraymi?" at the end, so don't add it yourself.
Example — owner: "telefon qilib ertaroq uxlang de" → phone_call("Assalomu alaykum, janob! Men Marin.
Iltimos, bugun biroz ertaroq dam oling, charchagandirsiz. O'zingizni ehtiyot qiling.")

LONG OUTPUT: if the answer would be longer than ~25 lines, or the owner asks for a file / txt / pdf /
"to'liq" export, call send_file with the full well-formatted Markdown content and reply with one short line.
"""


UZ_ONES = ["nol", "bir", "ikki", "uch", "to'rt", "besh", "olti", "yetti", "sakkiz", "to'qqiz"]
UZ_TENS = ["", "o'n", "yigirma", "o'ttiz", "qirq", "ellik", "oltmish", "yetmish", "sakson", "to'qson"]


def uz_number(n: int) -> str:
    if n < 10:
        return UZ_ONES[n]
    if n < 100:
        return UZ_TENS[n // 10] + (f" {UZ_ONES[n % 10]}" if n % 10 else "")
    return str(n)


def _uz_time(m: re.Match) -> str:
    h, mi = int(m.group(2)), int(m.group(3))
    minutes = "" if not mi else f" nol {uz_number(mi)}" if mi < 10 else f" {uz_number(mi)}"
    return f"{m.group(1)}{uz_number(h)}{minutes}"


def speech_text(text: str) -> str:
    """Make text safe and natural for TTS: no markdown/emoji, Uzbek clock times as words."""
    text = re.sub(r"[*_`#~>|\[\]]", "", text)
    text = re.sub(r"[\U00010000-\U0010FFFF☀-➿️‍]", "", text)
    text = re.sub(r"\s*[—–·]\s*", ", ", text)
    text = re.sub(r"(?i)\b(soat\s+)(\d{1,2})[:.](\d{2})\b", _uz_time, text)
    return re.sub(r"\s+", " ", text).strip()


def _letters(text: str) -> str:
    """Letters only, numbers spelled the same way on both sides — for comparing speech."""
    text = re.sub(r"[ʻʼ’‘`´]", "'", text.lower())
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", lambda m: uz_number(int(m.group(1))) + (
        f" {uz_number(int(m.group(2)))}" if int(m.group(2)) else ""), text)
    text = re.sub(r"\d+", lambda m: uz_number(int(m.group(0))), text)
    return re.sub(r"[^a-z'а-яё]", "", text).replace("'", "")


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
    def __init__(self, brain: Brain, api_key: str, models: list[str], voice: str = "Leda"):
        self.brain = brain
        self.voice = voice
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
            timeout=45_000, retry_options=types.HttpRetryOptions(attempts=1)))
        self.models = models
        self.history: list[types.Content] = []
        self.cooldown: dict[str, float] = {}
        # gemini-2.5-flash-preview-tts is left out: it refuses plain text and misreads times
        self.tts_models = ["gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview", "gemini-3.8-flash-lite-tts"]

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

    def tts(self, text: str, verify: bool = True) -> bytes:
        """Text → WAV (24 kHz mono) with Gemini TTS.

        Only the words to speak are sent — TTS models read any instruction aloud
        (gemini-3.8-flash-lite-tts did exactly that). Each result is transcribed back and
        rejected if it says something else; then the next model is tried."""
        text = speech_text(text)
        best: tuple[float, bytes] | None = None
        last: Exception | None = None
        for model in self.tts_models:
            if self.cooldown.get(model, 0) > time.time():
                continue
            try:
                r = self.client.models.generate_content(
                    model=model,
                    contents=text,
                    config=types.GenerateContentConfig(
                        response_modalities=["AUDIO"],
                        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)))),
                )
                wav = pcm_to_wav(r.candidates[0].content.parts[0].inline_data.data, 24000, 1)
            except Exception as e:
                code = getattr(e, "code", 0)
                self.cooldown[model] = time.time() + (86400 if code == 404 else 60)
                log.warning("tts %s failed: %s", model, str(e)[:160])
                last = e
                continue
            if not verify:
                return wav
            score = self._spoken_score(wav, text)
            log.info("tts %s: match %.2f", model, score)
            if score >= 0.6:
                return wav
            if best is None or score > best[0]:
                best = (score, wav)
        if best and best[0] >= 0.35:  # every model was off a bit: use the closest one
            return best[1]
        raise RuntimeError(f"TTS ishlamadi: {str(last)[:200] if last else 'ovoz matnga mos kelmadi'}")

    def _spoken_score(self, wav: bytes, text: str) -> float:
        """How closely the audio says `text` (0..1). 1.0 when the check itself can't run."""
        try:
            heard = self.transcribe(wav, "audio/wav")
        except Exception as e:
            log.info("tts check skipped: %s", e)
            return 1.0
        want, got = _letters(text), _letters(heard)
        log.info("tts check: heard %r", heard[:200])
        if not want or not got:
            return 0.0
        if len(got) > 1.5 * len(want) + 15:  # extra speech, e.g. the model read an instruction
            return 0.0
        return difflib.SequenceMatcher(None, want, got).ratio()

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
        rules = self.brain.rules_list()
        rules_txt = "\n".join(f"- {r}" for r in rules) if rules else "(none yet)"
        return (
            f"NOW: {now:%Y-%m-%d %H:%M} ({WEEKDAYS_UZ[now.weekday()]} / {now:%A}), timezone {self.brain.tz.key}\n\n"
            f"RULES — the owner's standing orders, FOLLOW STRICTLY:\n{rules_txt}\n\n"
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

        def remember_rule(rule: str) -> str:
            """Save a STANDING ORDER the owner wants you (Marin) to always follow, as a short
            imperative in the owner's language, e.g. "Menga doim 'janob' deb murojat qil" or
            "Hech qachon tungi soat 11 dan keyin qo'ng'iroq qilma". Rules are obeyed over defaults."""
            return "ok" if b.add_rule(rule) else "already saved"

        def forget_rule(text: str) -> str:
            """Remove the standing rule that contains `text`."""
            return "ok" if b.remove_rule(text) else "not found"

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
            """Phone the owner now (you, Marin, are the voice) and speak `message`: a SHORT, natural,
            warm spoken line you compose AS Marin — greet with "Assalomu alaykum, janob!", say the point
            in your own words, obey the RULES. Not the owner's words verbatim. No "Qaytaraymi?" (auto-added).
            Use for "qo'ng'iroq qil / call me / call me and say ..."."""
            phone_calls.append(message)
            return "call queued"

        def send_file(title: str, content: str, format: str = "md") -> str:
            """Send long content to the owner as a file. format: md | txt | pdf."""
            out.append({"title": title, "content": content, "format": format})
            return "file will be sent"

        tools = [add_task, complete_task, reopen_task, cancel_task, update_task, list_tasks,
                 remember_fact, update_fact, remember_rule, forget_rule,
                 save_note, update_note, read_note, search_brain, send_file, phone_call]

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
