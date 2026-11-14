"""Access restrictions must not masquerade as transient download failures."""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fkn_atlas.crawler import FetchFailure
from fkn_atlas.resources import Catalog
from fkn_atlas.storage import Store


class AccessStateTests(unittest.TestCase):
    def test_robots_denial_blocks_without_claiming_transient_access(self):
        with TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'test.sqlite'))
            try:
                catalog = Catalog(store)
                resource_id = catalog.upsert('yandex_disk', 'https://disk.yandex.ru/d/example',
                    {'path': '/lecture.pdf', 'name': 'lecture.pdf', 'type': 'file'})
                store.db.execute('''INSERT INTO resource_jobs
                    (resource_id,stage,state,attempts,updated_at)
                    VALUES (?,'content','running',1,'test')''', (resource_id,))
                store.db.commit()

                catalog.failure(resource_id, 'content',
                    FetchFailure('Disallowed by robots', retryable=False, state='robots_denied'))

                resource = catalog.get(resource_id)
                self.assertEqual(resource['access'], 'platform_restricted')
                self.assertEqual(resource['content_state'], 'metadata_only')
                job = store.db.execute('''SELECT state,not_before,last_error FROM resource_jobs
                    WHERE resource_id=? AND stage='content' ''', (resource_id,)).fetchone()
                self.assertEqual(tuple(job), ('blocked', 0, 'Disallowed by robots'))
                self.assertEqual(catalog.stats()['downloaded_files'], 0)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
