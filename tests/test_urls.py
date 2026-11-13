import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fkn_atlas.urls import normalize_url, in_wiki_scope, provider_for

class UrlTests(unittest.TestCase):
    def test_relative_and_fragment(self):
        self.assertEqual(normalize_url("/wiki/Test#part", "https://fruitdet.ru/wiki/Start"), "https://fruitdet.ru/wiki/Test")
    def test_no_secrets_or_js(self):
        self.assertIsNone(normalize_url("javascript:alert(1)"))
        self.assertIsNone(normalize_url("https://user:pass@example.org/x"))
    def test_tracking_only_removed(self):
        self.assertEqual(normalize_url("https://youtu.be/mROFPH8WKZg?si=x&utm_source=y&t=10"),
                         "https://youtu.be/mROFPH8WKZg?t=10")
    def test_scope(self):
        self.assertTrue(in_wiki_scope("https://fruitdet.ru/wiki/Wiki_%D0%A4%D0%9A%D0%9D"))
        self.assertFalse(in_wiki_scope("https://evilfruitdet.ru/wiki/Start"))
        self.assertFalse(in_wiki_scope("https://fruitdet.ru/not-wiki"))
    def test_providers(self):
        self.assertEqual(provider_for("https://disk.yandex.ru/d/lwE14jdmI4qZEQ"), "yandex_disk")
        self.assertEqual(provider_for("https://youtu.be/mROFPH8WKZg"), "youtube")
        self.assertEqual(provider_for("https://example.org/doc.pdf"), "direct_file")

if __name__ == "__main__": unittest.main()
