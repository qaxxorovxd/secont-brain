"""Plain-file "brain": everything lives in human-readable files so it can be
synced with git and read/edited by hand.

brain/
  journal/YYYY/YYYY-MM.md   every input, in time order (append-only)
  tasks.json                tasks / obligations / reminders
  facts.md                  short facts ("Ali's birthday is 5 March")
  notes/*.md                longer notes
  files/YYYY-MM/...         photos / documents you sent
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

TASK_FIELDS = ("title", "person", "group", "due", "time", "remind_at", "details", "status")
MONTHS_UZ = ["yan", "fev", "mar", "apr", "may", "iyun", "iyul", "avg", "sen", "okt", "noy", "dek"]
DAYS_UZ = ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"]


def human_date(d: str) -> str:
    """'2026-10-30' → '30-okt, juma'."""
    try:
        dt = datetime.strptime(d[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return d
    return f"{dt.day}-{MONTHS_UZ[dt.month - 1]}, {DAYS_UZ[dt.weekday()]}"


def norm(s: str) -> str:
    """Lowercase + unify Uzbek apostrophes so o'/oʻ/o’ all match."""
    return re.sub(r"[ʻʼ’‘`´]", "'", (s or "").lower())


def slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", norm(s).replace("'", ""), flags=re.UNICODE)
    return re.sub(r"[\s_-]+", "-", s).strip("-")[:50] or "note"


