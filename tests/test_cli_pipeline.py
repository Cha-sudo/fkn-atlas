"""One offline command-line round trip for the local research workflow."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fkn_atlas.cli import main
from fkn_atlas.crawler import process_html
from fkn_atlas.storage import Store


class CliPipelineTest(unittest.TestCase):
    def test_local_file_dossier_candidates_export_and_missing_database(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            missing = root / 'typo.sqlite'
            for action, extra in [('reconcile-report', []), ('reconcile-import', ['--dossier', 'none.json']),
                                  ('reconcile-candidates', ['--material-id', 'absent'])]:
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main([action, '--db', str(missing), *extra])
                self.assertEqual(error.exception.code, 2)
                self.assertFalse(missing.exists())

            db = root / 'research.sqlite'
            page, target = 'https://example.org/course', 'https://example.org/notes.txt'
            raw = ('<h1>PMI 2023/2024 and 2024/2025</h1><a href="' + target + '">notes</a>').encode()
            store = Store(str(db))
            process_html(store, page, raw.decode(), 0, 'example.org', 0, raw=raw)
            page_sha = store.db.execute('SELECT sha256 FROM pages WHERE url=?', (page,)).fetchone()[0]
            store.close()
            source = root / 'notes.txt'
            source.write_text('Vectors form a basis.', encoding='utf-8')

            def command(*args):
                output = io.StringIO()
                with redirect_stdout(output):
                    main([*args, '--db', str(db)])
                return json.loads(output.getvalue())

            imported = command('import-file', '--filename', str(source), '--source-url', target, '--discovered-on', page)
            self.assertEqual(imported['content_state'], 'text_extracted')
            span = {'sha256': imported['sha256'], 'start': 0, 'end': len(source.read_text()), 'quote': source.read_text()}
            versions = [{'id': year, 'title': 'Linear algebra', 'academic_year': year,
                         'program': 'PMI', 'stream': 'main', 'evidence': [
                             {'sha256': page_sha, 'start': 0, 'end': len(raw.decode()), 'quote': raw.decode()}]}
                        for year in ('2023/2024', '2024/2025')]
            materials = [{'id': name, 'version_id': version['id'], 'resource_id': imported['resource_id'],
                          'input_sha256': imported['sha256'], 'text_sha256': imported['sha256'],
                          'title': 'Notes', 'kind': 'notes', 'source_url': target,
                          'review': {'reviewer': 'test', 'summary': 'Synthetic complete text read.', 'evidence': [span]}}
                         for name, version in zip(('old', 'new'), versions)]
            dossier = root / 'dossier.json'
            dossier.write_text(json.dumps({'id': 'test-dossier', 'course': 'linear-algebra', 'versions': versions,
                                           'materials': materials, 'gaps': [], 'comparisons': []}), encoding='utf-8')
            imported_dossier = command('reconcile-import', '--dossier', str(dossier))
            self.assertEqual(imported_dossier, command('reconcile-report', '--dossier-id', 'test-dossier'))
            candidates = command('reconcile-candidates', '--material-id', 'old', '--limit', '1')
            self.assertEqual(candidates[0]['material_id'], 'new')
            self.assertEqual(candidates[0]['state'], 'candidate')
            export = root / 'export.jsonl'
            command('export', '--output', str(export))
            tables = {json.loads(line)['table'] for line in export.read_text(encoding='utf-8').splitlines()}
            self.assertTrue({'recon_versions', 'recon_materials', 'recon_dossiers'} <= tables)


if __name__ == '__main__':
    unittest.main()
