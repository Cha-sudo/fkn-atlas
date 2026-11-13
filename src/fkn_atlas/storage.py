"""SQLite catalog with immutable bytes, versioned occurrences and persistent retries.

Single worker only. A page, its links and its queue checkpoint commit together.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import tempfile
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pages (
 url TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'unknown',
 http_status INTEGER, sha256 TEXT, last_error TEXT, checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blobs (
 sha256 TEXT PRIMARY KEY, bytes INTEGER NOT NULL, path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
 id INTEGER PRIMARY KEY, source_url TEXT NOT NULL REFERENCES pages(url), final_url TEXT NOT NULL,
 sha256 TEXT NOT NULL REFERENCES blobs(sha256), title TEXT NOT NULL,
 http_status INTEGER NOT NULL, content_type TEXT NOT NULL, encoding TEXT NOT NULL,
 extractor TEXT NOT NULL, origin TEXT NOT NULL, observed_at TEXT NOT NULL,
 UNIQUE(source_url,final_url,sha256,extractor)
);
CREATE TABLE IF NOT EXISTS link_occurrences (
 id INTEGER PRIMARY KEY, source_url TEXT NOT NULL REFERENCES pages(url),
 snapshot_id INTEGER REFERENCES snapshots(id), ordinal INTEGER NOT NULL,
 target_url TEXT NOT NULL, raw_href TEXT NOT NULL, anchor_text TEXT NOT NULL,
 context TEXT NOT NULL, provider TEXT NOT NULL, internal INTEGER NOT NULL CHECK(internal IN (0,1)),
 exclusion_reason TEXT, observed_at TEXT NOT NULL,
 UNIQUE(snapshot_id,ordinal)
);
CREATE INDEX IF NOT EXISTS ix_occ_target ON link_occurrences(target_url);
CREATE INDEX IF NOT EXISTS ix_occ_provider ON link_occurrences(provider);
CREATE TABLE IF NOT EXISTS excluded_links (
 snapshot_id INTEGER NOT NULL REFERENCES snapshots(id), ordinal INTEGER NOT NULL,
 reason TEXT NOT NULL, PRIMARY KEY(snapshot_id,ordinal)
);
CREATE TABLE IF NOT EXISTS source_events (
 id INTEGER PRIMARY KEY, url TEXT NOT NULL, stage TEXT NOT NULL, state TEXT NOT NULL,
 http_status INTEGER, error TEXT, sha256 TEXT REFERENCES blobs(sha256),
 snapshot_id INTEGER REFERENCES snapshots(id), details TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue (
 url TEXT PRIMARY KEY, depth INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at TEXT NOT NULL,
 not_before REAL NOT NULL DEFAULT 0
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, filename: str):
        self.path = Path(filename)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_dir = self.path.with_suffix(".blobs")
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self._batch = 0
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 2:
            self.close()
            raise ValueError("Database schema is newer than this application")
        # Preserve all v1 records. They have no raw evidence and stay explicitly legacy.
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(link_occurrences)")}
        migration = []
        if columns and "snapshot_id" not in columns:
            migration.append("ALTER TABLE link_occurrences RENAME TO legacy_link_occurrences;")
        qcolumns = {r[1] for r in self.db.execute("PRAGMA table_info(queue)")}
        if qcolumns and "not_before" not in qcolumns:
            migration.append("ALTER TABLE queue ADD COLUMN not_before REAL NOT NULL DEFAULT 0;")
        migration.append(SCHEMA)
        legacy_exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_link_occurrences'").fetchone()
        if (columns and "snapshot_id" not in columns) or legacy_exists:
            migration.append("""INSERT INTO link_occurrences
                (source_url,snapshot_id,ordinal,target_url,raw_href,anchor_text,context,provider,internal,observed_at)
                SELECT old.source_url,NULL,old.id,old.target_url,old.target_url,old.anchor_text,old.context,old.provider,old.internal,old.observed_at
                FROM legacy_link_occurrences AS old WHERE NOT EXISTS (
                    SELECT 1 FROM link_occurrences AS current WHERE current.snapshot_id IS NULL
                    AND current.ordinal=old.id AND current.source_url=old.source_url
                    AND current.target_url=old.target_url AND current.anchor_text=old.anchor_text
                    AND current.context=old.context);
                DROP TABLE legacy_link_occurrences;""")
        try:
            # executescript commits any existing transaction: BEGIN must be inside the script.
            self.db.executescript("BEGIN IMMEDIATE;\n" + "\n".join(migration) + "\nPRAGMA user_version=2;\nCOMMIT;")
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _commit(self):
        if not self._batch:
            self.db.commit()

    @contextmanager
    def transaction(self):
        self.db.execute("SAVEPOINT page_result")
        self._batch += 1
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK TO page_result")
            self.db.execute("RELEASE page_result")
            raise
        else:
            self.db.execute("RELEASE page_result")
        finally:
            self._batch -= 1

    def setting(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        self.db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        self._commit()

    def bind_mode(self, mode):
        existing = self.setting("mode")
        if existing is None:
            hosts = [r[0] for r in self.db.execute("SELECT url FROM pages")]
            if hosts:
                existing = "demo" if all(x.startswith("https://fixture.local/") for x in hosts) else "scout"
        if existing is not None and existing != mode:
            raise ValueError(f"Database mode {existing!r} cannot be used for {mode!r}; use a separate --db")
        self.set_setting("mode", mode)

    def save_blob(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.blob_dir / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Stored evidence failed integrity check")
        else:
            # Never expose a partially written file at its content-addressed name.
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                        raise ValueError("Stored evidence failed integrity check")
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        self.db.execute("INSERT OR IGNORE INTO blobs VALUES (?,?,?)", (digest, len(data), path.relative_to(self.path.parent).as_posix()))
        self._commit()
        return digest

    def event(self, url, stage, state, status=None, error=None, body=None, snapshot_id=None, details=None, observed_at=None):
        sha = self.save_blob(body) if body is not None else None
        self.db.execute("""INSERT INTO source_events
            (url,stage,state,http_status,error,sha256,snapshot_id,details,checked_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (url, stage, state, status, error, sha, snapshot_id, json.dumps(details or {}, ensure_ascii=False), observed_at or utc_now()))
        self._commit()

    def enqueue(self, url: str, depth: int):
        self.db.execute("""INSERT INTO queue(url,depth,updated_at) VALUES (?,?,?)
            ON CONFLICT(url) DO UPDATE SET depth=MIN(queue.depth,excluded.depth),
            state=CASE WHEN excluded.depth<queue.depth AND queue.state='done' THEN 'queued' ELSE queue.state END,
            attempts=CASE WHEN excluded.depth<queue.depth AND queue.state='done' THEN 0 ELSE queue.attempts END,
            updated_at=excluded.updated_at""", (url, depth, utc_now()))
        self._commit()

    def reset_unfinished(self):
        for url, attempts in self.db.execute("SELECT url,attempts FROM queue WHERE state='in_progress'").fetchall():
            self.event(url, "recovery", "interrupted", details={"attempt": attempts})
        self.db.execute("""UPDATE queue SET state=CASE WHEN attempts>=3 THEN 'exhausted' ELSE 'queued' END,
            updated_at=? WHERE state IN ('in_progress','failed')""", (utc_now(),))
        self._commit()

    def next_url(self, max_depth=4):
        # A single process owns this connection and crawl queue.
        row = self.db.execute("""SELECT url,depth FROM queue WHERE state='queued' AND attempts<3
            AND not_before<=? AND depth<=? ORDER BY depth,rowid LIMIT 1""", (time.time(), max_depth)).fetchone()
        if row:
            self.db.execute("UPDATE queue SET state='in_progress',attempts=attempts+1,updated_at=? WHERE url=?", (utc_now(), row[0]))
            self._commit()
        return row

    def record_page(self, url, title, state, http_status, sha256, error):
        self.db.execute("""INSERT INTO pages VALUES (?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
            title=CASE WHEN excluded.title='' THEN pages.title ELSE excluded.title END,
            state=excluded.state,http_status=excluded.http_status,
            sha256=COALESCE(excluded.sha256,pages.sha256),last_error=excluded.last_error,checked_at=excluded.checked_at""",
            (url, title, state, http_status, sha256, error, utc_now()))
        self._commit()

    def snapshot(self, url, final_url, raw, title, status, content_type, encoding, origin):
        sha = self.save_blob(raw)
        self.record_page(url, title, "fetched", status, sha, None)
        self.db.execute("""INSERT OR IGNORE INTO snapshots
            (source_url,final_url,sha256,title,http_status,content_type,encoding,extractor,origin,observed_at)
            VALUES (?,?,?,?,?,?,?,'stdlib-html/2',?,?)""", (url, final_url, sha, title, status, content_type, encoding, origin, utc_now()))
        row = self.db.execute("SELECT id FROM snapshots WHERE source_url=? AND final_url=? AND sha256=? AND extractor='stdlib-html/2'", (url, final_url, sha)).fetchone()
        self._commit()
        return row[0]

    def record_link(self, source, target, text, context, provider, internal, snapshot_id, ordinal, raw_href, reason):
        self.db.execute("""INSERT OR IGNORE INTO link_occurrences
            (source_url,target_url,anchor_text,context,provider,internal,snapshot_id,ordinal,raw_href,exclusion_reason,observed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (source, target, text, context, provider, int(internal), snapshot_id, ordinal, raw_href, reason, utc_now()))
        self._commit()

    def exclude_link(self, snapshot_id, ordinal, reason):
        self.db.execute("INSERT OR IGNORE INTO excluded_links VALUES (?,?,?)", (snapshot_id, ordinal, reason))
        self._commit()

    def mark_done(self, url):
        self.db.execute("UPDATE queue SET state='done',last_error=NULL,not_before=0,updated_at=? WHERE url=?", (utc_now(), url))
        self._commit()

    def mark_failed(self, url, error, retryable=True, retry_after=0):
        row = self.db.execute("SELECT attempts FROM queue WHERE url=?", (url,)).fetchone()
        attempts = row[0] if row else 1
        state = "failed" if retryable and attempts < 3 else ("exhausted" if retryable else "blocked")
        deadline = time.time() + max(retry_after, 30 * 2 ** (attempts - 1)) if retryable else 0
        self.db.execute("UPDATE queue SET state=?,last_error=?,not_before=?,updated_at=? WHERE url=?", (state, error[:1000], deadline, utc_now(), url))
        self._commit()

    def stats(self):
        def counts(sql):
            return dict(self.db.execute(sql).fetchall())
        def scalar(sql):
            return self.db.execute(sql).fetchone()[0]
        return {
            "mode": self.setting("mode", "legacy_unclassified"),
            "pages_by_state": counts("SELECT state,COUNT(*) FROM pages GROUP BY state"),
            "queue_by_state": counts("SELECT state,COUNT(*) FROM queue GROUP BY state"),
            "link_occurrences": scalar("SELECT COUNT(*) FROM link_occurrences"),
            "distinct_target_urls": scalar("SELECT COUNT(DISTINCT target_url) FROM link_occurrences"),
            "providers": counts("SELECT provider,COUNT(*) FROM link_occurrences GROUP BY provider ORDER BY COUNT(*) DESC"),
            "snapshots": scalar("SELECT COUNT(*) FROM snapshots"),
            "legacy_occurrences_without_snapshot": scalar("SELECT COUNT(*) FROM link_occurrences WHERE snapshot_id IS NULL"),
            "blobs": scalar("SELECT COUNT(*) FROM blobs"),
            "stored_bytes": scalar("SELECT COALESCE(SUM(bytes),0) FROM blobs"),
            "events": scalar("SELECT COUNT(*) FROM source_events"),
            "events_by_stage_state": counts("SELECT stage||':'||state,COUNT(*) FROM source_events GROUP BY stage,state"),
            "excluded_links": counts("SELECT reason,COUNT(*) FROM excluded_links GROUP BY reason"),
            "crawl_exclusions": counts("SELECT exclusion_reason,COUNT(*) FROM link_occurrences WHERE exclusion_reason IS NOT NULL GROUP BY exclusion_reason"),
            "checked_at_range": list(self.db.execute("SELECT MIN(checked_at),MAX(checked_at) FROM source_events").fetchone()),
            "coverage_basis": "Only recorded pages and discovered URLs; no claim about all courses or content analysis",
            "content_analyzed": 0,
            "verified_replacements": 0,
        }

    def export(self, target: str):
        path = Path(target)
        if path.resolve() == self.path.resolve() or self.blob_dir.resolve() in path.resolve().parents:
            raise ValueError("Export cannot overwrite the database or evidence")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as out:
            for table in ("pages", "snapshots", "link_occurrences", "excluded_links", "source_events", "queue", "blobs"):
                cursor = self.db.execute(f"SELECT * FROM {table}")
                names = [d[0] for d in cursor.description]
                for row in cursor:
                    out.write(json.dumps({"table": table, **dict(zip(names, row))}, ensure_ascii=False) + "\n")
