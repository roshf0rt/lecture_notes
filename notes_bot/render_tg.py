from __future__ import annotations

import re
import textwrap

from .notes import Element, NoteDocument
from .ui import markdown_block_icon

TG_LIMIT = 4096
_PART_RESERVE = 16
# Raw-text size of one unit. Escaping at most doubles the length, so a unit stays well under TG_LIMIT.
_PIECE = 800
_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
_INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def escape(text: str) -> str:
    return _SPECIAL.sub(r"\\\1", text)


def inline(text: str) -> str:
    """Convert **bold** and `code` to MarkdownV2 and escape everything else."""
    parts: list[str] = []
    last = 0
    for match in _INLINE.finditer(text):
        parts.append(escape(text[last : match.start()]))
        if match.group(1) is not None:
            parts.append(f"*{escape(match.group(1))}*")
        else:
            # Inside code spans only backslash must be escaped.
            parts.append("`" + match.group(2).replace("\\", "\\\\") + "`")
        last = match.end()
    parts.append(escape(text[last:]))
    return "".join(parts)


def _pieces(text: str, size: int = _PIECE) -> list[str]:
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE.split(text.strip()):
        for chunk in textwrap.wrap(sentence, size):
            if current and len(current) + len(chunk) + 1 > size:
                pieces.append(current)
                current = chunk
            else:
                current = f"{current} {chunk}".strip()
    if current:
        pieces.append(current)
    return pieces


def _quote(lines: list[str]) -> str:
    return "\n".join(f">{line}" for line in lines)


def _icon(key: str | None, premium: bool, fallback: str) -> str:
    """An icon for a MarkdownV2 message; the custom emoji of the bot owner when premium is on."""
    return markdown_block_icon(key, premium, escape(fallback)) if key else escape(fallback)


def _bullets(items: list[str], marker: str) -> list[str]:
    return [f"{marker} {inline(piece)}" for item in items for piece in _pieces(item)]


def _numbered(items: list[str]) -> list[str]:
    units: list[str] = []
    for number, item in enumerate(items, start=1):
        for index, piece in enumerate(_pieces(item)):
            units.append((f"{number}\\. " if index == 0 else "    ") + inline(piece))
    return units


def _table(element: Element) -> list[str]:
    headers, rows = element.aligned()
    units: list[str] = []
    for title, *values in rows:
        if not title.strip():
            continue
        filled = [(header, value) for header, value in zip(headers[1:], values) if value.strip()]
        if len(filled) == 1:
            units.append(f"▸ *{inline_plain(title)}* — {inline(filled[0][1])}")
            continue
        lines = [f"▸ *{inline_plain(title)}*"]
        for header, value in filled:
            label = f"_{escape(header)}:_ " if header.strip() else ""
            lines.append(f"  • {label}{inline(value)}")
        units.append("\n".join(lines))
    return units


def inline_plain(text: str) -> str:
    """Escape text for use inside *bold*: any ** markers in it are dropped."""
    return escape(text.replace("**", ""))


# kind -> (emoji, key of the owner's custom emoji)
_CALLOUTS = {
    "important": ("📌", None),
    "warning": ("⚠️", "warnings"),
    "tip": ("💡", "examples"),
    "info": ("ℹ️", None),
    "quote": ("", None),
}


def _element(element: Element, premium: bool) -> tuple[list[str], bool]:
    """Units of one element, and whether they sit close together (a list) or spaced apart."""
    kind = element.type
    if kind == "paragraph":
        return [inline(piece) for piece in _pieces(element.text)], False
    if kind == "bullets":
        return _bullets(element.items, "•"), True
    if kind == "numbered":
        return _numbered(element.items), True
    if kind == "checklist":
        return _bullets(element.items, "☐"), True
    if kind == "table":
        return _table(element), False
    if kind == "callout":
        emoji, key = _CALLOUTS[element.kind]
        icon = f"{_icon(key, premium, emoji)} " if emoji else ""
        return [_quote([icon + inline(piece)]) for piece in _pieces(element.text)], False
    if kind == "toggle":
        head = f"*{inline_plain(element.title)}*\n" if element.title else ""
        pieces = _pieces(element.text)
        return [(head if index == 0 else "") + inline(piece) for index, piece in enumerate(pieces)], False
    if kind == "subheading":
        return [f"*{inline_plain(element.text)}*"], False
    return [], False


def _pack(units: list[tuple[str, str]], limit: int) -> list[str]:
    """Greedily fill messages. Each unit carries the separator placed before it."""
    messages: list[str] = []
    current = ""
    for separator, unit in units:
        candidate = f"{current}{separator}{unit}" if current else unit
        if len(candidate) <= limit:
            current = candidate
        else:
            messages.append(current)
            current = unit
    if current:
        messages.append(current)
    return messages


def render_messages(doc: NoteDocument, limit: int = TG_LIMIT, premium: bool = False) -> list[str]:
    """Render the note as MarkdownV2 messages, each within Telegram's length limit.

    With premium on, some icons are custom emoji (the bot owner needs Telegram Premium).
    """
    units: list[tuple[str, str]] = [("", f"{escape(doc.icon)} *{escape(doc.title)}*")]
    if doc.main_idea:
        heading = f"*{_icon('main_idea', premium, '🧠')} Главная идея*"
        pieces = _pieces(doc.main_idea)
        units.append(("\n\n", _quote([heading])))
        units.extend(("\n", _quote([inline(piece)])) if index == 0 else ("\n\n", _quote([inline(piece)]))
                     for index, piece in enumerate(pieces))
    for number, section in enumerate(doc.sections, start=1):
        icon = f"{escape(section.icon)} " if section.icon else ""
        heading = f"*{icon}{number}\\. {inline_plain(section.heading)}*"
        pending: str | None = heading
        for element in section.elements:
            items, compact = _element(element, premium)
            for index, item in enumerate(items):
                if pending is not None:
                    units.append(("\n\n", f"{pending}\n{item}"))
                    pending = None
                    continue
                units.append(("\n" if compact and index > 0 else "\n\n", item))
        if pending is not None:
            units.append(("\n\n", pending))

    messages = _pack(units, limit - _PART_RESERVE)
    if len(messages) == 1:
        return messages
    return [f"_\\({index}/{len(messages)}\\)_\n{message}" for index, message in enumerate(messages, start=1)]


def fit_one_message(doc: NoteDocument, premium: bool = False) -> list[str]:
    """Render the note as a single message, dropping sections from the end until it fits."""
    candidate = doc
    messages = render_messages(candidate, premium=premium)
    while len(messages) > 1 and candidate.sections:
        candidate = candidate.model_copy(update={"sections": candidate.sections[:-1]})
        messages = render_messages(candidate, premium=premium)
    if len(messages) > 1:
        # Only the main idea is left and it is still too long: cut it.
        candidate = candidate.model_copy(update={"main_idea": candidate.main_idea[:400]})
        messages = render_messages(candidate, premium=premium)
    return messages[:1]


def plain_text(markdown: str) -> str:
    """Fallback for a message Telegram refused: drop the markup and keep the words."""
    text = re.sub(r"!\[([^\]]*)\]\(tg://emoji\?id=\d+\)", r"\1", markdown)
    text = re.sub(r"(?m)^>", "", text)
    text = re.sub(r"(?<!\\)[*_`]", "", text)
    return re.sub(r"\\(.)", r"\1", text)
