# 🧠 AI Brain — Telegram second memory (meet **Marin**)

Telegram bot + Gemini. The assistant is **Marin**: she introduces herself as Marin, addresses you as
**"janob"**, remembers everything. Send her **text or voice** (uz / en) — she saves it in time order into
`./brain/`, tracks tasks per person, and answers later: *"kimlarga referat qilishim kerak?"*

```
Sen:  Iroda Karimovaga juma kunigacha referat qilishim kerak
Bot:  📌 Saqlandi: Iroda Karimova — referat · ⏰ 30-okt, juma

Sen:  Yana Ali, Sardor, Madina va Jasurga ham, seshanbagacha
Bot:  📌 Saqlandi (4 ta) · ⏰ 3-noy, seshanba

Sen:  Irodaga qildim
  … 2 kundan keyin …
Sen:  kimlarga referat qilishim kerak?
Bot:  Referat — 1/5 bajarildi
      ✅ Iroda Karimova — 30-okt
      ⬜ Ali · ⏰ 3-noy, seshanba
      ⬜ Sardor · ⏰ 3-noy, seshanba  …
```

## Features
- 🎙 Voice messages → transcribed (Uzbek Latin / English) → processed like text
- 📋 Tasks per person, grouped ("5 kishiga referat" → 5 tasks), done/remaining with dates
- 🔔 Reminders at a given time + ☀️ morning digest (overdue / today / tomorrow)
- 📌 Facts & notes ("Alining tug'ilgan kuni 5-mart"), keyword search across the whole history
- 🖼 Photos / documents are stored in `brain/files/` and described by AI
- 📎 Long answers come as `.md` / `.txt` / `.pdf` files
- 🔄 `./brain` is its own **private git repo** → auto commit / pull / push for syncing computers
- 🔒 Owner-only (first user who writes, or `ALLOWED_USER_IDS`)
- 🪄 **Standing rules** — tell her "menga doim janob de" / "soat 23 dan keyin qo'ng'iroq qilma" and she
  saves it to `brain/rules.md` and follows it from then on (`remember_rule` / `forget_rule`)
- 🎙 Phone calls use a configurable youthful voice (`TTS_VOICE`, default `Leda`)
- 👤 **Owner profile** — `brain/profile.md` says who you are; always in context, so "men kimman?" just works.
  Tell her lasting facts about yourself and she adds them (`update_profile`)
- 🌐 **Web search** — when the brain has no answer she searches the internet (Gemini Google Search
  grounding, falling back to keyless DuckDuckGo) instead of saying "bilmayman"

## Setup
```bash
git clone https://github.com/qaxxorovxd/ai-brain.git ai-brain && cd ai-brain
cp .env.example .env      # put TELEGRAM_BOT_TOKEN and GEMINI_API_KEY
```
Run (any OS, Python 3.10+):
```bash
python3 run.py            # creates .venv, installs deps, runs the bot, auto-restarts on crash
python3 run.py --once     # no auto-restart (for systemd / docker)
python3 run.py --setup    # only install
```
Shortcuts: **Windows** → double-click `run.bat`; **Linux/macOS** → `./run.sh`.
Then open your bot in Telegram and press **Start**. The first person who writes becomes the owner.

### Server (Linux, systemd)
```bash
sudo apt install -y python3 python3-venv git
git clone https://github.com/qaxxorovxd/ai-brain.git ~/ai-brain && cd ~/ai-brain
cp .env.example .env && nano .env                 # keys + TIMEZONE
git clone git@github.com:<you>/my-brain.git brain  # private data repo (needs an SSH key on the server)
sudo cp deploy/brainbot.service /etc/systemd/system/   # edit User= and WorkingDirectory= first
sudo systemctl daemon-reload && sudo systemctl enable --now brainbot
journalctl -u brainbot -f                          # logs
```
Without systemd: `nohup python3 run.py > bot.log 2>&1 &` (or inside `tmux`).

## 📞 Agent — phone-call reminders
A second Telegram **user** account phones you when a reminder fires, reads it aloud (Gemini TTS)
and asks *"Qaytaraymi?"* — say **"ha"** and it repeats (up to 3×).

