from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Length:
    id: str
    title: str
    guidance: str
    # The note must be at least this share of the source, in characters; 0 means no lower bound.
    share: float = 0.0


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
        0.2,
    ),
    Length(
        "max",
        "Максимально подробный",
        "Почти весь содержательный материал: каждое определение, пример, цифра, имя, пункт перечня и пояснение, "
        "объяснения в 3–5 предложений. Не сокращай ради краткости: если материала много, делай больше разделов. "
        "Ориентир — 40–60% полезного содержания исходника.",
        0.4,
    ),
    Length(
        "full",
        "Без сокращений",
        "Материал уже конспект или запись, который нельзя сокращать. Не выбрасывай ни одного факта, определения, "
        "примера, цифры или пункта, сохраняй формулировки. Задача — привести текст в структуру конспекта: разделы, "
        "таблицы, перечни, выноски, и исправить явные опечатки и сокращения («Офиц» → «Официально-деловой», «Пис» → «Письменная»). "
        "Ориентир — 90–100% исходника.",
        0.8,
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

# Used after a long note was written piece by piece: one title and main idea for the whole lecture,
# and the closing sections the user asked for, made from the finished note.
CLOSING_LENGTH = Length(
    "closing",
    "Итог",
    "Материал — готовый конспект одной лекции, записанный по частям. Не переписывай его. Верни title и icon "
    "для всей лекции и main_idea в 1–3 предложения по всей лекции. В sections положи только дополнительные разделы, "
    "о которых просят ниже; если о них не просят, sections — пустой список.",
)
CLOSING_LENGTH_ID = CLOSING_LENGTH.id

LENGTH_BY_ID: dict[str, Length] = {length.id: length for length in (*LENGTHS, ONE_MESSAGE_LENGTH, CLOSING_LENGTH)}
DEFAULT_LENGTH = "long"
