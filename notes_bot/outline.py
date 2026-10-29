"""Splitting a long text into topics. The model only marks where each topic starts; the text itself is cut here,
so nothing is retyped or lost. If the model's answer is unusable, the text is cut into equal parts instead."""
from __future__ import annotations

from dataclasses import dataclass

BLOCK_CHARS = 4000  # the text is numbered in blocks of about this size for the model to point at
SPLIT_CHARS = 150_000  # longer texts are split into topics, shorter ones make one note
OUTLINE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"title": {"type": "string"}, "from": {"type": "integer"}},
        "required": ["title", "from"],
    },
}
OUTLINE_PROMPT = (
    "Ты делишь учебный материал на темы. Текст разбит на блоки, каждый начинается строкой «[блок N]». "
    "Определи, где начинается каждая тема. Верни список тем по порядку: название темы и номер блока, с которого она начинается. "
    "Первая тема начинается с блока 0. Названия короткие, по сути темы. Ничего не пересказывай."
)


@dataclass(frozen=True)
class Part:
    title: str
    text: str


def blocks_of(text: str, size: int = BLOCK_CHARS) -> list[str]:
    """Paragraph-aligned blocks of about `size` characters; a block never splits a paragraph."""
    blocks: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        if current and len(current) + len(paragraph) > size:
            blocks.append(current)
            current = ""
        current = f"{current}\n{paragraph}" if current else paragraph
    if current.strip():
        blocks.append(current)
    return blocks


def marked(blocks: list[str]) -> str:
    return "\n\n".join(f"[блок {number}]\n{block}" for number, block in enumerate(blocks))


def plan(raw: list[dict], blocks: list[str], limit: int = SPLIT_CHARS) -> list[Part]:
    """Turn the model's starts into parts. Anything unusable falls back to equal parts."""
    starts: dict[int, str] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        number, title = item.get("from"), str(item.get("title") or "").strip()
        if isinstance(number, int) and 0 <= number < len(blocks) and title:
            starts.setdefault(number, title)
    if not starts:
        return fixed(blocks, limit)
    if 0 not in starts:
        starts[0] = starts[min(starts)]  # the text before the first marked topic belongs to it
    ordered = sorted(starts)
    parts: list[Part] = []
    for position, start in enumerate(ordered):
        end = ordered[position + 1] if position + 1 < len(ordered) else len(blocks)
        text = "\n\n".join(blocks[start:end]).strip()
        if not text:
            continue
        if len(text) > limit:
            parts.extend(Part(f"{starts[start]}, часть {number}", piece)
                         for number, piece in enumerate(_chunks(blocks[start:end], limit), start=1))
        else:
            parts.append(Part(starts[start], text))
    return parts or fixed(blocks, limit)


def fixed(blocks: list[str], limit: int = SPLIT_CHARS) -> list[Part]:
    return [Part(f"Часть {number}", piece) for number, piece in enumerate(_chunks(blocks, limit), start=1)]


def _chunks(blocks: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        if current and size + len(block) > limit:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(block)
        size += len(block)
    if current:
        chunks.append("\n\n".join(current))
    return chunks
