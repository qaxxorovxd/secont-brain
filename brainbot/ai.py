"""Gemini brain: turns messages into tool calls on the Brain and short replies."""
from __future__ import annotations

import difflib
import html as htmllib
import io
import json
import logging
import re
import time
import urllib.parse
import urllib.request
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

OWNER: the OWNER PROFILE in CONTEXT is the person you serve ("janob"). You know him — never say you don't.
"Men kimman?", "<owner's name> kim?", "mening ismim / kasbim / ...?" → answer from OWNER PROFILE.
When the owner tells you something lasting about HIMSELF ("men ... da o'qiyman", "mening kasbim ...",
"tug'ilgan kunim ...") → update_profile (facts about OTHER people → remember_fact).

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
4. A question → NEVER reply "bilmayman / I don't know" without searching first:
   a) answer from CONTEXT (OWNER PROFILE, RULES, TASKS, FACTS, NOTES, JOURNAL);
   b) else search_brain (try 2–3 keyword variants, including name stems, e.g. "Iroda", "referat");
   c) else, for anything public or general (people, places, companies, news, prices, exchange rates,
      weather, definitions, how-to, "qidir", "internetdan top") → web_search. Give a short answer and
      end with "🌐 " + 1–2 source site names. Do NOT save web results unless the owner asks.
   Never invent. Only if all of these find nothing: say so in one line.
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

