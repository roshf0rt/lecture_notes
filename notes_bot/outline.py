"""Splitting a long text into topics. The model only marks where each topic starts; the text itself is cut here,
so nothing is retyped or lost. If the model's answer is unusable, the text is cut into equal parts instead."""
from __future__ import annotations

from dataclasses import dataclass

# A topic shorter than this is not worth a note of its own: it joins the topic before it.
MIN_PART_CHARS = 2000
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
    "Ты делишь учебный материал на темы. Каждый абзац начинается с номера в квадратных скобках, например [12]. "
    "Определи, с какого абзаца начинается каждая тема. Верни список тем по порядку: название темы и номер абзаца, "
    "с которого она начинается. Первая тема начинается с абзаца 0. Тема — это крупный раздел, а не отдельный пример или вопрос. "
    "Названия короткие, по сути темы. Ничего не пересказывай."
)


@dataclass(frozen=True)
class Part:
    title: str
    text: str


def paragraphs_of(text: str) -> list[str]:
    """Every non-empty line is a paragraph, and the model points at paragraphs, so a topic can start anywhere."""
    return [line.strip() for line in text.split("\n") if line.strip()]


def marked(paragraphs: list[str]) -> str:
    return "\n".join(f"[{number}] {paragraph}" for number, paragraph in enumerate(paragraphs))


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
    topics = [
        Part(starts[start], "\n\n".join(blocks[start:end]).strip())
        for start, end in zip(ordered, ordered[1:] + [len(blocks)])
    ]
    parts: list[Part] = []
    for topic in _merge_small([topic for topic in topics if topic.text]):
        if len(topic.text) > limit:
            parts.extend(Part(f"{topic.title}, часть {number}", piece)
                         for number, piece in enumerate(_chunks(topic.text.split("\n\n"), limit), start=1))
        else:
            parts.append(topic)
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


def _merge_small(parts: list[Part]) -> list[Part]:
    """Join topics too short for a note of their own to the topic before them (the first one to the next)."""
    merged: list[Part] = []
    for part in parts:
        if merged and (len(part.text) < MIN_PART_CHARS or len(merged[-1].text) < MIN_PART_CHARS):
            merged[-1] = Part(merged[-1].title, f"{merged[-1].text}\n\n{part.text}")
        else:
            merged.append(part)
    return merged
