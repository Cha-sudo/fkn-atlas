import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fkn_atlas.storage import Store
from fkn_atlas.crawler import run_demo

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

class DemoTests(unittest.TestCase):
    def test_demo_provenance_idempotent(self):
        with TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "test.sqlite"))
            try:
                first = run_demo(store, FIXTURES)
                second = run_demo(store, FIXTURES)
                self.assertEqual(first["link_occurrences"], second["link_occurrences"])
                self.assertEqual(first["pages_by_state"].get("fetched"), 3)
                self.assertGreaterEqual(first["providers"].get("youtube", 0), 1)
                links = store.db.execute("SELECT source_url,anchor_text,context FROM link_occurrences WHERE provider='youtube'").fetchall()
                self.assertEqual(len(links), 1)
                self.assertIn("Семинар", str(store.db.execute("SELECT context FROM link_occurrences WHERE provider='direct_file'").fetchone()[0]))
            finally:
                store.close()

    def test_crash_recovery(self):
        with TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "test.sqlite"))
            try:
                store.enqueue("https://fixture.local/wiki/Test", 0)
                job = store.next_url()
                self.assertIsNotNone(job)
                store.reset_unfinished()
                self.assertEqual(store.next_url()[0], job[0])
            finally:
                store.close()

if __name__ == "__main__": unittest.main()
