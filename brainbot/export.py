"""Write long answers to .md / .txt / .pdf files."""
from __future__ import annotations

import re
from pathlib import Path

from .storage import slugify
from .tgformat import strip_md

FONT_CANDIDATES = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
]


def _font() -> tuple[str, str] | None:
    for reg, bold in FONT_CANDIDATES:
        if Path(reg).exists():
            return reg, bold if Path(bold).exists() else reg
    return None


def _pdf(path: Path, title: str, content: str) -> bool:
    font = _font()
    if not font:
        return False
    from fpdf import FPDF

    NL = {"new_x": "LMARGIN", "new_y": "NEXT"}
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_font("Body", "", font[0])
    pdf.add_font("Body", "B", font[1])
    pdf.add_page()
    pdf.set_font("Body", "B", 16)
    pdf.multi_cell(0, 9, title, **NL)
    pdf.ln(2)
    no_emoji = re.compile(r"[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F]")
    for raw in content.splitlines():
        line = no_emoji.sub("", raw).rstrip()
        if m := re.match(r"^(#{1,6})\s+(.*)", line):
            pdf.ln(2)
            pdf.set_font("Body", "B", max(11, 16 - 2 * len(m.group(1))))
            pdf.multi_cell(0, 7, strip_md(m.group(2)), **NL)
            continue
        pdf.set_font("Body", "", 11)
        if m := re.match(r"^(\s*)[-*+]\s+(.*)", line):
            line = "    " * (len(m.group(1)) // 2) + "• " + m.group(2)
        pdf.multi_cell(0, 6, strip_md(line) or " ", **NL)
    pdf.output(str(path))
    return True


def write_file(out_dir: Path, title: str, content: str, fmt: str = "md") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = (fmt or "md").lower().strip(".")
    base = out_dir / slugify(title)
    if fmt == "pdf":
        p = base.with_suffix(".pdf")
        try:
            if _pdf(p, title, content):
                return p
        except Exception:
            pass
        fmt = "md"  # no usable font → fall back
    if fmt == "txt":
        p = base.with_suffix(".txt")
        p.write_text(f"{title}\n{'=' * len(title)}\n\n{strip_md(content)}\n", encoding="utf-8")
        return p
    p = base.with_suffix(".md")
    p.write_text(f"# {title}\n\n{content}\n", encoding="utf-8")
    return p
