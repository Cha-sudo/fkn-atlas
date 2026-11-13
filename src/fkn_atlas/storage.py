"""SQLite records: pages, link occurrences and a resumable internal-page queue."""
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import sqlite3

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS pages (
  url TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'unknown',
  http_status INTEGER,
  sha256 TEXT,
  last_error TEXT,
  checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS link_occurrences (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_url TEXT NOT NULL REFERENCES pages(url),
  target_url TEXT NOT NULL,
  anchor_text TEXT NOT NULL,
  context TEXT NOT NULL,
  provider TEXT NOT NULL,
  internal INTEGER NOT NULL CHECK(internal IN (0,1)),
  observed_at TEXT NOT NULL,
  UNIQUE (source_url, target_url, anchor_text, context)
);
CREATE INDEX IF NOT EXISTS ix_links_target ON link_occurrences(target_url);
CREATE INDEX IF NOT EXISTS ix_links_provider ON link_occurrences(provider);
CREATE TABLE IF NOT EXISTS queue (
  url TEXT PRIMARY KEY,
  depth INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL
);
"""

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

class Store:
    def __init__(self, filename: str):
        p = Path(filename)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(p)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def enqueue(self, url: str, depth: int) -> None:
        self.db.execute("""INSERT OR IGNORE INTO queue(url,depth,state,attempts,updated_at)
                          VALUES (?,?,'queued',0,?)""", (url, depth, utc_now()))
        self.db.commit()

    def reset_unfinished(self) -> None:
        # Crash recovery. Retry failed attempts in a NEW run up to three times.
        self.db.execute("""UPDATE queue SET state='queued',updated_at=?
                          WHERE state IN ('in_progress','failed') AND attempts<3""", (utc_now(),))
        self.db.commit()

    def next_url(self) -> tuple[str, int] | None:
        row = self.db.execute("""SELECT url,depth FROM queue WHERE state='queued'
                                AND attempts<3 ORDER BY depth,url LIMIT 1""").fetchone()
        if not row:
            return None
        self.db.execute("UPDATE queue SET state='in_progress',attempts=attempts+1,updated_at=? WHERE url=?",
                        (utc_now(), row[0]))
        self.db.commit()
        return (row[0], row[1])

    def record_page(self, url: str, title: str, state: str, http_status: int | None,
                    sha256: str | None, error: str | None) -> None:
        self.db.execute("""INSERT INTO pages(url,title,state,http_status,sha256,last_error,checked_at)
                           VALUES (?,?,?,?,?,?,?)
                           ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                           state=excluded.state,http_status=excluded.http_status,
                           sha256=excluded.sha256,last_error=excluded.last_error,
                           checked_at=excluded.checked_at""",
                        (url, title, state, http_status, sha256, error, utc_now()))
        self.db.commit()

    def record_link(self, source: str, target: str, text: str, context: str,
                    provider: str, internal: bool) -> None:
        self.db.execute("""INSERT INTO link_occurrences
                           (source_url,target_url,anchor_text,context,provider,internal,observed_at)
                           VALUES (?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET observed_at=excluded.observed_at""",
                        (source, target, text, context, provider, int(internal), utc_now()))
        self.db.commit()

    def mark_done(self, url: str) -> None:
        self.db.execute("UPDATE queue SET state='done',last_error=NULL,updated_at=? WHERE url=?",
                        (utc_now(), url))
        self.db.commit()

    def mark_failed(self, url: str, error: str) -> None:
        self.db.execute("UPDATE queue SET state='failed',last_error=?,updated_at=? WHERE url=?",
                        (error[:1000], utc_now(), url))
        self.db.commit()

    def stats(self) -> dict:
        rows = self.db.execute("SELECT state,COUNT(*) FROM pages GROUP BY state").fetchall()
        queued = self.db.execute("SELECT state,COUNT(*) FROM queue GROUP BY state").fetchall()
        providers = self.db.execute("SELECT provider,COUNT(*) FROM link_occurrences GROUP BY provider ORDER BY COUNT(*) DESC").fetchall()
        return {
            "pages_by_state": dict(rows),
            "queue_by_state": dict(queued),
            "link_occurrences": self.db.execute("SELECT COUNT(*) FROM link_occurrences").fetchone()[0],
            "distinct_target_urls": self.db.execute("SELECT COUNT(DISTINCT target_url) FROM link_occurrences").fetchone()[0],
            "providers": dict(providers),
        }
