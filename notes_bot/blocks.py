from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Length:
    id: str
    title: str
    guidance: str
    # The note should take this share of the source, in characters, from low to high; (0, 0) means no target.
    # The ranges do not overlap, so a longer length always gives a longer note. The model writes about a third
    # more than asked (checked on a 53k-character lecture), so the ranges are set that much lower than the aim.
    low: float = 0.0
    high: float = 0.0


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
        "Сжато: только главные темы лекции, определения самых важных терминов и выводы. 3–6 разделов, "
        "без примеров, кроме одного-двух ключевых.",
        0.02, 0.04,
    ),
    Length(
        "medium",
        "Средний",
        "Сбалансированно: все основные темы, ключевые определения, важные примеры одной фразой и выводы.",
        0.07, 0.11,
    ),
    Length(
        "long",
        "Подробный",
        "Полно: все темы и подтемы лекции, определения, имена, даты, цифры, задания и требования. "
        "Кейсы и примеры — коротко, в 1–3 предложения: что произошло и какой вывод. Повторы и второстепенные детали опускай.",
        0.13, 0.2,
    ),
    Length(
        "max",
        "Максимально подробный",
        "Почти всё содержательное: каждое определение, пункт перечня, цифра и имя, каждый кейс и пример "
        "со всеми деталями, которые назвал лектор, его рассуждения и оговорки. Опускай только шутки, "
        "повторы и организационный шум.",
        0.24, 0.34,
    ),
    Length(
        "full",
        "Без сокращений",
        "Материал уже конспект или запись, который нельзя сокращать. Не выбрасывай ни одного факта, определения, "
        "примера, цифры или пункта, сохраняй формулировки. Задача — привести текст в структуру конспекта: разделы, "
        "таблицы, перечни, выноски, и исправить явные опечатки и сокращения («Офиц» → «Официально-деловой», «Пис» → «Письменная»). "
        "Убирай только то, что не несёт смысла: слова-паразиты, повторы, посторонние разговоры.",
        0.55, 0.9,
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
