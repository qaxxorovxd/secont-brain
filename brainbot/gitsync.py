"""Keep ./brain in sync with its own (private) git repo."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class GitSync:
    def __init__(self, path: Path, enabled: bool, lock):
        self.path = path
        self.lock = lock
        self.enabled = enabled and (path / ".git").exists()
        self.dirty = False

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True,
                              encoding="utf-8", timeout=120)

    def has_remote(self) -> bool:
        return bool(self._git("remote").stdout.strip())

    def mark_dirty(self, _msg: str = "") -> None:
        self.dirty = True

    def pull(self) -> str:
        if not self.enabled or not self.has_remote():
            return "no remote"
        with self.lock:
            r = self._git("pull", "--rebase", "--autostash")
        if r.returncode:
            log.warning("git pull failed: %s", r.stderr.strip())
            return "pull failed: " + r.stderr.strip()[-300:]
        return r.stdout.strip() or "ok"

    def push(self, force_commit: bool = False) -> str:
        if not self.enabled or not (self.dirty or force_commit):
            return "nothing to do"
        with self.lock:
            self.dirty = False
            self._git("add", "-A")
            if self._git("diff", "--cached", "--quiet").returncode:
                ident = [] if self._git("config", "user.email").stdout.strip() else \
                    ["-c", "user.name=brain-bot", "-c", "user.email=brain-bot@localhost"]
                self._git(*ident, "commit", "-m", "brain: auto-sync")
        if not self.has_remote():
            return "committed (no remote)"
        r = self._git("push")
        if r.returncode:  # remote moved on (another computer) → rebase and retry once
            self.pull()
            r = self._git("push")
        if r.returncode:
            log.warning("git push failed: %s", r.stderr.strip())
            self.dirty = True
            return "push failed: " + r.stderr.strip()[-300:]
        return "pushed"
