from __future__ import annotations

import re
import textwrap
from collections.abc import Callable

from .blocks import BLOCKS, BY_ID
from .notes import NoteDocument

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


def _heading(block_id: str) -> str:
    block = BY_ID[block_id]
    return f"*{escape(block.icon)} {escape(block.title)}*"


def _bullets(items: list[str], marker: str) -> list[str]:
    return [f"{marker} {inline(piece)}" for item in items for piece in _pieces(item)]


def _numbered(items: list[str]) -> list[str]:
    units: list[str] = []
    for number, item in enumerate(items, start=1):
        for index, piece in enumerate(_pieces(item)):
            units.append((f"{number}\\. " if index == 0 else "    ") + inline(piece))
    return units


def _main_idea(doc: NoteDocument) -> list[str]:
    return [_quote([_heading("main_idea"), inline(piece)]) for piece in _pieces(doc.main_idea)]


def _context(doc: NoteDocument) -> list[str]:
    return [inline(piece) for paragraph in doc.context for piece in _pieces(paragraph)]


def _concepts(doc: NoteDocument) -> list[str]:
    units: list[str] = []
    for concept in doc.concepts:
        head, *rest = _pieces(concept.definition)
        lines = [f"▸ *{escape(concept.term)}* — {inline(head)}"]
        lines.extend(inline(piece) for piece in rest)
        if concept.why.strip():
            lines.append(f"_Зачем:_ {inline(concept.why)}")
        units.append("\n".join(lines))
    return units


def _process(doc: NoteDocument) -> list[str]:
    if doc.process is None:
        return []
    intro = [inline(piece) for piece in _pieces(doc.process.intro)]
    return intro + _numbered(doc.process.steps)


def _differences(doc: NoteDocument) -> list[str]:
    if doc.differences is None:
        return []
    headers, rows = doc.differences.aligned()
    units: list[str] = []
    for title, *values in rows:
        if not title.strip():
            continue
        lines = [f"▸ *{escape(title)}*"]
        for header, value in zip(headers[1:], values):
            if value.strip():
                label = f"_{escape(header)}:_ " if header.strip() else ""
                lines.append(f"  • {label}{inline(value)}")
        units.append("\n".join(lines))
    return units


def _examples(doc: NoteDocument) -> list[str]:
    units: list[str] = []
    for example in doc.examples:
        head = f"*{escape(example.title)}*\n" if example.title.strip() else ""
        for index, piece in enumerate(_pieces(example.text)):
            units.append((head if index == 0 else "") + inline(piece))
    return units


# Short list-like blocks are joined with single newlines; the rest get a blank line between items.
_COMPACT = {"process", "warnings", "practice", "takeaways", "questions"}

_RENDERERS: dict[str, Callable[[NoteDocument], list[str]]] = {
    "main_idea": _main_idea,
    "context": _context,
    "concepts": _concepts,
    "process": _process,
    "differences": _differences,
    "examples": _examples,
    "warnings": lambda doc: [_quote([f"⚠️ {inline(piece)}"]) for item in doc.warnings for piece in _pieces(item)],
    "practice": lambda doc: _bullets(doc.practice, "→"),
    "takeaways": lambda doc: _bullets(doc.takeaways, "✦"),
    "questions": lambda doc: _numbered(doc.questions),
}


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


def render_messages(doc: NoteDocument, limit: int = TG_LIMIT) -> list[str]:
    """Render the note as MarkdownV2 messages, each within Telegram's length limit."""
    units: list[tuple[str, str]] = [("", f"{escape(doc.icon)} *{escape(doc.title)}*")]
    for block in BLOCKS:
        if not doc.has_block(block.id):
            continue
        items = _RENDERERS[block.id](doc)
        if not items:
            continue
        if block.id != "main_idea":
            items[0] = f"{_heading(block.id)}\n{items[0]}"
        for index, item in enumerate(items):
            compact = block.id in _COMPACT and index > 0
            units.append(("\n" if compact else "\n\n", item))

    messages = _pack(units, limit - _PART_RESERVE)
    if len(messages) == 1:
        return messages
    return [f"_\\({index}/{len(messages)}\\)_\n{message}" for index, message in enumerate(messages, start=1)]


def plain_text(markdown: str) -> str:
    """Fallback for a message Telegram refused: drop the markup and keep the words."""
    text = re.sub(r"(?m)^>", "", markdown)
    text = re.sub(r"(?<!\\)[*_`]", "", text)
    return re.sub(r"\\(.)", r"\1", text)
