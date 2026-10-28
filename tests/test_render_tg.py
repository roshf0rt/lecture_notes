import re

import pytest

from notes_bot.notes import Concept, NoteDocument
from notes_bot.render_tg import TG_LIMIT, escape, inline, plain_text, render_messages

UNESCAPED = r"(?<!\\)"


def unescaped_count(text: str, char: str) -> int:
    return len(re.findall(UNESCAPED + re.escape(char), text))


@pytest.mark.parametrize("char", list("_*[]()~`>#+-=|{}.!"))
def test_escape_covers_every_markdownv2_special(char):
    assert escape(char) == "\\" + char


def test_inline_converts_bold_and_code_and_escapes_the_rest():
    assert inline("Цена **1.5** и `x.y`!") == "Цена *1\\.5* и `x.y`\\!"


def test_inline_leaves_unmatched_markers_as_literal_text():
    assert inline("одна ** звёздочка") == "одна \\*\\* звёздочка"


def test_render_has_title_and_every_enabled_block(sample_doc):
    messages = render_messages(sample_doc)
    text = "\n".join(messages)
    assert text.startswith("📚 *Реклама: определения*")
    for heading in ["Ключевые понятия", "Модель / процесс", "Ключевые различия", "Что запомнить"]:
        assert heading in text
    assert "Главная идея лекции" in text


def test_short_note_fits_one_message(sample_doc):
    assert len(render_messages(sample_doc)) == 1


def test_markers_are_balanced_in_every_message(sample_doc):
    for message in render_messages(sample_doc):
        assert unescaped_count(message, "*") % 2 == 0, message
        assert unescaped_count(message, "_") % 2 == 0, message


def test_long_note_is_split_under_the_limit_with_part_markers():
    long_text = " ".join(f"Предложение номер {i} о механизме." for i in range(600))
    doc = NoteDocument(title="Длинный", takeaways=[long_text, long_text, long_text])
    messages = render_messages(doc)
    assert len(messages) > 1
    assert all(len(message) <= TG_LIMIT for message in messages)
    assert messages[0].startswith("_\\(1/")
    assert messages[-1].startswith(f"_\\({len(messages)}/{len(messages)}\\)_")


def test_very_long_single_paragraph_is_cut_into_pieces_under_the_limit():
    doc = NoteDocument(title="Один абзац", context=["слово " * 3000])
    messages = render_messages(doc)
    assert len(messages) > 1
    assert all(len(message) <= TG_LIMIT for message in messages)


def test_concept_with_why_renders_definition_and_reason():
    doc = NoteDocument(title="T", concepts=[Concept(term="Термин", definition="Определение.", why="Почему")])
    text = "\n".join(render_messages(doc))
    assert "▸ *Термин* — Определение\\." in text
    assert "_Зачем:_ Почему" in text


def test_plain_text_fallback_strips_markup_but_keeps_words():
    markdown = "📚 *Заголовок*\n>⚠️ Важно\\. Цена 1\\-2"
    assert plain_text(markdown) == "📚 Заголовок\n⚠️ Важно. Цена 1-2"


def test_premium_headings_use_custom_emoji_and_plain_fallback_drops_them(sample_doc):
    messages = render_messages(sample_doc, premium=True)
    joined = "\n".join(messages)
    assert "![🧠](tg://emoji?id=5864019342873598613)" in joined
    assert "tg://emoji" not in plain_text(joined)
    assert "tg://emoji" not in "\n".join(render_messages(sample_doc))
