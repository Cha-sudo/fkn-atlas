import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fkn_atlas.extract import extract_links

class ParserTests(unittest.TestCase):
    def test_heading_and_table_context(self):
        page = extract_links('<title>T</title><h2>ПМИ 2023</h2><table><tr><td>Лекция 7</td><td><a href="/a">Материал</a></td></tr></table>')
        self.assertEqual(page.title, "T")
        self.assertEqual(page.links[0].href, "/a")
        self.assertIn("ПМИ 2023", page.links[0].context)
        self.assertIn("Лекция 7", page.links[0].context)
    def test_ignores_script(self):
        page = extract_links('<script>let a="<a href=\'/bad\'>";</script><a href="/good">ok</a>')
        self.assertEqual([l.href for l in page.links], ["/good"])

if __name__ == "__main__": unittest.main()
