"""A reviewed text derivative must keep both its source and extraction evidence."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from fkn_atlas.storage import Store, utc_now
from fkn_atlas.resources import Catalog

spec = importlib.util.spec_from_file_location('reviewed_text_map', ROOT / 'scripts/reviewed_text_map.py')
mapping_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mapping_tool)


class ReviewedMapTest(unittest.TestCase):
    def test_new_derivative_requires_original_link_and_preserves_raw_text(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(str(Path(temp) / 'data.sqlite'))
            self.addCleanup(store.close)
            raw = 'à × A'.encode('utf8')
            sha = store.save_blob(raw)
            rid = Catalog(store).upsert('fixture', 'https://fixture.local/a',
                {'path': '/', 'type': 'file', 'name': 'a.pdf'})
            with self.assertRaisesRegex(ValueError, 'belong'):
                mapping_tool.derive(store, rid, sha, sha, {'à': 'а'}, 'test')
            store.db.execute('INSERT INTO resource_contents VALUES (?,?,?,?,?)',
                (rid, sha, 'application/pdf', None, utc_now()))
            store.db.execute('INSERT INTO derived_artifacts VALUES (?,?,?,?,?,?,?)',
                (rid, sha, sha, 'fixture', 'text_extracted', '{}', utc_now()))
            store.db.commit()
            result = mapping_tool.derive(store, rid, sha, sha, {'à': 'а'}, 'test')
            self.assertNotEqual(result, sha)
            self.assertEqual(mapping_tool.Reconciler(store)._text(result), 'а × A')
            self.assertEqual(mapping_tool.Reconciler(store)._blob(sha), raw)
            self.assertEqual(mapping_tool.derive(store, rid, sha, sha, {'à': 'а'}, 'test'), result)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM derived_artifacts').fetchone()[0], 2)
