from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Length:
    id: str
    title: str
    guidance: str


@dataclass(frozen=True)
class Extra:
    """A closing section the user may ask for. The model adds it at the end of the note."""

    id: str
    title: str
    instruction: str


EXTRAS: tuple[Extra, ...] = (
    Extra("remember", "Что запомнить",
          "раздел «Что запомнить»: элемент checklist на 5–10 самых важных мыслей, каждая одной фразой."),
    Extra("questions", "Вопросы для самопроверки",
          "раздел «Вопросы для самопроверки»: элемент numbered на 5–10 вопросов по материалу, "
          "от определений до применения. Ответы не пиши."),
    Extra("terms", "Термины и определения",
          "раздел «Термины и определения»: элемент table с колонками «Термин» и «Определение», "
          "все термины лекции в порядке появления, определение по лекции в одну-две фразы."),
)
EXTRA_BY_ID: dict[str, Extra] = {extra.id: extra for extra in EXTRAS}

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
    Length(
        "max",
        "Максимально подробный",
        "Почти весь содержательный материал: каждое определение, пример, цифра, имя, пункт перечня и пояснение, "
        "объяснения в 3–5 предложений. Не сокращай ради краткости: если материала много, делай больше разделов. "
        "Ориентир — 40–60% полезного содержания исходника.",
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
