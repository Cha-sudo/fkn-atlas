"""Failure-oriented checks for evidence, resume and network boundaries (offline)."""
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from email.message import Message
from urllib.request import Request
from urllib.error import HTTPError
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fkn_atlas.crawler import (Fetcher, FetchFailure, FetchResult, RestrictRedirect,
    SEEDS, ROBOTS_URL, process_html, retry_after_seconds, run_scout, run_demo)
from fkn_atlas.extract import extract_links
from fkn_atlas.storage import Store
from fkn_atlas.urls import normalize_url, in_wiki_scope


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "test.sqlite"))
        self.url = SEEDS[0]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def process(self, html, **kwargs):
        return process_html(self.store, self.url, html, 0, "fruitdet.ru", 2, **kwargs)

    def test_duplicate_occurrences_and_snapshot_idempotency(self):
        html = '<h2>ПМИ</h2><p><a href="/wiki/A">A</a><a href="/wiki/A">A</a></p>'
        self.process(html)
        self.process(html)
        self.assertEqual(self.store.stats()["link_occurrences"], 2)
        self.assertEqual(self.store.stats()["snapshots"], 1)
        self.assertEqual(self.store.stats()["events"], 2)

    def test_versions_and_failed_attempt_preserve_originals(self):
        for html in ('<a href="/wiki/A">A</a>', '<a href="/wiki/B">B</a>'):
            self.process(html)
        sha = self.store.db.execute("SELECT sha256 FROM pages").fetchone()[0]
        self.store.record_page(self.url, "", "http_error", 502, None, "HTTP 502")
        self.store.event(self.url, "page", "http_error", 502, body=b"upstream unavailable")
        self.assertEqual(self.store.db.execute("SELECT sha256 FROM pages").fetchone()[0], sha)
        self.assertEqual(self.store.stats()["snapshots"], 2)
        self.assertEqual(self.store.stats()["events"], 3)
        for digest, path in self.store.db.execute("SELECT sha256,path FROM blobs"):
            self.assertEqual(hashlib.sha256((self.store.path.parent / path).read_bytes()).hexdigest(), digest)

    def test_original_bytes_not_reencoded_text(self):
        raw = '<title>Лекция</title>'.encode('cp1251')
        self.process(raw.decode('cp1251'), raw=raw, encoding='cp1251')
        self.assertEqual(self.store.db.execute("SELECT sha256 FROM snapshots").fetchone()[0], hashlib.sha256(raw).hexdigest())

    def test_failed_transaction_leaves_no_partial_page(self):
        self.store.enqueue(self.url, 0)
        self.store.next_url()
        with patch.object(self.store, "record_link", side_effect=RuntimeError("interruption")):
            with self.assertRaises(RuntimeError):
                self.process('<a href="/wiki/A">A</a>')
        self.assertEqual(self.store.stats()["snapshots"], 0)
        self.assertEqual(self.store.stats()["pages_by_state"], {})
        self.assertEqual(self.store.stats()["queue_by_state"], {"in_progress": 1})
        self.store.reset_unfinished()
        self.assertEqual(self.store.next_url()[0], self.url)

    def test_third_interrupted_attempt_is_exhausted(self):
        self.store.enqueue(self.url, 0)
        for _ in range(3):
            self.assertIsNotNone(self.store.next_url())
            self.store.reset_unfinished()
        self.assertIsNone(self.store.next_url())
        self.assertEqual(self.store.stats()["queue_by_state"], {"exhausted": 1})

    def test_minimum_depth_reopens_completed_page(self):
        self.store.enqueue(self.url, 3)
        self.store.mark_done(self.url)
        self.store.enqueue(self.url, 0)
        self.assertEqual(self.store.next_url(), (self.url, 0))

    def test_retry_deadline_survives_reopen(self):
        self.store.enqueue(self.url, 0)
        self.store.next_url()
        self.store.mark_failed(self.url, "429", retry_after=600)
        self.store.close()
        self.store = Store(str(Path(self.tmp.name) / "test.sqlite"))
        self.store.reset_unfinished()
        self.assertIsNone(self.store.next_url())
        self.assertGreater(self.store.db.execute("SELECT not_before FROM queue").fetchone()[0], time.time()+590)

    def test_final_url_and_html_base_control_relative_links(self):
        self.process('<base href="../courses/"><a href="LA">ЛА</a>', final_url="https://fruitdet.ru/wiki/new/index")
        self.assertEqual(self.store.db.execute("SELECT target_url FROM link_occurrences").fetchone()[0], "https://fruitdet.ru/wiki/courses/LA")

    def test_modes_cannot_mix(self):
        self.store.bind_mode("demo")
        with self.assertRaises(ValueError):
            run_scout(self.store, fetcher=FakeFetcher())

    def test_robots_failure_records_evidence_before_jobs(self):
        f = FakeFetcher(robots_failure=FetchFailure("HTTP 503", status=503, body=b"unavailable"))
        report = run_scout(self.store, fetcher=f)
        self.assertEqual(report["run_status"], "robots_unavailable")
        self.assertEqual(report["queue_by_state"], {"queued": 2})
        self.assertEqual(f.requested, [])
        self.assertEqual(report["blobs"], 1)
        second = run_scout(self.store, fetcher=f)
        self.assertEqual(second["run_status"], "host_backoff")
        self.assertEqual(f.robots_calls, 1)

    def test_scout_two_runs_resume_and_preserve_occurrences(self):
        f = FakeFetcher()
        one = run_scout(self.store, max_pages=1, fetcher=f)
        self.assertEqual(one["attempted_this_run"], 1)
        two = run_scout(self.store, max_pages=2, fetcher=f)
        self.assertEqual(two["pages_by_state"], {"fetched": 3})
        self.assertEqual(f.requested.count(SEEDS[0]), 1)
        self.assertEqual(two["queue_by_state"], {"done": 3})
        self.assertEqual(run_scout(self.store, fetcher=f)["run_status"], "no_due_jobs")

    def test_rate_limit_stops_host_and_preserves_retry_after(self):
        f = FakeFetcher(failure=FetchFailure("HTTP 429", status=429, retry_after=600, body=b"slow down"))
        r = run_scout(self.store, fetcher=f)
        self.assertEqual(r["attempted_this_run"], 1)
        self.assertEqual(len(f.requested), 1)
        self.assertGreater(float(self.store.setting("host_not_before")), time.time()+590)
        self.assertEqual(run_scout(self.store, fetcher=f)["run_status"], "host_backoff")

    def test_terminal_http_failure_not_retried(self):
        f = FakeFetcher(failure=FetchFailure("HTTP 404", status=404, retryable=False))
        run_scout(self.store, fetcher=f)
        self.assertEqual(self.store.stats()["queue_by_state"], {"blocked": 2})
        self.assertEqual(run_scout(self.store, fetcher=f)["attempted_this_run"], 0)

    def test_exclusion_reasons_are_preserved(self):
        self.process('<a href="/wiki/A?action=delete">edit</a><a href="https://x.test/?token=secret">signed</a>')
        self.assertEqual(self.store.stats()["crawl_exclusions"], {"wiki_query_not_crawled": 1})
        self.assertEqual(self.store.stats()["excluded_links"], {"invalid_unsupported_or_sensitive_url": 1})
        self.assertEqual(self.store.stats()["queue_by_state"], {})

    def test_jsonl_export_keeps_provenance(self):
        self.process('<a href="/wiki/A">A</a>')
        target = Path(self.tmp.name) / "catalog.jsonl"
        self.store.export(str(target))
        rows = [json.loads(x) for x in target.read_text().splitlines()]
        self.assertTrue(any(x["table"] == "link_occurrences" and x["snapshot_id"] for x in rows))
        with self.assertRaises(ValueError):
            self.store.export(str(self.store.path))

    def test_same_blob_does_not_merge_different_sources(self):
        html = '<a href="https://youtu.be/abc">Lecture</a>'
        self.process(html)
        process_html(self.store, SEEDS[1], html, 0, "fruitdet.ru", 2)
        self.assertEqual(self.store.stats()["blobs"], 1)
        self.assertEqual(self.store.stats()["snapshots"], 2)
        self.assertEqual(self.store.stats()["link_occurrences"], 2)

    def test_interrupted_blob_publication_can_be_retried(self):
        body = b"original evidence"
        digest = hashlib.sha256(body).hexdigest()
        with patch('fkn_atlas.storage.os.link', side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError):
                self.store.save_blob(body)
        self.assertFalse((self.store.blob_dir / digest[:2] / digest).exists())
        self.assertEqual(self.store.save_blob(body), digest)

    def test_failed_blob_write_does_not_publish_partial_bytes(self):
        with patch('fkn_atlas.storage.os.fsync', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.store.save_blob(b'original')
        self.assertEqual(list(self.store.blob_dir.rglob('.pending-*')), [])
        self.assertEqual(self.store.stats()['blobs'], 0)

    def test_partial_legacy_recovery_does_not_lose_colliding_ids(self):
        self.process('<a href="/wiki/A">New</a>')
        self.store.db.execute('''CREATE TABLE legacy_link_occurrences AS
            SELECT id,source_url,target_url,'Old' AS anchor_text,context,provider,internal,observed_at
            FROM link_occurrences''')
        self.store.db.commit()
        self.store.close()
        self.store = Store(str(Path(self.tmp.name) / 'test.sqlite'))
        self.assertEqual(self.store.stats()['link_occurrences'], 2)
        self.assertEqual(self.store.stats()['legacy_occurrences_without_snapshot'], 1)


class FakeFetcher:
    robot_status = "absent_404"
    redirects = []

    def __init__(self, robots_failure=None, failure=None):
        self.robots_failure, self.failure = robots_failure, failure
        self.requested = []
        self.robots_calls = 0

    def load_robots(self):
        self.robots_calls += 1
        if self.robots_failure:
            raise self.robots_failure
        return FetchResult(b"not found", ROBOTS_URL, 404)

    def get_page(self, url):
        self.requested.append(url)
        if self.failure:
            raise self.failure
        return FetchResult(b'<title>Fixture</title><a href="/wiki/Course">Course</a>', url)


class BoundaryTests(unittest.TestCase):
    def test_reserved_url_escapes_and_query_order(self):
        self.assertEqual(normalize_url("https://e.test/a%2fb/%7E?q=%2F&x=2&x=1"), "https://e.test/a%2Fb/~?q=%2F&x=2&x=1")
        self.assertIsNone(normalize_url("https://e.test/file?X-Amz-Signature=private"))
        self.assertIsNone(normalize_url("https://e.test:bad/file"))

    def test_scope_rejects_actions_traversal_ports_and_files(self):
        for path in ("https://fruitdet.ru:8443/wiki/A", "https://fruitdet.ru/wiki/%2e%2e/private", "https://fruitdet.ru/wiki/A?action=delete", "https://fruitdet.ru/wiki/%53pecial:Login", "https://fruitdet.ru/wiki/x.pdf"):
            with self.subTest(path=path):
                self.assertFalse(in_wiki_scope(path))

    def test_heading_hierarchy_and_list_context(self):
        page = extract_links('<h2>ПМИ</h2><h3>2023</h3><li>Лекция 2 <a href="/a">видео</a> продолжение</li>')
        self.assertIn("ПМИ | 2023 | Лекция 2 видео продолжение", page.links[0].context)

    def test_redirect_checks_target_robots_before_request(self):
        f = Fetcher()
        f.robot_status = "parsed"
        f.robot.parse(["User-agent: *", "Disallow: /wiki/Private"])
        handler = RestrictRedirect(f)
        with self.assertRaises(FetchFailure) as caught:
            handler.redirect_request(Request(SEEDS[0]), None, 302, "", {}, "https://fruitdet.ru/wiki/Private")
        self.assertEqual(caught.exception.state, "robots_denied")
        with self.assertRaises(FetchFailure):
            handler.redirect_request(Request(SEEDS[0]), None, 302, "", {}, "https://evil.test/wiki/A")

    def test_robots_denial_has_no_previous_redirect_chain(self):
        f = Fetcher()
        f.robot_status = 'parsed'
        f.robot.parse(['User-agent: *', 'Disallow: /wiki/'])
        f.redirects = [{'from': 'old', 'to': 'old'}]
        with self.assertRaises(FetchFailure):
            f.get_page(SEEDS[0])
        self.assertEqual(f.redirects, [])

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(retry_after_seconds("120"), 120)
        self.assertEqual(retry_after_seconds("Thu, 01 Jan 1970 00:02:00 GMT", now=60), 60)
        self.assertEqual(retry_after_seconds("bad"), 0)

    def test_non_html_and_partial_responses_are_not_parsed(self):
        f = Fetcher()
        f.robot_status = "absent_404"
        for result in (FetchResult(b"pdf", SEEDS[0], content_type="application/pdf"), FetchResult(b"partial", SEEDS[0], status=206)):
            with patch.object(f, "request", return_value=result), self.assertRaises(FetchFailure):
                f.get_page(SEEDS[0])

    def test_response_byte_limit_and_http_error_body(self):
        class Response(io.BytesIO):
            status = 200
            headers = Message()
            headers["Content-Type"] = "text/html"
            def geturl(self): return SEEDS[0]
        f = Fetcher()
        with patch.object(f, "wait"), patch.object(f.opener, "open", return_value=Response(b"12345")):
            with self.assertRaises(FetchFailure) as caught:
                f.request(SEEDS[0], 4)
            self.assertEqual(caught.exception.state, "too_large")
        headers = Message()
        headers['Retry-After'] = '90'
        error = HTTPError(SEEDS[0], 429, "too many", headers, io.BytesIO(b"slow"))
        with patch.object(f, "wait"), patch.object(f.opener, "open", side_effect=error):
            with self.assertRaises(FetchFailure) as caught:
                f.request(SEEDS[0], 100)
            self.assertEqual(caught.exception.body, b"slow")
            self.assertEqual(caught.exception.retry_after, 90)

    def test_robots_crawl_delay_and_404(self):
        f = Fetcher()
        with patch.object(f, "request", return_value=FetchResult(b"User-agent: *\nCrawl-delay: 7\n", ROBOTS_URL, content_type="text/plain")):
            f.load_robots()
            self.assertEqual(f.delay, 7)
        with patch.object(f, "request", side_effect=FetchFailure("404", status=404, body=b"missing")):
            self.assertEqual(f.load_robots().status, 404)
            self.assertEqual(f.robot_status, "absent_404")

    def test_invalid_limits_rejected(self):
        with TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / 'a.sqlite'))
            try:
                for kw in ({"max_pages": 13}, {"delay": float('nan')}, {"max_depth": 5}):
                    with self.assertRaises(ValueError): run_scout(store, **kw)
            finally:
                store.close()