class Brain:
    def __init__(self, root: Path, tz: ZoneInfo):
        self.root = root
        self.tz = tz
        self.lock = threading.RLock()
        self.on_change: Callable[[str], None] | None = None
        for d in ("journal", "notes", "files"):
            (root / d).mkdir(parents=True, exist_ok=True)
        self.tasks_path = root / "tasks.json"
        self.facts_path = root / "facts.md"
        self.rules_path = root / "rules.md"

    # ---------- helpers ----------
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def stamp(self) -> str:
        return self.now().strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)

    def _changed(self, msg: str) -> None:
        if self.on_change:
            try:
                self.on_change(msg)
            except Exception:
                pass

    # ---------- journal ----------
    def log(self, text: str, kind: str = "text") -> None:
        now = self.now()
        path = self.root / "journal" / f"{now:%Y}" / f"{now:%Y-%m}.md"
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                if new:
                    f.write(f"# Journal {now:%Y-%m}\n")
                f.write(f"\n## {now:%Y-%m-%d %H:%M} · {kind}\n\n{text.strip()}\n")
        self._changed(f"journal: {kind}")

    def _journal_files(self) -> list[Path]:
        return sorted((self.root / "journal").glob("*/*.md"))

    def journal_entries(self) -> list[str]:
        out: list[str] = []
        for p in self._journal_files():
            parts = p.read_text(encoding="utf-8").split("\n## ")[1:]
            out.extend("## " + e.strip() for e in parts)
        return out

    def recent_journal(self, n: int = 15, max_chars: int = 4000) -> str:
        entries = []
        files = self._journal_files()
        for p in reversed(files[-2:]):
            parts = p.read_text(encoding="utf-8").split("\n## ")[1:]
            entries = ["## " + e.strip() for e in parts] + entries
        text = "\n\n".join(entries[-n:])
        return text[-max_chars:]

    # ---------- tasks ----------
    def _load(self) -> dict:
        if not self.tasks_path.exists():
            return {"next_id": 1, "tasks": []}
        return json.loads(self.tasks_path.read_text(encoding="utf-8"))

    def _save(self, data: dict, msg: str) -> None:
        self._write(self.tasks_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        self._changed(msg)

    def tasks(self) -> list[dict]:
        with self.lock:
            return self._load()["tasks"]

    def get_task(self, task_id: int) -> dict | None:
        return next((t for t in self.tasks() if t["id"] == task_id), None)

    def add_task(self, title: str, person: str = "", group: str = "", due: str = "",
                 time: str = "", remind_at: str = "", details: str = "") -> dict:
        with self.lock:
            data = self._load()
            t = {
                "id": data["next_id"], "title": title.strip(), "person": person.strip(),
                "group": group.strip(), "due": due.strip(), "time": time.strip(),
                "remind_at": remind_at.strip(), "reminded": False, "details": details.strip(),
                "status": "open", "created": self.stamp(), "updated": self.stamp(), "done_at": "",
            }
            data["next_id"] += 1
            data["tasks"].append(t)
            self._save(data, f"task #{t['id']} added")
            return t

    def update_task(self, task_id: int, **fields) -> dict | None:
        with self.lock:
            data = self._load()
            for t in data["tasks"]:
                if t["id"] != task_id:
                    continue
                for k, v in fields.items():
                    if k in TASK_FIELDS and v is not None:
                        t[k] = v.strip() if isinstance(v, str) else v
                if "remind_at" in fields:
                    t["reminded"] = False
                if fields.get("status") == "done" and not t.get("done_at"):
                    t["done_at"] = self.stamp()
                if fields.get("status") == "open":
                    t["done_at"] = ""
                t["updated"] = self.stamp()
                self._save(data, f"task #{task_id} {fields.get('status', 'updated')}")
                return t
        return None

    def mark_reminded(self, task_id: int) -> None:
        with self.lock:
            data = self._load()
            for t in data["tasks"]:
                if t["id"] == task_id:
                    t["reminded"] = True
            self._save(data, f"task #{task_id} reminded")

    def due_reminders(self) -> list[dict]:
        now = self.now().strftime("%Y-%m-%d %H:%M")
        return [t for t in self.tasks()
                if t["status"] == "open" and t.get("remind_at") and not t.get("reminded")
                and t["remind_at"] <= now]

    # ---------- facts ----------
    def add_fact(self, fact: str) -> None:
        with self.lock:
            old = self.facts_path.read_text(encoding="utf-8") if self.facts_path.exists() else "# Facts\n\n"
            self._write(self.facts_path, old.rstrip("\n") + f"\n- [{self.now():%Y-%m-%d}] {fact.strip()}\n")
        self._changed("fact added")

    def facts(self) -> str:
        return self.facts_path.read_text(encoding="utf-8") if self.facts_path.exists() else ""

    def replace_fact(self, old: str, new: str) -> bool:
        with self.lock:
            text = self.facts()
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if norm(old) in norm(line):
                    if new.strip():
                        lines[i] = f"- [{self.now():%Y-%m-%d}] {new.strip()}"
                    else:
                        del lines[i]
                    self._write(self.facts_path, "\n".join(lines) + "\n")
                    self._changed("fact updated")
                    return True
        return False

    # ---------- rules (standing orders Marin must follow) ----------
    def rules_list(self) -> list[str]:
        if not self.rules_path.exists():
            return []
        out = []
        for line in self.rules_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("- "):
                out.append(line[2:].strip())
        return out

    def _write_rules(self, rules: list[str]) -> None:
        body = "# Rules — Marin must follow these strictly\n\n" + "".join(f"- {r}\n" for r in rules)
        self._write(self.rules_path, body)
        self._changed("rules updated")

    def add_rule(self, rule: str) -> bool:
        rule = rule.strip()
        with self.lock:
            rules = self.rules_list()
            if any(norm(rule) == norm(r) for r in rules):
                return False
            rules.append(rule)
            self._write_rules(rules)
        return True

    def remove_rule(self, text: str) -> bool:
        with self.lock:
            rules = self.rules_list()
            kept = [r for r in rules if norm(text) not in norm(r)]
            if len(kept) == len(rules):
                return False
            self._write_rules(kept)
        return True

    # ---------- notes ----------
    def _note_path(self, note_id: str) -> Path:
        return self.root / "notes" / f"{Path(note_id).stem}.md"

    def save_note(self, title: str, content: str, tags: str = "") -> str:
        with self.lock:
            base = f"{self.now():%Y-%m-%d}-{slugify(title)}"
            note_id, i = base, 2
            while self._note_path(note_id).exists():
                note_id, i = f"{base}-{i}", i + 1
            self._write(self._note_path(note_id),
                        f"# {title.strip()}\n\ntags: {tags}\ncreated: {self.stamp()}\n"
                        f"updated: {self.stamp()}\n\n{content.strip()}\n")
        self._changed(f"note {note_id}")
        return note_id

    def update_note(self, note_id: str, content: str, append: bool = True) -> bool:
        p = self._note_path(note_id)
        with self.lock:
            if not p.exists():
                return False
            text = p.read_text(encoding="utf-8")
            text = re.sub(r"^updated: .*$", f"updated: {self.stamp()}", text, count=1, flags=re.M)
            if append:
                text = text.rstrip("\n") + f"\n\n**{self.stamp()}**\n{content.strip()}\n"
            else:
                head = text.split("\n\n", 2)
                text = "\n\n".join(head[:2]) + f"\n\n{content.strip()}\n"
            self._write(p, text)
        self._changed(f"note {note_id} updated")
        return True

    def read_note(self, note_id: str) -> str | None:
        p = self._note_path(note_id)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def notes_index(self, limit: int = 60) -> list[str]:
        out = []
        for p in sorted((self.root / "notes").glob("*.md"), reverse=True)[:limit]:
            first = p.read_text(encoding="utf-8").split("\n", 1)[0].lstrip("# ").strip()
            out.append(f"{p.stem}: {first}")
        return out

    # ---------- files ----------
    def save_file(self, data: bytes, name: str) -> str:
        now = self.now()
        safe = re.sub(r"[^\w.\-]", "_", name)[-80:]
        path = self.root / "files" / f"{now:%Y-%m}" / f"{now:%Y%m%d-%H%M%S}-{safe}"
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self._changed(f"file {safe}")
        return path.relative_to(self.root).as_posix()

    # ---------- search ----------
    def search(self, query: str, limit: int = 15) -> list[str]:
        terms = [t for t in re.split(r"\s+", norm(query)) if len(t) >= 2]
        if not terms:
            return []
        # match on word prefixes so "irodaga" still finds "iroda" and vice versa
        stems = [t[:max(3, len(t) - 3)] if len(t) > 4 else t for t in terms]

        def score(text: str) -> int:
            n = norm(text)
            return sum(1 for s in stems if s in n)

        hits: list[tuple[int, str, str]] = []
        for t in self.tasks():
            blob = " ".join(str(t.get(k, "")) for k in ("title", "person", "group", "details"))
            if (s := score(blob)):
                hits.append((s + 1, t.get("updated", ""), "TASK " + json.dumps(t, ensure_ascii=False)))
        for line in self.facts().splitlines():
            if (s := score(line)):
                hits.append((s + 1, line[3:13], "FACT " + line))
        for p in (self.root / "notes").glob("*.md"):
            txt = p.read_text(encoding="utf-8")
            if (s := score(txt)):
                hits.append((s, "", f"NOTE {p.stem}: {txt[:500]}"))
        for e in self.journal_entries():
            if (s := score(e)):
                hits.append((s, e[3:19], "JOURNAL " + e[:500]))
        hits.sort(key=lambda h: (h[0], h[1]), reverse=True)
        return [h[2] for h in hits[:limit]]

    # ---------- views ----------
    def context_tasks(self, done_days: int = 30) -> list[dict]:
        cutoff = (self.now() - timedelta(days=done_days)).strftime("%Y-%m-%d")
        tasks = self.tasks()
        # done tasks stay visible while their group still has open items
        open_groups = {norm(t["group"]) for t in tasks if t["status"] == "open" and t.get("group")}
        keep = []
        for t in tasks:
            if t["status"] == "open" or (t["status"] == "done" and (
                    t.get("done_at", "") >= cutoff or norm(t.get("group", "")) in open_groups)):
                keep.append({k: v for k, v in t.items() if v not in ("", False, None) and k != "updated"})
        return keep
