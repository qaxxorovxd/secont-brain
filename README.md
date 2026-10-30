# 🧠 AI Brain — Telegram second memory

Telegram bot + Gemini. Send it **text or voice** (uz / en) — it saves everything in time order into
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