1. Get `api_id` / `api_hash` at <https://my.telegram.org> → put `TG_API_ID`, `TG_API_HASH`, `AGENT_PHONE` in `.env`.
2. In the bot: **/agent → 🔗 Hisobni ulash** → login code (type it with spaces: `1 2 3 4 5`) → 2FA password if any.
3. **/agent → 📞 Test qo'ng'iroq**. If the call doesn't ring: Settings → Privacy → Calls → allow the agent.

You can also just say *"menga hozir qo'ng'iroq qil"* or *"soat 10 da qo'ng'iroq qilib eslat"*.

### Contacting other people
*"@ali_valiyev ga qo'ng'iroq qil: …"* (📞 call) · *"opamga ovozli xabar tashla: …"* (🎙 voice note only) ·
*"+99890… ga yoz: …"* (✉️ text only)

1. Marin writes the message to that person (in the 3rd person: "Axmadjon aka … keladi").
2. 🛡 **Privacy check** before anything is sent: secrets (tokens, keys, passwords) are always blocked;
   phone numbers / emails / IPs pass only if you typed them in the command yourself; then an AI reviewer
   compares the text with your profile, facts and tasks. Anything doubtful is held and shown to you with
   **✅ Baribir yuborish / ❌ Bekor qilish**. If the reviewer is unavailable, nothing is sent (fail-closed).
3. The agent writes *"Assalomu alaykum! Siz bilan Marin, <OWNER_NAME>ning shaxsiy AI assistenti
   bog'lanyapti."*, phones them and speaks the message ("Qaytaraymi?" works for them too). If the call
   fails, it is sent as a **voice note** in her voice (with the text as caption; plain text if TTS fails).
   You get a report.
4. Their replies to the agent (text or voice → transcribed) are forwarded to you. Marin never chats with
   them on her own; say *"unga javob yoz: …"*.

### Writing in a group
*"TATU 2-kurs guruhiga yoz: ertaga dars yo'q"* · *"Oila guruhiga ovozli xabar tashla: …"* → the agent posts
your words (text or a voice note in Marin's voice) in that group; it must be a member allowed to write.

### Speaking in a group live (voice chat)
*"TATU 2-kurs jonli efiriga kir va 'bugun dars soat 2 da' de"* → the agent finds the group by title
(it must be a member), checks that a live voice chat is on, joins, waits 3 s, says your words in Marin's
voice and leaves. The report says if there was no live, if an admin muted the agent, or if it was removed.
Your words go through the same privacy check.

Plain names only match people Marin reached before (never a random public @username). Max 30 per day.
Reached people are kept in `.state/contacts.json`.
The session is stored in `.state/agent.json` (never in git). Server needs `ffmpeg`.

## Sync (`./brain` = separate private repo)
Create an **empty private repo** on GitHub (e.g. `my-brain`), then:
```bash
cd brain
git init -b main
git remote add origin git@github.com:<you>/my-brain.git
git add -A && git commit -m "init" && git push -u origin main
```
On another computer: clone the code, then `git clone git@github.com:<you>/my-brain.git brain`.
The bot commits + pushes after every change and pulls every 5 minutes (`/sync` forces it).

> ⚠️ Run the bot on **one** machine at a time (Telegram allows one poller per token).
> Other machines just get the synced `brain/` folder to read.

## Brain layout (plain files — readable without the bot)
```
brain/
  journal/2026/2026-10.md   every message, timestamped (append-only)
  tasks.json                tasks: person, group, due, remind_at, status, done_at
  facts.md                  short facts
  profile.md                who the owner is (always in context)
  rules.md                  standing orders
  notes/*.md                longer notes
  files/2026-10/…           photos & documents you sent
```

## Commands
| | |
|---|---|
| `/tasks` | open tasks (+ recently done) |
| `/today` | overdue · today · tomorrow |
| `/export` | all tasks + facts as a file |
| `/sync` | git pull + push of `brain/` |
| `/new` | clear chat context (memory stays) |

## Notes
- Free Gemini tier is ~5 requests/min per model; the bot automatically falls back through
  `GEMINI_MODELS`. With a paid key it is instant.
- Code: `brainbot/` — `storage.py` (files), `ai.py` (Gemini + tools), `bot.py` (Telegram),
  `gitsync.py`, `export.py`, `tgformat.py` (Markdown → Telegram HTML).
