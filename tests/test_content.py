import io
import json
from pathlib import Path
import stat
import struct
import sys
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED, ZIP_STORED
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fkn_atlas.content import inspect_content, MAX_TEXT_BYTES


def zip_bytes(entries, compression=ZIP_STORED):
    out = io.BytesIO()
    with ZipFile(out, 'w', compression=compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return out.getvalue()


class ContentTests(unittest.TestCase):
    def test_text_is_extracted_but_not_analyzed(self):
        result = inspect_content('Лекция: линейные пространства'.encode(), 'lecture.txt')
        self.assertEqual(result['state'], 'text_extracted')
        self.assertIn('линейные пространства', result['text'])
        self.assertNotEqual(result['state'], 'content_analyzed')

    def test_text_byte_limit_handles_multibyte(self):
        result = inspect_content(('я' * MAX_TEXT_BYTES).encode(), 'large.md')
        self.assertLessEqual(len(result['text'].encode()), MAX_TEXT_BYTES)
        self.assertTrue(result['metadata']['text_truncated'])

    def test_binary_and_unknown_encoding_not_guessed(self):
        self.assertEqual(inspect_content(b'a\x00b', 'data.txt')['state'], 'retrieved')
        self.assertEqual(inspect_content(b'\xff\xfeinvalid', 'data.txt')['state'], 'retrieved')

    def test_notebook_ignores_executed_outputs(self):
        raw = json.dumps({'cells': [{'cell_type': 'code', 'source': ['print(1)'], 'outputs': [{'text': 'SECRET OUTPUT'}]},
                                   {'cell_type': 'markdown', 'source': 'Theory'}]}).encode()
        result = inspect_content(raw, 'lesson.ipynb')
        self.assertIn('print(1)', result['text'])
        self.assertNotIn('SECRET OUTPUT', result['text'])
        self.assertFalse(result['metadata']['code_executed'])

    def test_bad_notebook_stays_retrieved(self):
        self.assertEqual(inspect_content(b'{broken', 'bad.ipynb')['state'], 'retrieved')

    def test_deep_notebook_json_does_not_crash_pipeline(self):
        result = inspect_content(b'[' * 10000 + b']' * 10000, 'deep.ipynb')
        self.assertEqual(result['state'], 'retrieved')
        self.assertEqual(result['metadata']['reason'], 'invalid_notebook')

    def test_valid_archive_preserves_member_bytes_and_provenance_fields(self):
        raw = zip_bytes([('course/lecture.txt', 'Множества'.encode()), ('nested.zip', b'not opened'), ('setup.exe', b'not executed')])
        result = inspect_content(raw, 'materials.zip')
        self.assertEqual(result['state'], 'archive_inspected')
        self.assertEqual(result['metadata']['members_read'], 1)
        first, nested, executable = result['members']
        self.assertEqual(first['body'], 'Множества'.encode())
        self.assertIsNotNone(first['sha256'])
        self.assertNotIn('body', nested)
        self.assertNotIn('body', executable)
        self.assertFalse(result['metadata']['code_executed'])

    def test_unsafe_paths_rejected_before_any_member_read(self):
        for path in ('../bad.txt', '/absolute.txt', 'C:/bad.txt', 'a\\bad.txt', 'a/./bad.txt', 'a//bad.txt',
                     'a./bad.txt', 'file.txt:stream', 'NUL.txt', 'folder/CON'):
            with self.subTest(path=path):
                result = inspect_content(zip_bytes([('safe.txt', b'safe'), (path, b'bad')]), 'unsafe.zip')
                self.assertTrue(result['metadata']['archive_rejected'])
                self.assertEqual(result['members'], [])

    def test_duplicate_windows_and_unicode_paths_rejected(self):
        for first, second in [('A.txt', 'a.txt'), ('é.txt', 'e\u0301.txt')]:
            result = inspect_content(zip_bytes([(first, b'a'), (second, b'b')]), 'duplicate.zip')
            self.assertEqual(result['metadata']['reason'], 'duplicate_member_path')

    def test_nul_in_original_zip_name_rejected(self):
        raw = zip_bytes([('abc.txt', b'data')]).replace(b'abc.txt', b'a\x00c.txt')
        result = inspect_content(raw, 'nul.zip')
        self.assertEqual(result['metadata']['reason'], 'unsafe_member_path')

    def test_symlink_rejected(self):
        info = ZipInfo('link.txt')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        result = inspect_content(zip_bytes([(info, b'/etc/passwd')]), 'symlink.zip')
        self.assertEqual(result['metadata']['reason'], 'non_regular_member')

    def test_encrypted_member_rejected_before_read(self):
        raw = bytearray(zip_bytes([('secret.txt', b'not actually encrypted')]))
        local = raw.index(b'PK\x03\x04')
        central = raw.index(b'PK\x01\x02')
        struct.pack_into('<H', raw, local + 6, 1)
        struct.pack_into('<H', raw, central + 8, 1)
        result = inspect_content(bytes(raw), 'encrypted.zip')
        self.assertEqual(result['state'], 'rejected')
        self.assertEqual(result['metadata']['reason'], 'encrypted_member')
        self.assertEqual(result['members'], [])

    def test_compression_bomb_rejected(self):
        result = inspect_content(zip_bytes([('bomb.txt', b'a' * 100000)], ZIP_DEFLATED), 'bomb.zip')
        self.assertEqual(result['metadata']['reason'], 'compression_ratio_limit')

    def test_member_and_total_limits_preflight(self):
        raw = zip_bytes([('one.txt', b'1234'), ('two.txt', b'5678')])
        with patch('fkn_atlas.content.MAX_MEMBER_BYTES', 3):
            self.assertEqual(inspect_content(raw, 'a.zip')['metadata']['reason'], 'member_size_limit')
        with patch('fkn_atlas.content.MAX_EXPANDED_BYTES', 7):
            self.assertEqual(inspect_content(raw, 'a.zip')['metadata']['reason'], 'expanded_size_limit')
        with patch('fkn_atlas.content.MAX_MEMBERS', 1):
            self.assertEqual(inspect_content(raw, 'a.zip')['metadata']['reason'], 'too_many_members')

    def test_corrupt_zip_does_not_return_partial_members(self):
        raw = zip_bytes([('first.txt', b'ok first'), ('second.txt', b'unique body')])
        raw = raw.replace(b'unique body', b'corruptbody')
        result = inspect_content(raw, 'bad.zip')
        self.assertEqual(result['members'], [])
        self.assertEqual(result['metadata']['reason'], 'invalid_zip')

    def test_corrupt_deflate_is_rejected_without_partial_members(self):
        for name, member in [('bad.zip', 'lesson.txt'), ('bad.docx', 'word/document.xml')]:
            with self.subTest(name=name):
                raw = bytearray(zip_bytes([(member, b'some educational text')], ZIP_DEFLATED))
                filename_size, extra_size = struct.unpack_from('<HH', raw, 26)
                raw[30 + filename_size + extra_size] = 0xff  # Reserved DEFLATE block type.
                result = inspect_content(bytes(raw), name)
                self.assertIn(result['state'], ('retrieved', 'rejected'))
                self.assertEqual(result['text'], '')
                self.assertEqual(result['members'], [])

    def test_docx_text_and_xml_entities(self):
        ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
        xml = f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>Lecture</w:t></w:r></w:p></w:body></w:document>'.encode()
        result = inspect_content(zip_bytes([('word/document.xml', xml)]), 'lecture.docx')
        self.assertEqual(result['text'], 'Lecture')
        entity = b'<!DOCTYPE x [<!ENTITY e "expanded">]><x>&e;</x>'
        result = inspect_content(zip_bytes([('word/document.xml', entity)]), 'bad.docx')
        self.assertEqual(result['metadata']['reason'], 'xml_entities_not_allowed')

    def test_docx_utf16_entity_declaration_is_rejected(self):
        xml = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY e "expanded">]><x>&e;</x>'
        raw = zip_bytes([('word/document.xml', xml.encode('utf-16'))])
        result = inspect_content(raw, 'encoded.docx')
        self.assertEqual(result['state'], 'rejected')
        self.assertEqual(result['metadata']['reason'], 'xml_entities_not_allowed')
        self.assertEqual(result['text'], '')

    def test_pdf_optional_dependency_and_bad_pdf(self):
        from importlib.metadata import PackageNotFoundError
        with patch('fkn_atlas.content.package_metadata.version', side_effect=PackageNotFoundError):
            self.assertEqual(inspect_content(b'%PDF', 'x.pdf')['state'], 'extractor_unavailable')
        try:
            import pypdf
        except ImportError:
            self.skipTest('optional pypdf absent')
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=100, height=100)
        out = io.BytesIO()
        writer.write(out)
        result = inspect_content(out.getvalue(), 'blank.pdf')
        self.assertEqual(result['metadata']['pages_total'], 1)
        self.assertEqual(result['metadata']['pages_read'], 1)
        self.assertFalse(result['metadata']['ocr_performed'])
        self.assertEqual(result['state'], 'retrieved')

    def test_pdf_page_limit_is_reported(self):
        try:
            import pypdf
        except ImportError:
            self.skipTest('optional pypdf absent')
        writer = pypdf.PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=100, height=100)
        out = io.BytesIO()
        writer.write(out)
        with patch('fkn_atlas.content.MAX_PDF_PAGES', 2):
            result = inspect_content(out.getvalue(), 'partial.pdf')
        self.assertEqual(result['metadata']['pages_read'], 2)
        self.assertEqual(result['metadata']['pages_total'], 3)
        self.assertTrue(result['metadata']['page_limit_reached'])

    def test_unknown_or_executable_is_not_read_as_text(self):
        result = inspect_content(b'print("not run")', 'script.exe')
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(result['text'], '')


if __name__ == '__main__':
    unittest.main()
