from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Length:
    id: str
    title: str
    guidance: str


LENGTHS: tuple[Length, ...] = (
    Length(
        "short",
        "Короткий",
        "Сжато: только главные темы лекции, определения самых важных терминов и выводы. "
        "Ориентир — около 5–8% полезного содержания исходника, 3–6 разделов.",
    ),
    Length(
        "medium",
        "Средний",
        "Сбалансированно: все основные темы, ключевые определения, важные примеры и выводы. "
        "Ориентир — 12–20% полезного содержания исходника.",
    ),
    Length(
        "long",
        "Подробный",
        "Максимально полно: все темы и подтемы лекции, определения, имена, даты, цифры, кейсы и примеры, "
        "рассуждения лектора, задания и требования. Ничего содержательного не пропускай. "
        "Ориентир — 25–40% полезного содержания исходника.",
    ),
)

# Used only for the "one short message" output: it has to fit into one Telegram message.
ONE_MESSAGE_LENGTH = Length(
    "onepage",
    "Одно сообщение",
    "Очень коротко, чтобы конспект целиком поместился в одно сообщение Telegram, не больше 3000 знаков вместе с оформлением. "
    "Главная идея и 3–5 коротких разделов, в основном bullets и одна-две выноски, без таблиц, по одной-две строки на пункт.",
)
ONE_MESSAGE_LENGTH_ID = ONE_MESSAGE_LENGTH.id

LENGTH_BY_ID: dict[str, Length] = {length.id: length for length in (*LENGTHS, ONE_MESSAGE_LENGTH)}
DEFAULT_LENGTH = "long"
