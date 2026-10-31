import pytest

from notes_bot import render_rich
from notes_bot.notes import NoteDocument
from notes_bot.render_rich import RichTooLong, rich_messages


def _doc(sections, main_idea="Главная идея лекции."):
    return NoteDocument(title="Репутация", icon="🛡️", main_idea=main_idea, sections=sections)


def test_headings_lists_quotes_and_collapsible_blocks_use_rich_html():
    doc = _doc([{"heading": "Кейсы", "icon": "📈", "elements": [
        {"type": "paragraph", "text": "Текст **важно** здесь."},
        {"type": "bullets", "items": ["первый", "второй"]},
        {"type": "numbered", "items": ["шаг"]},
        {"type": "checklist", "items": ["сделать"]},
        {"type": "callout", "kind": "warning", "text": "Оговорка."},
        {"type": "callout", "kind": "quote", "text": "Формула."},
        {"type": "toggle", "title": "Ревизорро", "text": "Разбор кейса."},
        {"type": "subheading", "text": "Подраздел"},
    ]}])
    (message,) = rich_messages(doc)
    assert message.startswith("<h1>🛡️ Репутация</h1><blockquote><b>Главная идея.</b> Главная идея лекции.</blockquote>")
    for fragment in ["<h2>📈 1. Кейсы</h2>", "<p>Текст <b>важно</b> здесь.</p>", "<ul><li>первый</li><li>второй</li></ul>",
                     "<ol><li>шаг</li></ol>", '<input type="checkbox">сделать', "<blockquote><b>Осторожно.</b> Оговорка.</blockquote>",
                     "<aside>Формула.</aside>", "<details><summary>Ревизорро</summary><p>Разбор кейса.</p></details>",
                     "<h3>Подраздел</h3>"]:
        assert fragment in message


def test_text_is_escaped_so_that_no_markup_leaks_into_the_message():
    doc = _doc([{"heading": "A < B & C", "elements": [{"type": "paragraph", "text": "<b>не жирный</b> & <script>"}]}])
    (message,) = rich_messages(doc)
    assert "&lt;b&gt;не жирный&lt;/b&gt; &amp; &lt;script&gt;" in message
    assert "A &lt; B &amp; C" in message


def test_a_table_becomes_a_list_of_its_rows():
    doc = _doc([{"heading": "Термины", "elements": [{"type": "table", "headers": ["Термин", "Определение"],
                                                     "rows": [["Репутация", "Мнение аудиторий"]]}]}])
    (message,) = rich_messages(doc)
    assert "<ul><li><b>Репутация</b>: Определение: Мнение аудиторий</li></ul>" in message


def test_a_long_note_is_cut_into_messages_within_the_limits_at_section_boundaries():
    section = lambda number: {"heading": f"Раздел {number}", "elements": [
        {"type": "bullets", "items": [f"пункт {number}.{item} " + "слово " * 60 for item in range(40)]}]}
    doc = _doc([section(number) for number in range(1, 30)])
    messages = rich_messages(doc)
    assert len(messages) > 1
    assert all(len(message) <= render_rich.CHARS_PER_MESSAGE for message in messages)
    headings = [heading for message in messages for heading in message.split("<h2>")[1:]]
    assert len(headings) == 29 and headings[0].startswith("1. Раздел 1")


def test_a_list_longer_than_one_message_is_split_into_several_lists():
    doc = _doc([{"heading": "Много", "elements": [{"type": "bullets", "items": [f"п{n}" for n in range(1000)]}]}])
    messages = rich_messages(doc)
    assert sum(message.count("<ul>") for message in messages) >= 5


def test_a_single_element_too_big_for_one_message_is_refused():
    doc = _doc([{"heading": "Огромный", "elements": [{"type": "paragraph", "text": "а" * 40_000}]}])
    with pytest.raises(RichTooLong):
        rich_messages(doc)
