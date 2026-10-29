import re

import pytest

from notes_bot.notes import NoteDocument
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


def test_render_has_title_main_idea_and_numbered_section_headings(sample_doc):
    messages = render_messages(sample_doc)
    text = "\n".join(messages)
    assert text.startswith("📚 *Реклама: определения*")
    assert "Главная идея" in text
    for heading in ["1\\. Определения", "2\\. Порядок проверки рекламы", "4\\. Что запомнить"]:
        assert heading in text
    assert "☐ Недостоверность" in text and "1\\. Определить объект" in text


def test_short_note_fits_one_message(sample_doc):
    assert len(render_messages(sample_doc)) == 1


def test_markers_are_balanced_in_every_message(sample_doc):
    for message in render_messages(sample_doc):
        assert unescaped_count(message, "*") % 2 == 0, message
        assert unescaped_count(message, "_") % 2 == 0, message


def _one(elements, title="Длинный"):
    return NoteDocument(title=title, sections=[{"heading": "Раздел", "elements": elements}])


def test_long_note_is_split_under_the_limit_with_part_markers():
    long_text = " ".join(f"Предложение номер {i} о механизме." for i in range(600))
    messages = render_messages(_one([{"type": "bullets", "items": [long_text, long_text, long_text]}]))
    assert len(messages) > 1
    assert all(len(message) <= TG_LIMIT for message in messages)
    assert messages[0].startswith("_\\(1/")
    assert messages[-1].startswith(f"_\\({len(messages)}/{len(messages)}\\)_")


def test_very_long_single_paragraph_is_cut_into_pieces_under_the_limit():
    messages = render_messages(_one([{"type": "paragraph", "text": "слово " * 3000}], "Один абзац"))
    assert len(messages) > 1
    assert all(len(message) <= TG_LIMIT for message in messages)


def test_table_rows_render_as_lines_with_column_labels():
    text = "\n".join(render_messages(_one([{
        "type": "table", "headers": ["Понятие", "Определение", "Зачем"],
        "rows": [["Термин", "Определение.", "Почему"], ["Другой", "Только определение.", ""]],
    }])))
    assert "▸ *Термин*" in text and "_Определение:_ Определение\\." in text and "_Зачем:_ Почему" in text
    assert "▸ *Другой* — Только определение\\." in text


def test_callouts_toggles_and_subheadings_have_their_own_look():
    text = "\n".join(render_messages(_one([
        {"type": "subheading", "text": "Подраздел"},
        {"type": "callout", "kind": "warning", "text": "Осторожно"},
        {"type": "callout", "kind": "quote", "text": "Формула"},
        {"type": "toggle", "title": "Кейс", "text": "Разбор."},
    ])))
    assert "*Подраздел*" in text and ">⚠️ Осторожно" in text and ">Формула" in text
    assert "*Кейс*\nРазбор\\." in text


def test_plain_text_fallback_strips_markup_but_keeps_words():
    markdown = "📚 *Заголовок*\n>⚠️ Важно\\. Цена 1\\-2"
    assert plain_text(markdown) == "📚 Заголовок\n⚠️ Важно. Цена 1-2"


def test_premium_headings_use_custom_emoji_and_plain_fallback_drops_them(sample_doc):
    messages = render_messages(sample_doc, premium=True)
    joined = "\n".join(messages)
    assert "![🧠](tg://emoji?id=5864019342873598613)" in joined and "tg://emoji?id=6030563507299160824" in joined
    assert "tg://emoji" not in plain_text(joined)
    assert "tg://emoji" not in "\n".join(render_messages(sample_doc))
