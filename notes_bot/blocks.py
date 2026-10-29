from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Block:
    id: str
    title: str
    icon: str
    tone: str


# Order here is the order in the note. `tone` is a colour role from themes.py.
BLOCKS: tuple[Block, ...] = (
    Block("main_idea", "Главная идея лекции", "🧠", "blue"),
    Block("context", "Базовая логика / контекст", "🧭", "blue"),
    Block("concepts", "Ключевые понятия", "📘", "purple"),
    Block("process", "Модель / процесс", "⚙️", "orange"),
    Block("differences", "Ключевые различия", "🔀", "purple"),
    Block("examples", "Примеры и кейсы", "💡", "green"),
    Block("warnings", "Важно и ловушки", "⚠️", "yellow"),
    Block("practice", "Практическое применение", "🛠️", "green"),
    Block("takeaways", "Что запомнить", "✅", "ink"),
    Block("questions", "Вопросы для самопроверки", "❓", "gray"),
)

BY_ID: dict[str, Block] = {block.id: block for block in BLOCKS}

DEFAULT_BLOCKS: tuple[str, ...] = (
    "main_idea",
    "context",
    "concepts",
    "process",
    "differences",
    "warnings",
    "takeaways",
    "questions",
)


@dataclass(frozen=True)
class Length:
    id: str
    title: str
    guidance: str


LENGTHS: tuple[Length, ...] = (
    Length(
        "short",
        "Короткий",
        "Очень сжато: 3–4 ключевых понятия, 3–4 пункта в «Что запомнить», не больше одного примера. "
        "Ориентир — около 5% полезного содержания исходника.",
    ),
    Length(
        "medium",
        "Средний",
        "Сбалансированно: 3–7 понятий, 4–6 пунктов в «Что запомнить», до двух примеров. "
        "Ориентир — 8–15% полезного содержания исходника.",
    ),
    Length(
        "long",
        "Подробный",
        "Развёрнуто: до 10 понятий, до трёх примеров, пояснения в 2–4 предложения. "
        "Ориентир — 15–25% полезного содержания исходника.",
    ),
)

# Used only for the "one short message" output: it has to fit into one Telegram message.
ONE_MESSAGE_LENGTH = Length(
    "onepage",
    "Одно сообщение",
    "Очень коротко, чтобы конспект целиком поместился в одно сообщение Telegram, не больше 3000 знаков вместе с оформлением. "
    "Оставь только главную идею, самые важные понятия и выводы, по одной-две строки на пункт.",
)
ONE_MESSAGE_LENGTH_ID = ONE_MESSAGE_LENGTH.id

LENGTH_BY_ID: dict[str, Length] = {length.id: length for length in (*LENGTHS, ONE_MESSAGE_LENGTH)}
DEFAULT_LENGTH = "medium"
