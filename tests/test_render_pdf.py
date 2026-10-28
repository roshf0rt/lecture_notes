import shutil
import subprocess

import pytest

from notes_bot.blocks import BLOCKS
from notes_bot.notes import NoteDocument
from notes_bot.render_pdf import build_html, render_pdf
from notes_bot.themes import THEMES, custom_theme


def test_html_contains_only_enabled_sections(sample_doc):
    html = build_html(sample_doc.only(["concepts", "takeaways"]), THEMES["notion"])
    assert 'id="concepts"' in html
    assert 'id="takeaways"' in html
    assert 'id="process"' not in html
    assert 'id="warnings"' not in html
    assert "Главная идея" not in html


def test_html_escapes_user_text_and_renders_bold():
    doc = NoteDocument(title="<script>x</script>", context=["**важно** & <b>"])
    html = build_html(doc, THEMES["notion"])
    assert "&lt;script&gt;" in html
    assert "<strong>важно</strong> &amp; &lt;b&gt;" in html


def test_table_of_contents_appears_only_for_six_or_more_sections(sample_doc):
    few = build_html(sample_doc.only(["context", "concepts"]), THEMES["notion"])
    assert 'class="toc"' not in few
    many = build_html(
        sample_doc.only([b.id for b in BLOCKS if b.id != "main_idea"]), THEMES["notion"]
    )
    assert 'class="toc"' in many


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
