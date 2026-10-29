import shutil
import subprocess

import pytest

from notes_bot.notes import NoteDocument
from notes_bot.render_pdf import build_html, render_pdf
from notes_bot.themes import THEMES, custom_theme


def test_html_has_numbered_sections_with_anchors_and_every_element_kind(sample_doc):
    html = build_html(sample_doc, THEMES["notion"])
    for number, heading in enumerate(["Определения", "Порядок проверки рекламы", "Контрреклама и антиреклама", "Что запомнить"], start=1):
        assert f'id="s{number}"' in html and f"{number}. {heading}" in html
    for marker in ['class="numbered"', 'class="checklist"', 'class="toggle"', "callout-warning", "<table>", "<h3"]:
        assert marker in html, marker
    assert "Главная идея лекции" in html


def test_html_escapes_user_text_and_renders_bold():
    doc = NoteDocument(title="<script>x</script>", sections=[
        {"heading": "<b>H</b>", "elements": [{"type": "paragraph", "text": "**важно** & <b>"}]},
    ])
    html = build_html(doc, THEMES["notion"])
    assert "&lt;script&gt;" in html and "&lt;b&gt;H&lt;/b&gt;" in html
    assert "<strong>важно</strong> &amp; &lt;b&gt;" in html


def test_table_of_contents_appears_only_from_three_sections(sample_doc):
    few = build_html(sample_doc.model_copy(update={"sections": sample_doc.sections[:2]}), THEMES["notion"])
    assert 'class="toc"' not in few
    many = build_html(sample_doc, THEMES["notion"])
    assert 'class="toc"' in many and 'href="#s4"' in many
    assert 'href="#s2-1"' in many  # a subheading is listed under its section


def test_a_note_without_sections_is_still_a_document():
    html = build_html(NoteDocument(title="Пусто", main_idea="Идея."), THEMES["notion"])
    assert "Идея." in html and 'class="toc"' not in html


def test_theme_colours_reach_the_css(sample_doc):
    html = build_html(sample_doc, custom_theme("#0f766e"))
    assert ".tone-blue h2 { color: #0f766e; }" in html


def test_render_pdf_produces_a_pdf_file(sample_doc):
    pdf = render_pdf(sample_doc, THEMES["notion"])
    assert pdf.startswith(b"%PDF")
    assert pdf.rstrip().endswith(b"%%EOF")


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext not installed")
def test_pdf_text_keeps_cyrillic(sample_doc, tmp_path):
    path = tmp_path / "note.pdf"
    path.write_bytes(render_pdf(sample_doc, THEMES["notion"]))
    text = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True, check=True).stdout
    assert "Рекламодатель" in text
    assert "Главная идея" in text
