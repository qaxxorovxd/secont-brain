#!/usr/bin/env python3
"""Cross-platform launcher (Windows / Linux / macOS server).

    python3 run.py            # setup .venv if needed, then run the bot (auto-restart on crash)
    python3 run.py --once     # run without auto-restart (use this under systemd / docker)
    python3 run.py --setup    # only create .venv + install requirements

First run creates .venv and installs requirements.txt; they are reinstalled automatically
whenever requirements.txt changes.
"""
import hashlib
import os
import subprocess
import sys
import time
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQ = ROOT / "requirements.txt"
STAMP = VENV / ".requirements.sha256"


def setup() -> None:
    if not PY.exists():
        print("> creating .venv ...", flush=True)
        venv.create(VENV, with_pip=True)
    digest = hashlib.sha256(REQ.read_bytes()).hexdigest()
    if not STAMP.exists() or STAMP.read_text().strip() != digest:
        print("> installing requirements ...", flush=True)
        subprocess.check_call([str(PY), "-m", "pip", "install", "-q", "--upgrade", "pip"])
        subprocess.check_call([str(PY), "-m", "pip", "install", "-q", "-r", str(REQ)])
        STAMP.write_text(digest)


def main() -> int:
    args = set(sys.argv[1:])
    if not (ROOT / ".env").exists():
        print("ERROR: .env not found. Copy .env.example to .env and fill in the keys.")
        return 1
    setup()
    if "--setup" in args:
        print("OK: setup done")
        return 0

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    cmd = [str(PY), "-m", "brainbot"]
    if "--once" in args:
        return subprocess.call(cmd, cwd=ROOT, env=env)

    delay = 5
    while True:
        started = time.time()
        try:
            code = subprocess.call(cmd, cwd=ROOT, env=env)
        except KeyboardInterrupt:
            return 0
        if code in (0, 2):  # clean stop / config error
            return code
        # crashed: restart, backing off if it keeps failing quickly
        delay = 5 if time.time() - started > 60 else min(delay * 2, 300)
        print(f"! bot exited with code {code}, restarting in {delay}s ...", flush=True)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
