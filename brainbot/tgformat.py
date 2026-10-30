"""Convert the model's Markdown into Telegram-safe HTML."""
import html
import re

_INLINE = [
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"<b>\1</b>"),
    (re.compile(r"__(.+?)__", re.S), r"<u>\1</u>"),
    (re.compile(r"~~(.+?)~~", re.S), r"<s>\1</s>"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), r"<i>\1</i>"),
    (re.compile(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])"), r"<i>\1</i>"),
    (re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)"), r'<a href="\2">\1</a>'),
]


def _inline(text: str) -> str:
    # protect `code` spans from other formatting
    codes: list[str] = []

    def stash(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", stash, text)
    for rx, rep in _INLINE:
        text = rx.sub(rep, text)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m.group(1))]}</code>", text)


def md_to_html(md: str) -> str:
    out: list[str] = []
    quote: list[str] = []
    parts = re.split(r"```[\w-]*\n?(.*?)```", md, flags=re.S)
    for i, part in enumerate(parts):
        if i % 2:  # fenced code block
            out.append(f"<pre>{html.escape(part.rstrip())}</pre>")
            continue
        for line in html.escape(part, quote=False).split("\n"):
            if line.startswith("&gt;"):
                quote.append(_inline(line[4:].lstrip()))
                continue
            if quote:
                out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
                quote = []
            if m := re.match(r"^#{1,6}\s+(.*)", line):
                line = f"<b>{_inline(m.group(1))}</b>"
            elif m := re.match(r"^(\s*)[-*+]\s+(.*)", line):
                line = f"{m.group(1)}• {_inline(m.group(2))}"
            elif re.match(r"^\s*([-*_]\s*){3,}$", line):
                line = "──────────"
            else:
                line = _inline(line)
            out.append(line)
        if quote:
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote = []
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def strip_md(md: str) -> str:
    return re.sub(r"(\*\*|__|~~|`)", "", md)
