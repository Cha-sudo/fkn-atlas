import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fkn_atlas.storage import Store
from fkn_atlas.crawler import process_html
from fkn_atlas.ingest import import_local_file
from fkn_atlas.resources import extract_resource


class LocalImportTests(unittest.TestCase):
    def test_requires_discovery_and_validates_bytes_preserving_repeat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(str(root / 'data.sqlite'))
            self.addCleanup(store.close)
            file = root / 'notes.txt'
            file.write_text('Definitions and exercises', encoding='utf8')
            page, target = 'https://example.org/course', 'https://drive.google.com/file/d/example/view'
            with self.assertRaisesRegex(ValueError, 'discovery'):
                import_local_file(store, file, target, page)
            raw = ('<p>Course <a href="' + target + '">notes</a></p>').encode()
            process_html(store, page, raw.decode(), 0, 'example.org', 0, raw=raw)
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                import_local_file(store, file, target, page, expected_sha256='0' * 64)
            with self.assertRaisesRegex(ValueError, 'size'):
                import_local_file(store, file, target, page, expected_size=0)
            result = import_local_file(store, file, target, page)
            self.assertEqual(result['sha256'], hashlib.sha256(file.read_bytes()).hexdigest())
            self.assertEqual(result['content_state'], 'text_extracted')
            self.assertEqual(import_local_file(store, file, target, page), result)
            self.assertEqual(store.db.execute('SELECT count(*) FROM resource_contents').fetchone()[0], 1)
            self.assertEqual(store.db.execute("SELECT count(*) FROM source_events WHERE stage='local_import'").fetchone()[0], 1)
            self.assertEqual(store.db.execute('SELECT access FROM resources').fetchone()[0], 'unknown')

    def test_exact_version_extraction_and_reimport_old_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(str(root / 'data.sqlite'))
            self.addCleanup(store.close)
            file = root / 'notes.txt'
            page, target = 'https://example.org/course', 'https://example.org/notes.txt'
            raw = ('<p><a href="' + target + '">notes</a></p>').encode()
            process_html(store, page, raw.decode(), 0, 'example.org', 0, raw=raw)
            file.write_text('Version one: basis', encoding='utf8')
            first = import_local_file(store, file, target, page)
            file.write_text('Version two: determinant', encoding='utf8')
            second = import_local_file(store, file, target, page)
            rid = first['resource_id']
            self.assertEqual(second['resource_id'], rid)
            self.assertNotEqual(first['sha256'], second['sha256'])
            # Force identical timestamps to reproduce the old nondeterministic selection.
            store.db.execute("UPDATE resource_contents SET retrieved_at='2026-11-14T00:00:00+00:00'")
            store.db.execute('DELETE FROM derived_artifacts WHERE input_sha256=?', (second['sha256'],))
            store.db.commit()
            extract_resource(store, rid, input_sha256=second['sha256'])
            row = store.db.execute('SELECT artifact_sha256 FROM derived_artifacts WHERE resource_id=? AND input_sha256=?',
                                   (rid, second['sha256'])).fetchone()
            self.assertEqual(row[0], second['sha256'])
            with self.assertRaisesRegex(ValueError, 'not been retrieved'):
                extract_resource(store, rid, input_sha256='0'*64)
            file.write_text('Version one: basis', encoding='utf8')
            self.assertEqual(import_local_file(store, file, target, page), first)
            self.assertEqual(store.db.execute('SELECT expected_sha256,content_state FROM resources WHERE id=?', (rid,)).fetchone(),
                             (first['sha256'], 'text_extracted'))
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM resource_contents').fetchone()[0], 2)

    def test_signed_source_is_rejected_before_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(str(Path(temp) / 'data.sqlite'))
            self.addCleanup(store.close)
            with self.assertRaisesRegex(ValueError, 'signed'):
                import_local_file(store, 'not-read.pdf', 'https://example.org/file?signature=SECRET', 'https://example.org/course')
            self.assertNotIn('SECRET', '\n'.join(store.db.iterdump()))
