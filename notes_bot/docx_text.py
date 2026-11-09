"""Improve Word text by removing technical clutter across formatting runs.

Paragraphs, fields, breaks and visible objects form boundaries. The module never
rebuilds paragraphs or changes field instructions, styles, drawings or links.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
import re
from urllib.parse import unquote_plus, urlsplit

from lxml import etree

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'
INVISIBLE = re.compile('[\u200b\u2060\ufeff\u00ad\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]')
SPACES = str.maketrans({c: ' ' for c in '\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000'})
TYPOGRAPHY = {'\u2013': '-', '\u2014': '-', '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"', '\u2026': '...'}
TYPOGRAPHY_PATTERN = re.compile('[' + ''.join(TYPOGRAPHY) + ']')
CITATIONS = re.compile(
    r'(?:cite|filecite|navlist|products|finance|forecast|schedule|standing)[^]*'
    r'|:contentReference\[oaicite:\d+\](?:\{[^}]*\})?'
    r'|\bturn\d+(?:search|view|news|image)\d+\b', re.I,
)
URLS = re.compile(r'https?://[^\s<>"\u201c\u201d\u00ab\u00bb]+', re.I)
TEXT_TAGS = {f'{{{W}}}t', f'{{{W}}}delText'}
PARAGRAPH = f'{{{W}}}p'
PROPERTIES = {f'{{{W}}}{name}' for name in ('pPr', 'rPr', 'sdtPr')}
BOUNDARY_ELEMENTS = {f'{{{W}}}{name}' for name in (
    'tab', 'ptab', 'br', 'cr', 'fldChar', 'instrText', 'delInstrText',
    'drawing', 'pict', 'object', 'sym', 'noBreakHyphen', 'softHyphen',
    'footnoteReference', 'endnoteReference', 'footnoteRef', 'endnoteRef',
    'separator', 'continuationSeparator', 'lastRenderedPageBreak',
)}
BOUNDARY_CONTAINERS = {f'{{{W}}}{name}' for name in (
    'fldSimple', 'ins', 'del', 'moveFrom', 'moveTo',
)}


def _utm_edits(url: str) -> list[tuple[int, int, str]]:
    """Return deletions for a supported tracking parameter, keeping other bytes.

    Re-encoding a complete query can break signed URLs and alter escaping. Only
    the identified query pieces and a neighbouring ampersand are removed here.
    """
    try:
        urlsplit(url)  # Validate malformed bracketed hosts without fetching.
    except ValueError:
        return []
    question = url.find('?')
    fragment = url.find('#')
    if question < 0 or (fragment >= 0 and fragment < question):
        return []
    query_end = fragment if fragment >= 0 else len(url)
    query = url[question + 1:query_end]
    pieces = query.split('&')
    remove = []
    starts = []
    cursor = question + 1
    for piece in pieces:
        starts.append(cursor)
        key, _, value = piece.partition('=')
        remove.append(unquote_plus(key).lower() == 'utm_source' and unquote_plus(value).lower() == 'chatgpt.com')
        cursor += len(piece) + 1
    if not any(remove):
        return []
    if all(remove):
        return [(question, query_end, '')]
    edits = []
    i = 0
    while i < len(pieces):
        if not remove[i]:
            i += 1
            continue
        first = i
        while i + 1 < len(pieces) and remove[i + 1]:
            i += 1
        last = i
        if first == 0:
            # Keep the question mark, remove the following separator as well.
            edits.append((starts[first], starts[last + 1], ''))
        else:
            edits.append((starts[first] - 1, starts[last] + len(pieces[last]), ''))
        i += 1
    return edits


def strip_utm(url: str) -> str:
    """Remove supported tracking data without normalising the remaining URL."""
    for start, end, replacement in reversed(_utm_edits(url)):
        url = url[:start] + replacement + url[end:]
    return url


def _prose_url_end(value: str) -> int:
    """Keep balanced URL parentheses; exclude common punctuation from prose."""
    end = len(value)
    pairs = {')': '(', ']': '[', '}': '{'}
    while end:
        last = value[end - 1]
        if last in '.,;:!?':
            end -= 1
        elif last in pairs and value[:end].count(last) > value[:end].count(pairs[last]):
            end -= 1
        else:
            break
    return end


def _set_text(element, value: str) -> bool:
    if (element.text or '') == value:
        return False
    element.text = value
    if value and (value[0].isspace() or value[-1].isspace()):
        element.set(XML_SPACE, 'preserve')
    return True


def _apply_edits(elements, edits: list[tuple[int, int, str]]) -> bool:
    """Apply non-overlapping edits in concatenated text without moving runs.

    Unchanged slices stay in their original text nodes. Replacement characters
    inherit formatting from the first replaced character. Work is linear in the
    text size and the number of edit spans, including very long paragraphs.
    """
    if not edits:
        return False
    values = [element.text or '' for element in elements]
    offsets = [0]
    for value in values:
        offsets.append(offsets[-1] + len(value))
    size = offsets[-1]
    pieces = [[] for _ in elements]

    def append_original(start: int, end: int):
        if start >= end:
            return
        index = bisect_right(offsets, start) - 1
        while start < end:
            stop = min(end, offsets[index + 1])
            if stop > start:
                pieces[index].append(values[index][start - offsets[index]:stop - offsets[index]])
            start = stop
            index += 1

    cursor = 0
    for start, end, replacement in sorted(edits):
        if start < cursor or not (0 <= start < end <= size):
            raise ValueError('Text edits must be non-overlapping replacement spans')
        append_original(cursor, start)
        if replacement:
            index = bisect_right(offsets, start) - 1
            pieces[index].append(replacement)
        cursor = end
    append_original(cursor, size)
    changed = False
    for element, chunks in zip(elements, pieces):
        changed = _set_text(element, ''.join(chunks)) or changed
    return changed


def _clean_group(elements, deep: bool, counts: Counter) -> bool:
    changed = False
    for element in elements:
        original = element.text or ''
        value, removed = INVISIBLE.subn('', original)
        counts['invisible'] += removed
        counts['spaces'] += sum(ord(char) in SPACES for char in value)
        value = value.translate(SPACES)
        changed = _set_text(element, value) or changed
    text = ''.join(element.text or '' for element in elements)
    if not text:
        return changed
    # Literal paths and query values can legitimately contain words like
    # turn0search0, so citation removal must leave complete URLs intact.
    urls = list(URLS.finditer(text))
    url_index = 0
    edits = []
    for match in CITATIONS.finditer(text):
        while url_index < len(urls) and urls[url_index].end() <= match.start():
            url_index += 1
        if url_index < len(urls) and urls[url_index].start() < match.end():
            continue
        edits.append((match.start(), match.end(), ''))
        counts['citations'] += 1
    changed = _apply_edits(elements, edits) or changed
    text = ''.join(element.text or '' for element in elements)
    edits = []
    outside_start = 0
    for match in URLS.finditer(text):
        if deep:
            for mark in TYPOGRAPHY_PATTERN.finditer(text, outside_start, match.start()):
                edits.append((mark.start(), mark.end(), TYPOGRAPHY[mark.group()]))
                counts['typography'] += 1
        candidate = match.group()[:_prose_url_end(match.group())]
        url_edits = _utm_edits(candidate)
        if url_edits:
            counts['links'] += 1
            edits.extend((match.start() + start, match.start() + end, replacement) for start, end, replacement in url_edits)
        outside_start = match.end()
    if deep:
        for mark in TYPOGRAPHY_PATTERN.finditer(text, outside_start):
            edits.append((mark.start(), mark.end(), TYPOGRAPHY[mark.group()]))
            counts['typography'] += 1
    return _apply_edits(elements, edits) or changed


def clean_text(value: str, deep: bool, counts: Counter) -> str:
    """Compatibility helper for isolated strings using the same cleanup rules."""
    element = etree.Element(f'{{{W}}}t')
    element.text = value
    _clean_group([element], deep, counts)
    return element.text or ''


def clean_word_text(root, deep: bool, counts: Counter) -> bool:
    """Clean display text in each paragraph; return whether XML was changed.

    Citation markers can cross run/hyperlink/bookmark boundaries, but never a
    paragraph, tab, line break, field instruction, revision or visible object.
    Word instruction text and DrawingML text are intentionally not rewritten.
    """
    changed = False
    processed = set()
    for paragraph in root.iter(PARAGRAPH):
        group = []

        def flush():
            nonlocal changed
            if group:
                changed = _clean_group(group, deep, counts) or changed
                group.clear()

        def visit(element):
            if not isinstance(element.tag, str):
                flush()
                return
            if element.tag == PARAGRAPH and element is not paragraph:
                flush()
                return
            if element.tag in PROPERTIES:
                return
            if element.tag in TEXT_TAGS:
                processed.add(element)
                group.append(element)
                return
            if element.tag in BOUNDARY_ELEMENTS:
                flush()
                return
            container_boundary = element.tag in BOUNDARY_CONTAINERS
            if container_boundary:
                flush()
            for child in element:
                visit(child)
            if container_boundary:
                flush()

        visit(paragraph)
        flush()
    # Rare non-paragraph text nodes still get local cleanup, never concatenation.
    for element in root.iter():
        if element.tag in TEXT_TAGS and element not in processed:
            changed = _clean_group([element], deep, counts) or changed
    return changed
