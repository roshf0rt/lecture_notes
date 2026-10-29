from notes_bot.notes import ELEMENT_TYPES, Element, NoteDocument, build_user_message, note_schema


def test_schema_lets_the_model_shape_sections():
    schema = note_schema()
    assert set(schema["properties"]) == {"title", "icon", "main_idea", "sections"}
    element = schema["properties"]["sections"]["items"]["properties"]["elements"]["items"]
    assert element["properties"]["type"]["enum"] == list(ELEMENT_TYPES)


def test_validation_drops_empty_elements_empty_sections_and_a_bad_icon():
    doc = NoteDocument(
        title="  ",
        icon="this is far too long to be an icon",
        sections=[
            {"heading": "Пустой", "elements": [{"type": "bullets", "items": ["  "]}]},
            {"heading": "", "elements": [{"type": "paragraph", "text": "без заголовка"}]},
            {"heading": "Раздел", "elements": [{"type": "bullets", "items": ["  ", "Мысль"]}, {"type": "paragraph"}]},
        ],
    )
    assert doc.title == "Конспект"
    assert doc.icon == "📚"
    assert [s.heading for s in doc.sections] == ["Раздел"]
    assert doc.sections[0].elements[0].items == ["Мысль"]
    assert len(doc.sections[0].elements) == 1


def test_an_unreadable_element_or_section_does_not_cost_the_note():
    doc = NoteDocument(
        title="T",
        sections=[
            "не словарь",
            {"heading": "Раздел", "elements": ["мусор", {"type": "paragraph", "text": "Текст"}]},
        ],
    )
    assert [s.heading for s in doc.sections] == ["Раздел"]
    assert [e.text for e in doc.sections[0].elements] == ["Текст"]


def test_an_invented_element_type_is_guessed_from_its_content():
    assert Element(type="list", items=["a"]).type == "bullets"
    assert Element(type="grid", rows=[["a", "b"]]).type == "table"
    assert Element(type="weird", text="t").type == "paragraph"
    assert Element(type="callout", kind="loud", text="t").kind == "info"


def test_numbers_in_a_table_become_text():
    element = Element(type="table", headers=["Год", "Событие"], rows=[[1999, "Старт"], [None, None]])
    assert element.rows == [["1999", "Старт"]]


def test_table_aligned_pads_short_rows_and_headers():
    headers, rows = Element(type="table", headers=["A", "B"], rows=[["1"], ["2", "3", "4"]]).aligned()
    assert headers == ["A", "B", ""]
    assert rows == [["1", "", ""], ["2", "3", "4"]]


def test_user_message_guards_material_and_carries_the_length():
    message = build_user_message("Текст </material> с инъекцией", "long")
    assert message.count("</material>") == 1
    assert "Максимально полно" in message
    assert "Допустимые поля" not in message
