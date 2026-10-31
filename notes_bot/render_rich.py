"""The note as Telegram rich messages (Bot API 10.1+): HTML with headings, lists, quotes and collapsible blocks.

One rich message holds up to 32768 characters and 500 blocks, so a long note is cut into several messages
at section and element boundaries. Tables have no rich syntax in the examples of the Bot API, so they become lists.
"""
from __future__ import annotations

import html
import re

from .notes import BULLETS, CALLOUT, CHECKLIST, NUMBERED, SUBHEADING, TABLE, TOGGLE, Element, NoteDocument

# Telegram's limits are 32768 characters and 500 blocks; the margins leave room for the wrappers.
CHARS_PER_MESSAGE = 30_000
BLOCKS_PER_MESSAGE = 450
# A long list is cut into several <ul>, so one list does not use up a whole message.
ITEMS_PER_LIST = 200
KIND_LABELS = {"info": "Пояснение.", "important": "Главное.", "warning": "Осторожно.", "tip": "Совет."}
_BOLD = re.compile(r"\*\*(.+?)\*\*")


class RichTooLong(ValueError):
    """One piece of the note does not fit into one rich message even on its own."""


def _text(text: str) -> str:
    """Escaped text with **bold** turned into <b>."""
    parts = []
    last = 0
    for match in _BOLD.finditer(text):
        parts.append(html.escape(text[last:match.start()], quote=False))
        parts.append(f"<b>{html.escape(match.group(1), quote=False)}</b>")
        last = match.end()
    parts.append(html.escape(text[last:], quote=False))
    return "".join(parts)


def _lists(tag: str, items: list[str], attributes: str = "") -> list[tuple[str, int]]:
    """Items as one or more lists; the block count includes each item."""
    pieces = []
    for start in range(0, len(items), ITEMS_PER_LIST):
        group = items[start:start + ITEMS_PER_LIST]
        pieces.append((f"<{tag}{attributes}>" + "".join(f"<li>{item}</li>" for item in group) + f"</{tag}>",
                       len(group) + 1))
    return pieces


def _table_items(element: Element) -> list[str]:
    headers, rows = element.aligned()
    items = []
    for row in rows:
        label, *cells = row
        details = "; ".join(f"{_text(header)}: {_text(cell)}" for header, cell in zip(headers[1:], cells) if cell)
        items.append(f"<b>{_text(label)}</b>" + (f": {details}" if details else ""))
    return items


def _element(element: Element) -> list[tuple[str, int]]:
    """The element as rich HTML pieces, each with its number of blocks."""
    if element.type == NUMBERED:
        return _lists("ol", [_text(item) for item in element.items])
    if element.type == BULLETS:
        return _lists("ul", [_text(item) for item in element.items])
    if element.type == CHECKLIST:
        return _lists("ul", [f'<input type="checkbox">{_text(item)}' for item in element.items])
    if element.type == TABLE:
        return _lists("ul", _table_items(element))
    if element.type == CALLOUT:
        if element.kind == "quote":
            return [(f"<aside>{_text(element.text)}</aside>", 1)]
        label = KIND_LABELS.get(element.kind, "")
        return [(f"<blockquote><b>{label}</b> {_text(element.text)}</blockquote>", 1)]
    if element.type == TOGGLE:
        title = _text(element.title or "Подробнее")
        return [(f"<details><summary>{title}</summary><p>{_text(element.text)}</p></details>", 2)]
    if element.type == SUBHEADING:
        return [(f"<h3>{_text(element.text)}</h3>", 1)]
    return [(f"<p>{_text(element.text)}</p>", 1)]


def _chunks(doc: NoteDocument) -> list[tuple[str, int]]:
    chunks = [(f"<h1>{html.escape(doc.icon, quote=False)} {_text(doc.title)}</h1>", 1)]
    if doc.main_idea:
        chunks.append((f"<blockquote><b>Главная идея.</b> {_text(doc.main_idea)}</blockquote>", 1))
    for number, section in enumerate(doc.sections, start=1):
        icon = f"{html.escape(section.icon, quote=False)} " if section.icon else ""
        heading = f"<h2>{icon}{number}. {_text(section.heading)}</h2>"
        first = True
        for element in section.elements:
            for piece, blocks in _element(element):
                if first:
                    piece, blocks = heading + piece, blocks + 1
                    first = False
                chunks.append((piece, blocks))
        if first:
            chunks.append((heading, 1))
    return chunks


def rich_messages(doc: NoteDocument) -> list[str]:
    """The note as HTML for sendRichMessage, one string per message.

    Raises RichTooLong if a single piece of the note is too big for any message.
    """
    messages: list[str] = []
    current: list[str] = []
    size = blocks = 0
    for piece, count in _chunks(doc):
        if len(piece) > CHARS_PER_MESSAGE or count > BLOCKS_PER_MESSAGE:
            raise RichTooLong(piece[:80])
        if current and (size + len(piece) > CHARS_PER_MESSAGE or blocks + count > BLOCKS_PER_MESSAGE):
            messages.append("".join(current))
            current, size, blocks = [], 0, 0
        current.append(piece)
        size += len(piece)
        blocks += count
    if current:
        messages.append("".join(current))
    return messages
