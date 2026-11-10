"""Regression tests using a real Word package, not just text substitutions.

Run: python -m unittest discover -s tests -p test_docx_cleaner.py -v
Test-only dependency: python-docx>=1.2,<2
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
import posixpath
import struct
import unittest
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile
import zlib

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.shared import Inches
from lxml import etree

from notes_bot.docx_cleaner import APP, CT, REL, W, InvalidDocument, clean_docx

PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8A'
    'AQUBAScY42YAAAAASUVORK5CYII='
)


def package_edit(content: bytes, *, replacements=None, additions=None, removed=()) -> bytes:
    replacements = replacements or {}
    additions = additions or {}
    output = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(output, 'w', ZIP_DEFLATED) as result:
        for name in source.namelist():
            if name in removed:
                continue
            result.writestr(name, replacements.get(name, source.read(name)))
        for name, data in additions.items():
            result.writestr(name, data)
    return output.getvalue()


def rgb_png(rgb):
    """A distinct valid PNG using only stdlib, for package relationship tests."""
    def chunk(kind, value):
        return struct.pack('!I', len(value)) + kind + value + struct.pack('!I', zlib.crc32(kind + value))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0' + bytes(rgb))) + chunk(b'IEND', b''))


def revision(kind: str, text: str, id_: int):
    node = OxmlElement(f'w:{kind}')
    node.set(f'{{{W}}}id', str(id_))
    node.set(f'{{{W}}}author', 'Previous Revision Author')
    node.set(f'{{{W}}}date', '2021-01-01T00:00:00Z')
    run = OxmlElement('w:r')
    t = OxmlElement('w:delText' if kind in ('del', 'moveFrom') else 'w:t')
    t.text = text
    run.append(t)
    node.append(run)
    return node


def fixture() -> bytes:
    """Produce a normal python-docx file with realistic things to preserve/remove."""
    doc = Document()
    doc.core_properties.author = 'Previous Core Author'
    doc.core_properties.last_modified_by = 'Previous Core Editor'
    doc.core_properties.title = 'Technical cleanup fixture'
    p = doc.add_paragraph()
    first = p.add_run('Заголовок')
    first.bold = True
    p.add_run(' — текст\u200b с\u00a0пробелами…')
    p._p.set(f'{{{W}}}rsidR', '00ABCDEF')
    p._p.set('{http://schemas.microsoft.com/office/word/2010/wordml}paraId', '12345678')
    doc.add_comment(first, text='Private comment payload', author='Previous Comment Author', initials='PA')
    direct = doc.add_paragraph().add_run('Private hidden payload')
    direct.font.hidden = True
    style = doc.styles.add_style('HiddenTestStyle', WD_STYLE_TYPE.CHARACTER)
    style.font.hidden = True
    inherited = doc.add_paragraph().add_run('Private styled hidden payload')
    inherited.style = style
    visible = doc.add_paragraph().add_run('Visible override')
    visible.style = style
    visible.font.hidden = False
    edits = doc.add_paragraph('Revision: ')
    edits._p.append(revision('ins', 'Inserted text', 1))
    edits._p.append(revision('del', 'Private deleted payload', 2))
    edits._p.append(revision('moveFrom', 'Private moved source payload', 3))
    edits._p.append(revision('moveTo', 'Moved text', 4))
    citation = doc.add_paragraph('Before ')
    citation.add_run(':contentReference[oaicite:').italic = True
    citation.add_run('0]{index=0}')
    citation.add_run(' after').bold = True
    url = doc.add_paragraph()
    url.add_run('https://example.com/page?utm_')
    url.add_run('source=chatgpt.com&x=1')
    hyperlink = OxmlElement('w:hyperlink')
    rid = doc.part.relate_to('https://example.com/?utm_source=chatgpt.com&x=1', RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    hyperlink.set('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id', rid)
    run = OxmlElement('w:r'); text = OxmlElement('w:t'); text.text = 'Link'; run.append(text); hyperlink.append(run)
    doc.add_paragraph()._p.append(hyperlink)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = 'Table cell one'
    table.cell(1, 1).text = 'Table cell two'
    doc.sections[0].header.paragraphs[0].text = 'Header text'
    doc.sections[0].footer.paragraphs[0].text = 'Footer text'
    doc.add_picture(BytesIO(PNG), width=Inches(0.2))
    variables = OxmlElement('w:docVars')
    variable = OxmlElement('w:docVar')
    variable.set(f'{{{W}}}name', 'PrivateVariable')
    variable.set(f'{{{W}}}val', 'Private variable payload')
    variables.append(variable)
    doc.settings.element.append(variables)
    buf = BytesIO(); doc.save(buf)
    return buf.getvalue()


class DocxCleanerTests(unittest.TestCase):
    def assert_package_integrity(self, content):
        with ZipFile(BytesIO(content)) as z:
            names = set(z.namelist())
            self.assertEqual(len(names), len(z.namelist()), 'Duplicate ZIP names')
            self.assertIsNone(z.testzip())
            for name in names:
                if not name.endswith('.rels'):
                    continue
                root = etree.fromstring(z.read(name))
                owner = '' if name == '_rels/.rels' else name.replace('/_rels/', '/')[:-5]
                for rel in root:
                    if rel.get('TargetMode') == 'External':
                        continue
                    target = unquote(urlsplit(rel.get('Target')).path)
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(owner), target)).lstrip('/')
                    self.assertIn(resolved, names, f'Broken relationship in {name}: {target}')
            for element in etree.fromstring(z.read('[Content_Types].xml')):
                if element.tag == f'{{{CT}}}Override':
                    self.assertIn(element.get('PartName').lstrip('/'), names)

    def test_deep_clean_preserves_visible_document_and_sets_metadata(self):
        source = fixture()
        output, counts = clean_docx(source, True, ['Иван Иванов', 'Анна Петрова'])
        result = Document(BytesIO(output))
        self.assertEqual(result.core_properties.author, 'Иван Иванов; Анна Петрова')
        self.assertEqual(result.core_properties.last_modified_by, 'Анна Петрова')
        self.assertEqual(result.tables[0].cell(1, 1).text, 'Table cell two')
        self.assertEqual(result.sections[0].header.paragraphs[0].text, 'Header text')
        self.assertEqual(result.sections[0].footer.paragraphs[0].text, 'Footer text')
        self.assertTrue(result.paragraphs[0].runs[0].bold)
        self.assertEqual(len(result.inline_shapes), 1)
        self.assert_package_integrity(output)
        with ZipFile(BytesIO(source)) as before, ZipFile(BytesIO(output)) as after:
            for name in before.namelist():
                if name.startswith('word/media/'):
                    self.assertEqual(before.read(name), after.read(name))
            document = etree.fromstring(after.read('word/document.xml'))
            text = ''.join(document.itertext())
            for retained in ('Inserted text', 'Moved text', 'Visible override', 'Before ', ' after'):
                self.assertIn(retained, text)
            for removed in ('Private hidden payload', 'Private styled hidden payload', 'Private deleted payload', 'Private moved source payload', ':contentReference', 'utm_source=chatgpt.com'):
                self.assertNotIn(removed, text)
            for name in after.namelist():
                if name.endswith(('.xml', '.rels')):
                    data = after.read(name)
                    for secret in (b'Previous Comment Author', b'Previous Revision Author',
                                   b'Private comment payload', b'Private variable payload'):
                        self.assertFalse(secret in data, f'{secret!r} remains in {name}')
            self.assertFalse(any(name.startswith('customXml/') for name in after.namelist()))
            self.assertFalse(any('comments' in name.lower() for name in after.namelist()))
            minutes = int(etree.fromstring(after.read('docProps/app.xml')).find(f'{{{APP}}}TotalTime').text)
            self.assertEqual(minutes, counts['minutes'])
            self.assertEqual(result.core_properties.created, Document(BytesIO(source)).core_properties.created)
            self.assertGreater(result.core_properties.modified, result.core_properties.created)

    def test_existing_revision_session_ids_are_preserved_without_new_history(self):
        source = fixture()
        with ZipFile(BytesIO(source)) as z:
            document = etree.fromstring(z.read('word/document.xml'))
            settings = etree.fromstring(z.read('word/settings.xml'))
        run = document.find(f'.//{{{W}}}r')
        run.set(f'{{{W}}}rsidR', '00112233')
        run.set(f'{{{W}}}rsidRPr', '00445566')
        for old in list(settings.findall(f'{{{W}}}rsids')):
            settings.remove(old)
        sessions = etree.SubElement(settings, f'{{{W}}}rsids')
        etree.SubElement(sessions, f'{{{W}}}rsidRoot').set(f'{{{W}}}val', '00112233')
        etree.SubElement(sessions, f'{{{W}}}rsid').set(f'{{{W}}}val', '00445566')
        source = package_edit(source, replacements={
            'word/document.xml': etree.tostring(document),
            'word/settings.xml': etree.tostring(settings),
        })
        output, _ = clean_docx(source, True, ['Author'])
        self.assert_package_integrity(output)
        result = Document(BytesIO(output))
        self.assertEqual(result.paragraphs[0].text, 'Заголовок — текст с пробелами…')
        with ZipFile(BytesIO(output)) as z:
            processed = etree.fromstring(z.read('word/document.xml'))
            processed_settings = etree.fromstring(z.read('word/settings.xml'))
        def ids(root):
            return {(etree.QName(key).localname, value)
                    for node in root.iter() for key, value in node.attrib.items()
                    if etree.QName(key).namespace == W and etree.QName(key).localname.startswith('rsid')}
        self.assertEqual(ids(processed), ids(document))
        self.assertEqual(
            etree.tostring(processed_settings.find(f'{{{W}}}rsids'), method='c14n'),
            etree.tostring(sessions, method='c14n'),
        )

    def test_package_without_revision_session_ids_does_not_gain_any(self):
        document = Document()
        document.add_paragraph('Обычный текст без истории сеансов.')
        for root in (document.element, document.settings.element):
            for node in list(root.iter()):
                if not isinstance(node.tag, str):
                    continue
                for key in list(node.attrib):
                    if etree.QName(key).namespace == W and etree.QName(key).localname.startswith('rsid'):
                        del node.attrib[key]
                if node.tag in (f'{{{W}}}rsids', f'{{{W}}}rsid') and node.getparent() is not None:
                    node.getparent().remove(node)
        buf = BytesIO(); document.save(buf)
        output, _ = clean_docx(buf.getvalue(), True, ['Author'])
        with ZipFile(BytesIO(output)) as z:
            for name in ('word/document.xml', 'word/settings.xml'):
                root = etree.fromstring(z.read(name))
                for node in root.iter():
                    self.assertNotIn(node.tag, (f'{{{W}}}rsids', f'{{{W}}}rsid'))
                    for key in node.attrib:
                        self.assertFalse(etree.QName(key).namespace == W and
                                         etree.QName(key).localname.startswith('rsid'))

    def test_zip_entries_use_processing_time_and_portable_creator_profile(self):
        for deep in (False, True):
            with self.subTest(deep=deep):
                before = datetime.now(timezone.utc)
                output, _ = clean_docx(fixture(), deep, ['Author'])
                after = datetime.now(timezone.utc)
                self.assert_package_integrity(output)
                Document(BytesIO(output))
                with ZipFile(BytesIO(output)) as z:
                    for entry in z.infolist():
                        timestamp = datetime(*entry.date_time, tzinfo=timezone.utc)
                        # ZIP timestamps have no timezone field and a two-second resolution.
                        self.assertGreaterEqual(timestamp, before - timedelta(seconds=2))
                        self.assertLessEqual(timestamp, after)
                        self.assertEqual(entry.create_system, 0)

    def test_identically_formatted_text_fragments_are_compacted_without_content_changes(self):
        document = Document()
        paragraph = document.add_paragraph()
        fragments = ['Слово — «пример»… ' for _ in range(700)]
        for text in fragments:
            paragraph.add_run(text).bold = True
        source = BytesIO(); document.save(source)
        output, counts = clean_docx(source.getvalue(), True, ['Author'])
        self.assert_package_integrity(output)
        result = Document(BytesIO(output))
        self.assertEqual(result.paragraphs[0].text, ''.join(fragments))
        self.assertEqual(len(result.paragraphs[0].runs), 1)
        self.assertTrue(result.paragraphs[0].runs[0].bold)
        self.assertEqual(counts['runs_merged'], 699)

    def test_preparation_time_is_bounded_and_not_inferred_as_creation_date(self):
        for length, expected in ((0, 0), (1, 5), (14950, 90), (150000, 240)):
            with self.subTest(length=length):
                source = Document()
                source.core_properties.created = datetime(2020, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
                source.add_paragraph('а' * length)
                buf = BytesIO(); source.save(buf)
                before = datetime.now(timezone.utc).replace(microsecond=0)
                output, counts = clean_docx(buf.getvalue(), True, ['Перескоков Иван'])
                after = datetime.now(timezone.utc)
                result = Document(BytesIO(output))
                self.assertEqual(counts['minutes'], expected)
                self.assertEqual(counts['minutes_manual'], 0)
                self.assertEqual(result.core_properties.created, source.core_properties.created)
                self.assertLessEqual(before, result.core_properties.modified)
                self.assertLessEqual(result.core_properties.modified, after)
                with ZipFile(BytesIO(output)) as z:
                    app = etree.fromstring(z.read('docProps/app.xml'))
                    self.assertEqual(app.find(f'{{{APP}}}TotalTime').text, str(expected))
                    self.assertEqual(app.find(f'{{{APP}}}Application').text, 'Microsoft Office Word')

    def test_manual_time_and_application_are_metadata_values_only(self):
        source = fixture()
        output, counts = clean_docx(source, True, ['Перескоков Иван'], editing_minutes=12,
                                    application_name='Microsoft Office Word')
        self.assertEqual(counts['minutes'], 12)
        self.assertEqual(counts['minutes_manual'], 1)
        result = Document(BytesIO(output))
        self.assertEqual(result.core_properties.created, Document(BytesIO(source)).core_properties.created)
        with ZipFile(BytesIO(output)) as z:
            app = etree.fromstring(z.read('docProps/app.xml'))
            self.assertEqual(app.find(f'{{{APP}}}Application').text, 'Microsoft Office Word')
            self.assertEqual(app.find(f'{{{APP}}}TotalTime').text, '12')
        for minutes in (-1, 10081, True, 1.5, '15'):
            with self.subTest(minutes=minutes), self.assertRaises(InvalidDocument):
                clean_docx(source, True, ['Author'], editing_minutes=minutes)
        for application in ('', 'x' * 101, 'Word\x00test'):
            with self.subTest(application=repr(application)), self.assertRaises(InvalidDocument):
                clean_docx(source, True, ['Author'], application_name=application)

    def test_missing_settings_part_is_created_with_valid_relationship_and_type(self):
        source = fixture()
        with ZipFile(BytesIO(source)) as z:
            rels = etree.fromstring(z.read('word/_rels/document.xml.rels'))
            types = etree.fromstring(z.read('[Content_Types].xml'))
        for el in list(rels):
            if el.get('Type', '').endswith('/settings'):
                rels.remove(el)
        for el in list(types):
            if el.get('PartName') == '/word/settings.xml':
                types.remove(el)
        source = package_edit(source, replacements={
            'word/_rels/document.xml.rels': etree.tostring(rels),
            '[Content_Types].xml': etree.tostring(types),
        }, removed={'word/settings.xml'})
        output, _ = clean_docx(source, True, ['Author'])
        self.assert_package_integrity(output)
        result = Document(BytesIO(output))
        self.assertEqual(result.settings.element.tag, f'{{{W}}}settings')
        with ZipFile(BytesIO(output)) as z:
            rels = etree.fromstring(z.read('word/_rels/document.xml.rels'))
            settings = [el for el in rels if el.get('Type', '').endswith('/settings')]
            self.assertEqual(len(settings), 1)
            self.assertEqual(settings[0].get('Target'), 'settings.xml')
            types = etree.fromstring(z.read('[Content_Types].xml'))
            overrides = [el for el in types if el.get('PartName') == '/word/settings.xml']
            self.assertEqual(len(overrides), 1)
            self.assertEqual(overrides[0].get('ContentType'),
                             'application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml')

    def test_existing_settings_at_custom_path_preserve_visible_options(self):
        source = fixture()
        custom = 'word/comments-settings.xml'
        with ZipFile(BytesIO(source)) as z:
            rels = etree.fromstring(z.read('word/_rels/document.xml.rels'))
            types = etree.fromstring(z.read('[Content_Types].xml'))
            settings = etree.fromstring(z.read('word/settings.xml'))
        zoom = settings.find(f'{{{W}}}zoom')
        if zoom is None:
            zoom = etree.SubElement(settings, f'{{{W}}}zoom')
        zoom.set(f'{{{W}}}percent', '85')
        for el in rels:
            if el.get('Type', '').endswith('/settings'):
                el.set('Target', posixpath.basename(custom))
        for el in types:
            if el.get('PartName') == '/word/settings.xml':
                el.set('PartName', '/' + custom)
        source = package_edit(source, replacements={
            'word/_rels/document.xml.rels': etree.tostring(rels),
            '[Content_Types].xml': etree.tostring(types),
        }, additions={custom: etree.tostring(settings)}, removed={'word/settings.xml'})
        output, _ = clean_docx(source, True, ['Author'])
        self.assert_package_integrity(output)
        result = Document(BytesIO(output))
        self.assertEqual(result.settings.element.find(f'{{{W}}}zoom').get(f'{{{W}}}percent'), '85')
        with ZipFile(BytesIO(output)) as z:
            self.assertIn(custom, z.namelist())
            self.assertNotIn('word/settings.xml', z.namelist())

    def test_missing_creation_date_uses_processing_time_and_xml_headers_are_valid(self):
        source = fixture()
        with ZipFile(BytesIO(source)) as z:
            core = etree.fromstring(z.read('docProps/core.xml'))
        created_tag = '{http://purl.org/dc/terms/}created'
        for el in core.findall(created_tag):
            core.remove(el)
        source = package_edit(source, replacements={'docProps/core.xml': etree.tostring(core)})
        before = datetime.now(timezone.utc).replace(microsecond=0)
        output, _ = clean_docx(source, True, ['Author'], editing_minutes=20)
        result = Document(BytesIO(output))
        self.assertGreaterEqual(result.core_properties.created, before)
        self.assertEqual(result.core_properties.created, result.core_properties.modified)
        with ZipFile(BytesIO(output)) as z:
            for path in ('docProps/core.xml', 'docProps/app.xml', 'word/document.xml', 'word/settings.xml'):
                data = z.read(path)
                self.assertTrue(data.startswith(b'<?xml version="1.0" encoding="UTF-8"'), path)
                etree.fromstring(data)

    def test_basic_mode_keeps_comments_hidden_content_and_revisions(self):
        output, _ = clean_docx(fixture(), False, ['Author'])
        self.assert_package_integrity(output)
        with ZipFile(BytesIO(output)) as z:
            self.assertIn('word/comments.xml', z.namelist())
            document = etree.fromstring(z.read('word/document.xml'))
            self.assertIn('Private hidden payload', ''.join(document.itertext()))
            self.assertIsNotNone(document.find(f'.//{{{W}}}ins'))
        self.assertEqual(Document(BytesIO(output)).core_properties.author, 'Author')

    def test_missing_metadata_parts_are_created(self):
        source, _ = clean_docx(fixture(), True)
        output, _ = clean_docx(source, True, ['One', 'Two'])
        self.assert_package_integrity(output)
        self.assertEqual(Document(BytesIO(output)).core_properties.author, 'One; Two')

    def test_rejects_bad_archives_macros_and_invalid_authors(self):
        with self.assertRaises(InvalidDocument):
            clean_docx(b'not a zip')
        source = fixture()
        macro = package_edit(source, additions={'word/vbaProject.bin': b'macro'})
        with self.assertRaises(InvalidDocument):
            clean_docx(macro, True, ['Author'])
        for authors in ([], [''], ['One\x00Two'], ['x' * 201]):
            with self.subTest(authors=repr(authors)), self.assertRaises(InvalidDocument):
                clean_docx(source, True, authors)

    def test_rejects_unsupported_strict_format(self):
        source = fixture()
        with ZipFile(BytesIO(source)) as z:
            document = z.read('word/document.xml').replace(W.encode(), b'http://purl.oclc.org/ooxml/wordprocessingml/main')
        source = package_edit(source, replacements={'word/document.xml': document})
        with self.assertRaises(InvalidDocument):
            clean_docx(source, True, ['Author'])

    def test_accepts_deleted_table_row_but_refuses_complex_cell_and_paragraph_edits(self):
        source = fixture()
        with ZipFile(BytesIO(source)) as z:
            original = z.read('word/document.xml')
        root = etree.fromstring(original)
        row = root.find(f'.//{{{W}}}tr')
        props = OxmlElement('w:trPr')
        deletion = OxmlElement('w:del'); deletion.set(f'{{{W}}}id', '20')
        deletion.set(f'{{{W}}}author', 'Table Revision Author')
        props.append(deletion); row.insert(0, props)
        deleted_row = package_edit(source, replacements={'word/document.xml': etree.tostring(root)})
        output, counts = clean_docx(deleted_row, True, ['Author'])
        self.assertEqual(len(Document(BytesIO(output)).tables[0].rows), 1)
        self.assertGreater(counts['revisions'], 0)
        self.assert_package_integrity(output)
        for kind in ('cell', 'paragraph'):
            root = etree.fromstring(original)
            if kind == 'cell':
                node = OxmlElement('w:cellDel')
                root.find(f'.//{{{W}}}tcPr').append(node)
            else:
                ppr = OxmlElement('w:pPr'); rpr = OxmlElement('w:rPr')
                deletion = OxmlElement('w:del'); deletion.set(f'{{{W}}}id', '21')
                rpr.append(deletion); ppr.append(rpr)
                root.find(f'.//{{{W}}}p').insert(0, ppr)
            complex_revision = package_edit(source, replacements={'word/document.xml': etree.tostring(root)})
            with self.subTest(kind=kind), self.assertRaises(InvalidDocument):
                clean_docx(complex_revision, True, ['Author'])

    def test_modern_comment_parts_and_relationships_are_removed(self):
        source = fixture()
        with ZipFile(BytesIO(source)) as z:
            rels = etree.fromstring(z.read('word/_rels/document.xml.rels'))
            types = etree.fromstring(z.read('[Content_Types].xml'))
        additions = {}
        for i, name in enumerate(('commentsExtended', 'commentsIds', 'commentsExtensible', 'people'), start=50):
            filename = f'word/{name}.xml'
            additions[filename] = b'<payload>Private modern comment payload</payload>'
            etree.SubElement(rels, f'{{{REL}}}Relationship', Id=f'rId{i}',
                             Type=f'http://schemas.microsoft.com/office/2011/relationships/{name}',
                             Target=name + '.xml')
            etree.SubElement(types, f'{{{CT}}}Override', PartName='/' + filename, ContentType='application/xml')
        source = package_edit(source, additions=additions, replacements={
            'word/_rels/document.xml.rels': etree.tostring(rels), '[Content_Types].xml': etree.tostring(types),
        })
        output, _ = clean_docx(source, True, ['Author'])
        self.assert_package_integrity(output)
        with ZipFile(BytesIO(output)) as z:
            for filename in additions:
                self.assertNotIn(filename, z.namelist())

    def test_rejects_embedded_objects_signatures_and_dtd(self):
        source = fixture()
        for filename in ('word/embeddings/oleObject1.bin', '_xmlsignatures/sig1.xml'):
            unsupported = package_edit(source, additions={filename: b'unsupported'})
            with self.subTest(filename=filename), self.assertRaises(InvalidDocument):
                clean_docx(unsupported, True, ['Author'])
        with ZipFile(BytesIO(source)) as z:
            root = etree.fromstring(z.read('word/document.xml'))
        xml = b'<!DOCTYPE document [<!ENTITY hidden "payload">]>' + etree.tostring(root)
        dtd = package_edit(source, replacements={'word/document.xml': xml})
        with self.assertRaises(InvalidDocument):
            clean_docx(dtd, True, ['Author'])

    def test_removes_media_used_only_by_hidden_deleted_runs_but_keeps_shared_image(self):
        document = Document(BytesIO(fixture()))
        shared_hidden = document.add_paragraph().add_run()
        shared_hidden.font.hidden = True
        shared_hidden.add_picture(BytesIO(PNG), width=Inches(0.2))
        hidden_png = rgb_png((255, 0, 0))
        hidden = document.add_paragraph().add_run()
        hidden.font.hidden = True
        hidden.add_picture(BytesIO(hidden_png), width=Inches(0.2))
        deleted_png = rgb_png((0, 0, 255))
        deleted_paragraph = document.add_paragraph()
        removed_run = deleted_paragraph.add_run()
        removed_run.add_picture(BytesIO(deleted_png), width=Inches(0.2))
        deleted = OxmlElement('w:del')
        deleted.set(f'{{{W}}}id', '123')
        deleted.set(f'{{{W}}}author', 'Previous Revision Author')
        deleted_paragraph._p.append(deleted)
        deleted.append(removed_run._r)
        saved = BytesIO(); document.save(saved)
        output, _ = clean_docx(saved.getvalue(), True, ['Author'])
        self.assert_package_integrity(output)
        with ZipFile(BytesIO(output)) as z:
            media = [z.read(name) for name in z.namelist() if name.startswith('word/media/')]
        self.assertIn(PNG, media)
        self.assertNotIn(hidden_png, media)
        self.assertNotIn(deleted_png, media)
        self.assertEqual(len(Document(BytesIO(output)).inline_shapes), 1)


if __name__ == '__main__':
    unittest.main()
