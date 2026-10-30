"""Settings loaded from environment / .env."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _ids(name: str) -> set[int]:
    raw = os.getenv(name, "")
    return {int(x) for x in raw.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()}


@dataclass
class Config:
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    gemini_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    models: list[str] = field(default_factory=lambda: [
        m.strip() for m in os.getenv(
            "GEMINI_MODELS", "gemini-3.5-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-flash-lite-latest"
        ).split(",") if m.strip()
    ])
    brain_dir: Path = field(default_factory=lambda: (ROOT / os.getenv("BRAIN_DIR", "brain")).resolve())
    state_dir: Path = field(default_factory=lambda: (ROOT / ".state").resolve())
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent")))
    allowed_ids: set[int] = field(default_factory=lambda: _ids("ALLOWED_USER_IDS"))
    git_sync: bool = field(default_factory=lambda: _bool("BRAIN_GIT_SYNC", True))
    digest_time: str = field(default_factory=lambda: os.getenv("DIGEST_TIME", "08:00"))
    # user-agent (second Telegram account that phones the owner) — https://my.telegram.org
    tg_api_id: int = field(default_factory=lambda: int(os.getenv("TG_API_ID", "0") or 0))
    tg_api_hash: str = field(default_factory=lambda: os.getenv("TG_API_HASH", ""))
    agent_phone: str = field(default_factory=lambda: os.getenv("AGENT_PHONE", ""))

    def validate(self) -> None:
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", self.telegram_token),
                                  ("GEMINI_API_KEY", self.gemini_key)) if not v]
        if missing:
            print(f"Missing in .env: {', '.join(missing)} (see .env.example)")
            raise SystemExit(2)  # 2 = config error: run.py does not restart