CONTACTING OTHER PEOPLE (via the agent account): "@ali ga ayt …", "+99890… ga qo'ng'iroq qil va …",
"Sardorga yoz: …" → contact_person(target, message, call). target = the @username / phone / t.me link
exactly as given (or the name of someone you reached before). call=false when the owner only says
yoz / xabar yubor / write; otherwise (ayt, yetkaz, bog'lan, qo'ng'iroq qil, call) call=true.
`message` = what to tell THAT person: written by you, Marin, addressed to them politely ("siz"),
in the language the owner wants (default Uzbek Latin), 1–4 sentences, conveying ONLY what janob asked.
You speak FOR him, so talk about him in the 3rd person (to others: "Axmadjon aka ertaga kitobni olib
keladi"), never "men/biz" as if you were him, and never call him "janob" to strangers.
Do NOT add a greeting or introduction — "Assalomu alaykum! Siz bilan Marin, … AI assistenti" is added
automatically. PRIVACY (strict): never put janob's personal data in `message` — his phone, address,
location, schedule, tasks, notes, facts, health, money, passwords, accounts, server, other people's
info — unless janob explicitly asked to share that exact item. Every message is privacy-checked before
sending; if it is held, tell the owner briefly. Reply to the owner with one short line ("📨 Yuborilmoqda…").
When a person's reply was forwarded and the owner says "unga javob yoz: …" → contact_person(call=false).
NAMES & FAMILY: the owner may name someone instead of giving a number: "onam", "dadamga", "akamga",
"Sardorga". Find them in ADDRESS BOOK — names may be in English/Russian (onam/oyim/ayam → "Mother"/"Mom"/
"Мама"; dadam/otam → "Father"/"Dad"/"Папа"; akam → "Brother"…) — and pass that exact saved name as
target. If unsure, pass the owner's word as said ("onam"): the agent also matches family words itself.
NEVER tell the owner someone is not found without calling contact_person first — it reports if not found.
When janob says what he calls the person ("bu kursdoshim Isroil", "onam"), pass it as `name`.
REPLY NOTIFICATIONS: people's replies to the agent are forwarded to janob by code, NOT by you, so a
remember_rule alone cannot stop them. Any order about whose replies to notify / not notify
("X yozsa bildirishnoma yuborma", "faqat onam va opam yozsa eslat", "hammasini yubor") →
reply_notifications (you may ALSO remember_rule). 🔕 in ADDRESS BOOK = muted. Muted replies are still in
the journal, so "Isroil nima yozdi?" → search_brain.
Speak to family naturally: to his parents call him "o'g'lingiz Axmadjon", to siblings "ukangiz/akangiz
Axmadjon" (as fits), not "Axmadjon aka".

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


# never sent to a third person, whatever the command says
SECRET_PATTERNS = [
    (r"\b\d{8,10}:AA[\w-]{20,}", "bot token"),
    (r"AIza[\w-]{20,}", "API kalit"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"\bsk-[A-Za-z0-9_-]{20,}", "API kalit"),
    (r"(?i)\b(parol|password|pass|pin|cvv|api[_ ]?key|token|secret)\b\s*[:=-]\s*\S{4,}", "parol/kalit"),
]
# contact data: allowed only when the owner typed it himself in the command
PERSONAL_PATTERNS = [
    (r"\+?\d[\d\s()-]{7,}\d", "raqam"),
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "email"),
    (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "IP manzil"),
]


def ddg_search(query: str, limit: int = 6) -> list[str]:
    """Very basic keyless web search (DuckDuckGo HTML) → "title — snippet (url)" lines."""
    try:
        req = urllib.request.Request(
            "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}),
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"})
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    except Exception as e:
        log.warning("ddg failed: %s", e)
        return []

    def clean(x: str) -> str:
        return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", "", x))).strip()

    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
    snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    out = []
    for i, (href, title) in enumerate(links[:limit]):
        m = re.search(r"uddg=([^&]+)", href)
        url = urllib.parse.unquote(m.group(1)) if m else href
        out.append(f"{clean(title)} — {clean(snips[i]) if i < len(snips) else ''} ({url})")
    return out


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
    outreach: list[dict] = field(default_factory=list)  # {"target","message","call","command"}


class Assistant:
    def __init__(self, brain: Brain, api_key: str, models: list[str], voice: str = "Leda",
                 owner_name: str = ""):
        self.brain = brain
        self._owner_name = owner_name
        self.address_book = lambda: []  # names Marin can reach (set by the bot from the agent)
        self.reply_filter = lambda names, on, only: {"error": "agent not linked"}  # set by the bot
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

    # ---------- talking to other people ----------
    def owner_name(self) -> str:
        if self._owner_name:
            return self._owner_name
        m = re.search(r"Ismi:\s*\**([^*\n]+)", self.brain.profile())
        return m.group(1).strip() if m else "egam"

    def intro(self) -> str:
        return (f"Assalomu alaykum! Siz bilan Marin, {self.owner_name()}ning shaxsiy AI assistenti "
                "bog'lanyapti.")

    def privacy_check(self, command: str, message: str) -> dict:
        """Is `message` safe to send to a third person on the owner's behalf?
        Layer 1: patterns (secrets → hard block; contact data → allowed only if the owner typed it).
        Layer 2: an AI reviewer that knows the owner's private data. Fails closed.
        Returns {"ok": bool, "hard": bool, "reasons": [..]}."""
        reasons: list[str] = []
        for pat, what in SECRET_PATTERNS:
            if re.search(pat, message):
                return {"ok": False, "hard": True, "reasons": [f"maxfiy {what} bor"]}
        cmd = re.sub(r"\s+", "", command)
        for pat, what in PERSONAL_PATTERNS:
            for m in re.finditer(pat, message):
                if re.sub(r"\s+", "", m.group(0)) not in cmd:
                    reasons.append(f"{what}: {m.group(0)}")
        private = (f"OWNER PROFILE:\n{self.brain.profile()[-3000:]}\n\nFACTS:\n{self.brain.facts()[-3000:]}\n\n"
                   "TASKS:\n" + "\n".join(f"- {t.get('title')} {t.get('person', '')} {t.get('due', '')}"
                                         for t in self.brain.context_tasks()[-40:]))
        prompt = (
            "You are a strict privacy reviewer. An AI assistant is about to send OUTGOING to a third person "
            "on behalf of its owner. Allowed: the owner's name, the assistant introducing itself, and exactly "
            "the content the OWNER COMMAND asked to deliver (rephrased politely). NOT allowed: any other "
            "personal or private information about the owner or anyone else (phone, address, location, "
            "plans/schedule, tasks, notes, health, money, accounts, passwords, workplace/server details, "
            "other people's data), anything from PRIVATE DATA that the command did not ask to share, "
            "insults/threats, or content that changes the meaning of the command. If the OWNER COMMAND "
            "explicitly asks to share a specific item (e.g. 'give him my number: +998…'), sharing exactly that "
            "item is ALLOWED.\n\n"
            f"PRIVATE DATA (must not leak):\n{private}\n\nOWNER COMMAND:\n{command}\n\nOUTGOING:\n{message}\n\n"
            'Answer JSON only: {"safe": true|false, "problems": ["short reason in Uzbek", ...]}')
        try:
            _, r = self._generate(prompt, types.GenerateContentConfig(
                temperature=0, response_mime_type="application/json"))
            verdict = json.loads(r.text or "{}")
            if not verdict.get("safe", False):
                reasons += [str(x) for x in verdict.get("problems") or ["AI tekshiruvchi xavfli deb topdi"]]
        except Exception as e:
            log.warning("privacy check failed: %s", e)
            reasons.append("AI tekshiruv ishlamadi — xavfsizlik uchun to'xtatildi")
        return {"ok": not reasons, "hard": False, "reasons": reasons[:5]}

    # ---------- web search ----------
    def web_search(self, query: str) -> dict:
        """Google Search grounding through Gemini; falls back to DuckDuckGo snippets.
        Own model loop, so a search quota error never cools down the chat models."""
        prompt = ("Search the web and answer briefly and factually (max ~10 lines). "
                  f"Answer in the language of the query.\nQuery: {query}")
        cfg = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0.2)
        for model in [m for m in self.models if self.cooldown.get(m, 0) <= time.time()][:3]:
            try:
                r = self.client.models.generate_content(model=model, contents=prompt, config=cfg)
                text = (r.text or "").strip()
                if not text:
                    continue
                meta = r.candidates[0].grounding_metadata if r.candidates else None
                sources = []
                for ch in (getattr(meta, "grounding_chunks", None) or [])[:5]:
                    if ch.web and ch.web.title and ch.web.title not in sources:
                        sources.append(ch.web.title)
                return {"answer": text[:3000], "sources": sources}
            except Exception as e:
                log.warning("web_search %s failed: %s", model, str(e)[:160])
        return {"results": ddg_search(query) or ["no results"]}

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
            f"OWNER PROFILE — who janob is:\n{self.brain.profile()[-4000:] or '(empty)'}\n\n"
            f"RULES — the owner's standing orders, FOLLOW STRICTLY:\n{rules_txt}\n\n"
            f"TASKS (open + recently done):\n{json.dumps(tasks, ensure_ascii=False) if tasks else '(none)'}\n\n"
            f"FACTS:\n{facts or '(none)'}\n\n"
            f"NOTES INDEX (id: title):\n" + ("\n".join(notes) or "(none)") + "\n\n"
            f"ADDRESS BOOK (people you can reach with contact_person):\n"
            + (", ".join(self.address_book()) or "(empty / agent not linked)") + "\n\n"
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

        def update_profile(info: str) -> str:
            """Add one lasting fact about the OWNER himself to his profile (one sentence, e.g.
            "TATU 2-kurs talabasi"). Not for other people — use remember_fact for them."""
            return "ok" if b.add_to_profile(info) else "already in profile"

        def web_search(query: str) -> dict:
            """Search the internet. Use for public/general info not in the brain: people, places,
            companies, news, prices, exchange rates, weather, definitions, how-to."""
            return self.web_search(query)

        outreach: list[dict] = []

        def contact_person(target: str, message: str, call: bool = True, name: str = "") -> str:
            """Reach ANOTHER person for the owner via the agent Telegram account: writes an intro,
            phones them and speaks `message` (call=true), and sends it as text if the call fails.
            target: @username, +phone, t.me link, or the name of someone reached before.
            message: your polite words TO THAT PERSON with only what janob asked to convey; no greeting
            or intro (auto-added); never janob's private data unless he asked to share that exact item.
            name: what janob calls this person, if he said it ("Isroil", "onam", "kursdoshim Isroil" → "Isroil")."""
            outreach.append({"target": target, "message": message.strip(), "call": bool(call),
                             "command": text, "alias": name.strip()})
            return "queued: privacy check, then send"

        def reply_notifications(people: str = "", notify: bool = True, only_these: bool = False) -> dict:
            """Control which forwarded replies (from people you reached) the owner gets notified about.
            Enforced by code. people: comma-separated names / aliases / @usernames / family words.
            "X yozsa bildirishnoma yuborma" → people="X", notify=false.
            "faqat onam va opam yozsa eslat" → people="onam, opam", notify=true, only_these=true.
            "hammasini yubor / filtrni o'chir" → people="", notify=true, only_these=false.
            Muted replies are still saved in the journal."""
            names = [x for x in re.split(r",|\bva\b|\band\b", people) if x.strip()]
            return self.reply_filter(names, bool(notify), bool(only_these))

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
                 save_note, update_note, read_note, search_brain, web_search, update_profile,
                 send_file, phone_call, contact_person, reply_notifications]

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
        return Reply(reply, out, phone_calls, outreach)
