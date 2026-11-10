"""Text cleanup preserves punctuation and run formatting in both modes."""
from collections import Counter

from lxml import etree
import pytest

from notes_bot.docx_text import W, XML_SPACE, clean_text, clean_word_text, compact_word_runs


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


def text_run(parent, value, *, style=None, rsid=None):
    run = etree.SubElement(parent, f'{{{W}}}r')
    if rsid:
        run.set(f'{{{W}}}rsidR', rsid)
    if style:
        props = etree.SubElement(run, f'{{{W}}}rPr')
        etree.SubElement(props, f'{{{W}}}{style}')
    node = etree.SubElement(run, f'{{{W}}}t')
    node.text = value
    if value and (value[0].isspace() or value[-1].isspace()):
        node.set(XML_SPACE, 'preserve')
    return run


def test_compacts_seven_hundred_equivalent_runs_without_changing_text_or_style():
    paragraph = etree.Element(f'{{{W}}}p', nsmap={'w': W})
    values = [f'{i} — «пример»… ' for i in range(700)]
    for value in values:
        text_run(paragraph, value, style='b', rsid='00112233')
    counts = Counter()
    assert compact_word_runs(paragraph, counts)
    runs = paragraph.findall(f'{{{W}}}r')
    assert len(runs) == 1
    assert runs[0].find(f'{{{W}}}rPr/{{{W}}}b') is not None
    assert runs[0].get(f'{{{W}}}rsidR') == '00112233'
    assert runs[0].find(f'{{{W}}}t').text == ''.join(values)
    assert runs[0].find(f'{{{W}}}t').get(XML_SPACE) == 'preserve'
    assert counts == {'runs_before': 700, 'runs_after': 1, 'runs_merged': 699}
    assert not compact_word_runs(paragraph, Counter())


def test_preserves_different_run_formatting_and_revision_ids():
    paragraph = etree.Element(f'{{{W}}}p', nsmap={'w': W})
    text_run(paragraph, 'жирный', style='b', rsid='00112233')
    text_run(paragraph, 'курсив', style='i', rsid='00112233')
    text_run(paragraph, 'первая сессия', rsid='00112233')
    text_run(paragraph, 'вторая сессия', rsid='00445566')
    before = etree.tostring(paragraph)
    assert not compact_word_runs(paragraph, Counter())
    assert etree.tostring(paragraph) == before


@pytest.mark.parametrize('barrier', ['tab', 'br', 'footnoteReference', 'drawing', 'fldChar', 'instrText'])
def test_does_not_merge_across_nontext_runs(barrier):
    paragraph = etree.Element(f'{{{W}}}p', nsmap={'w': W})
    text_run(paragraph, 'до ')
    run = etree.SubElement(paragraph, f'{{{W}}}r')
    etree.SubElement(run, f'{{{W}}}{barrier}')
    text_run(paragraph, ' после')
    before = etree.tostring(paragraph)
    assert not compact_word_runs(paragraph, Counter())
    assert etree.tostring(paragraph) == before


def test_bookmarks_and_hyperlinks_remain_boundaries_but_link_text_can_compact():
    paragraph = etree.Element(f'{{{W}}}p', nsmap={'w': W})
    text_run(paragraph, 'до')
    etree.SubElement(paragraph, f'{{{W}}}bookmarkStart', {f'{{{W}}}id': '1'})
    text_run(paragraph, 'закладки')
    etree.SubElement(paragraph, f'{{{W}}}bookmarkEnd', {f'{{{W}}}id': '1'})
    hyperlink = etree.SubElement(paragraph, f'{{{W}}}hyperlink')
    text_run(hyperlink, 'ссыл')
    text_run(hyperlink, 'ка')
    text_run(paragraph, 'после')
    counts = Counter()
    assert compact_word_runs(paragraph, counts)
    assert len(paragraph.findall(f'{{{W}}}r')) == 3
    assert len(hyperlink.findall(f'{{{W}}}r')) == 1
    assert hyperlink.find(f'{{{W}}}r/{{{W}}}t').text == 'ссылка'
    assert len(paragraph.findall(f'{{{W}}}bookmarkStart')) == 1
    assert len(paragraph.findall(f'{{{W}}}bookmarkEnd')) == 1
    assert counts['runs_merged'] == 1


@pytest.mark.parametrize('container', ['fldSimple', 'ins', 'del', 'moveFrom', 'moveTo'])
def test_does_not_rewrite_fields_or_tracked_containers(container):
    paragraph = etree.Element(f'{{{W}}}p', nsmap={'w': W})
    wrapper = etree.SubElement(paragraph, f'{{{W}}}{container}')
    text_run(wrapper, 'не')
    text_run(wrapper, 'менять')
    before = etree.tostring(paragraph)
    assert not compact_word_runs(paragraph, Counter())
    assert etree.tostring(paragraph) == before


def test_compaction_keeps_multiple_text_nodes_and_significant_spaces():
    paragraph = etree.Element(f'{{{W}}}p', nsmap={'w': W})
    first = text_run(paragraph, ' текст ')
    second = etree.SubElement(first, f'{{{W}}}t')
    second.text = 'и'
    text_run(paragraph, ' ещё  текст ')
    assert compact_word_runs(paragraph, Counter())
    assert len(paragraph.findall(f'{{{W}}}r')) == 1
    texts = first.findall(f'{{{W}}}t')
    assert len(texts) == 1
    assert texts[0].text == ' текст и ещё  текст '
    assert texts[0].get(XML_SPACE) == 'preserve'


@pytest.mark.parametrize('value', [' leading', 'trailing '])
@pytest.mark.parametrize('space_mode', [None, 'default'])
def test_compaction_does_not_expose_default_boundary_whitespace(value, space_mode):
    paragraph = etree.Element(f'{{{W}}}p', nsmap={'w': W})
    first = text_run(paragraph, value)
    node = first.find(f'{{{W}}}t')
    node.attrib.pop(XML_SPACE, None)
    if space_mode is not None:
        node.set(XML_SPACE, space_mode)
    text_run(paragraph, 'next')
    before = etree.tostring(paragraph)
    assert not compact_word_runs(paragraph, Counter())
    assert etree.tostring(paragraph) == before
