"""Text cleanup preserves punctuation and run formatting in both modes."""
from collections import Counter

from lxml import etree
import pytest

from notes_bot.docx_text import W, clean_text, clean_word_text


@pytest.mark.parametrize('deep', [False, True])
def test_preserves_punctuation_and_significant_joiners(deep):
    text = '“Цитата” — ‘пример’… 2025–2026; 5 − 3 = 2. 👩\u200d💻 فارسی\u200cنویسی'
    counts = Counter()
    assert clean_text(text, deep, counts) == text
    assert counts['typography'] == 0


@pytest.mark.parametrize('deep', [False, True])
def test_cleans_split_markers_without_replacing_punctuation_or_formatting(deep):
    root = etree.Element(f'{{{W}}}document', nsmap={'w': W})
    paragraph = etree.SubElement(etree.SubElement(root, f'{{{W}}}body'), f'{{{W}}}p')
    values = ['“Заголовок” — ', ':contentReference[oaicite:', '0]{index=0}', '“текст”…\u200b']
    nodes = []
    for index, value in enumerate(values):
        run = etree.SubElement(paragraph, f'{{{W}}}r')
        properties = etree.SubElement(run, f'{{{W}}}rPr')
        etree.SubElement(properties, f'{{{W}}}' + ('b' if index == 0 else 'i'))
        node = etree.SubElement(run, f'{{{W}}}t')
        node.text = value
        nodes.append(node)
    counts = Counter()
    assert clean_word_text(root, deep, counts)
    assert [node.text or '' for node in nodes] == ['“Заголовок” — ', '', '', '“текст”…']
    assert paragraph[0].find(f'{{{W}}}rPr/{{{W}}}b') is not None
    assert paragraph[-1].find(f'{{{W}}}rPr/{{{W}}}i') is not None
    assert counts['citations'] == 1
    assert counts['invisible'] == 1
    assert counts['typography'] == 0


@pytest.mark.parametrize('deep', [False, True])
def test_url_bytes_and_surrounding_typography_survive_tracking_cleanup(deep):
    source = 'Ссылка — https://example.com/путь–тест?x=%E2%80%94&keep=a%20b&utm_source=chatgpt.com#раздел…'
    expected = 'Ссылка — https://example.com/путь–тест?x=%E2%80%94&keep=a%20b#раздел…'
    counts = Counter()
    assert clean_text(source, deep, counts) == expected
    assert counts['links'] == 1
    assert counts['typography'] == 0