class MigrationTests(unittest.TestCase):
    def test_v1_records_survive_without_invented_snapshots(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite"
            db = sqlite3.connect(path)
            db.executescript('''
                CREATE TABLE pages(url TEXT PRIMARY KEY,title TEXT NOT NULL DEFAULT '',state TEXT NOT NULL DEFAULT 'unknown',http_status INTEGER,sha256 TEXT,last_error TEXT,checked_at TEXT NOT NULL);
                CREATE TABLE link_occurrences(id INTEGER PRIMARY KEY,source_url TEXT NOT NULL REFERENCES pages(url),target_url TEXT NOT NULL,anchor_text TEXT NOT NULL,context TEXT NOT NULL,provider TEXT NOT NULL,internal INTEGER NOT NULL,observed_at TEXT NOT NULL,UNIQUE(source_url,target_url,anchor_text,context));
                CREATE TABLE queue(url TEXT PRIMARY KEY,depth INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'queued',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,updated_at TEXT NOT NULL);
                INSERT INTO pages VALUES ('https://fixture.local/wiki/A','Old','fetched',200,'old-sha',NULL,'2026-01-01');
                INSERT INTO link_occurrences VALUES (1,'https://fixture.local/wiki/A','https://e.test/','link','context','web',0,'2026-01-01');
            ''')
            db.close()
            original_connect = sqlite3.connect
            def interrupt_migration(*args, **kwargs):
                connection = original_connect(*args, **kwargs)
                connection.set_authorizer(lambda action, table, *rest:
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == 'link_occurrences' else sqlite3.SQLITE_OK)
                return connection
            with patch('fkn_atlas.storage.sqlite3.connect', side_effect=interrupt_migration):
                with self.assertRaises(sqlite3.DatabaseError):
                    Store(str(path))
            check = original_connect(path)
            self.assertEqual(check.execute('SELECT COUNT(*) FROM link_occurrences').fetchone()[0], 1)
            self.assertNotIn('snapshot_id', [r[1] for r in check.execute('PRAGMA table_info(link_occurrences)')])
            check.close()
            store = Store(str(path))
            self.assertEqual(store.stats()["legacy_occurrences_without_snapshot"], 1)
            self.assertEqual(store.stats()["snapshots"], 0)
            store.close()
            store = Store(str(path))
            self.assertEqual(store.stats()["link_occurrences"], 1)
            store.close()


if __name__ == '__main__':
    unittest.main()
