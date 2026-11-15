"""Private append-only source history and resumable SQLite work queue."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from urllib.parse import urlsplit, urlunsplit, quote, unquote


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uid(*parts) -> str:
    return digest(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode())


def canonical(url: str) -> str:
    p = urlsplit(url.strip())
    if p.scheme not in {"https", "http", "local"} or not p.netloc or p.username or p.password:
        raise ValueError("Expected public HTTP(S) URL or local:// identifier")
    host = p.hostname.encode("idna").decode().lower()
    port = p.port
    default_port = 443 if p.scheme == 'https' else 80
    netloc = host + (f":{port}" if port and port != default_port else "")
    # Decode only unreserved characters; encoded path separators retain their meaning.
    path = quote(p.path or "/", safe="/%:@!$&'()*+,;=-._~")
    return urlunsplit((p.scheme.lower(), netloc, path, p.query, ""))


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
INSERT OR IGNORE INTO meta VALUES('schema','1');
CREATE TABLE IF NOT EXISTS resources(
 id TEXT PRIMARY KEY,url TEXT UNIQUE NOT NULL,provider TEXT NOT NULL,role TEXT NOT NULL,
 discovered REAL NOT NULL);
CREATE TABLE IF NOT EXISTS blobs(sha TEXT PRIMARY KEY,size INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(
 id TEXT PRIMARY KEY,resource_id TEXT NOT NULL REFERENCES resources(id),
 sha TEXT NOT NULL REFERENCES blobs(sha),mime TEXT NOT NULL,created REAL NOT NULL,
 final_url TEXT NOT NULL,UNIQUE(resource_id,sha));
CREATE TABLE IF NOT EXISTS attempts(
 id INTEGER PRIMARY KEY,resource_id TEXT NOT NULL REFERENCES resources(id),at REAL NOT NULL,
 status TEXT NOT NULL,http_status INTEGER,error TEXT,snapshot_id TEXT REFERENCES snapshots(id));
CREATE TABLE IF NOT EXISTS nodes(
 id TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL REFERENCES snapshots(id),parent_id TEXT,
 ordinal INTEGER NOT NULL,tag TEXT NOT NULL,path TEXT NOT NULL,text TEXT NOT NULL,
 headings TEXT NOT NULL,UNIQUE(snapshot_id,path));
CREATE TABLE IF NOT EXISTS occurrences(
 id TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL REFERENCES snapshots(id),node_id TEXT NOT NULL REFERENCES nodes(id),
 target_id TEXT NOT NULL REFERENCES resources(id),href TEXT NOT NULL,label TEXT NOT NULL,
 context TEXT NOT NULL,headings TEXT NOT NULL,path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS extractions(
 id TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL REFERENCES snapshots(id),engine TEXT NOT NULL,
 status TEXT NOT NULL,created REAL NOT NULL,UNIQUE(snapshot_id,engine));
CREATE TABLE IF NOT EXISTS chunks(
 id TEXT PRIMARY KEY,extraction_id TEXT NOT NULL REFERENCES extractions(id),
 locator TEXT NOT NULL,text TEXT NOT NULL,quality TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(id UNINDEXED,text,tokenize='unicode61');
CREATE TABLE IF NOT EXISTS versions(
 id TEXT PRIMARY KEY,program TEXT NOT NULL,year TEXT NOT NULL,course TEXT NOT NULL,
 stream TEXT NOT NULL,source_snapshot TEXT NOT NULL REFERENCES snapshots(id),
 evidence_node TEXT NOT NULL REFERENCES nodes(id),verification TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lessons(
 id TEXT PRIMARY KEY,version_id TEXT NOT NULL REFERENCES versions(id),label TEXT NOT NULL,
 position INTEGER NOT NULL,topics TEXT NOT NULL,status TEXT NOT NULL,
 evidence_node TEXT NOT NULL REFERENCES nodes(id),notes TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lesson_materials(
 lesson_id TEXT NOT NULL REFERENCES lessons(id),resource_id TEXT NOT NULL REFERENCES resources(id),
 occurrence_id TEXT REFERENCES occurrences(id),PRIMARY KEY(lesson_id,resource_id,occurrence_id));
CREATE TABLE IF NOT EXISTS reviews(
 id TEXT PRIMARY KEY,lesson_id TEXT NOT NULL REFERENCES lessons(id),version INTEGER NOT NULL,
 verdict TEXT NOT NULL,covered TEXT NOT NULL,uncovered TEXT NOT NULL,reason TEXT NOT NULL,
 reviewer TEXT NOT NULL,created REAL NOT NULL,UNIQUE(lesson_id,version));
CREATE TABLE IF NOT EXISTS evidence(
 review_id TEXT NOT NULL REFERENCES reviews(id),chunk_id TEXT NOT NULL REFERENCES chunks(id),
 quote TEXT NOT NULL,check_method TEXT NOT NULL,PRIMARY KEY(review_id,chunk_id,quote));
CREATE TABLE IF NOT EXISTS review_sources(
 review_id TEXT NOT NULL REFERENCES reviews(id),chunk_id TEXT NOT NULL REFERENCES chunks(id),
 source_version_id TEXT NOT NULL REFERENCES versions(id),transfer_authorization TEXT,
 PRIMARY KEY(review_id,chunk_id,source_version_id));
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY,resource_id TEXT NOT NULL REFERENCES resources(id),state TEXT NOT NULL,
 tries INTEGER NOT NULL DEFAULT 0,due REAL NOT NULL,lease_until REAL,token TEXT,last_error TEXT);
CREATE TABLE IF NOT EXISTS fragments(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,topic TEXT NOT NULL,subtopics TEXT NOT NULL,
 anchors TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS source_edges(
 source_id TEXT NOT NULL REFERENCES resources(id),target_id TEXT NOT NULL REFERENCES resources(id),
 kind TEXT NOT NULL,evidence_snapshot TEXT NOT NULL REFERENCES snapshots(id),note TEXT NOT NULL,
 PRIMARY KEY(source_id,target_id,kind,evidence_snapshot));
CREATE TABLE IF NOT EXISTS assessments(
 id TEXT PRIMARY KEY,fragment_id TEXT NOT NULL REFERENCES fragments(id),version INTEGER NOT NULL,
 analysis TEXT NOT NULL,reviewer TEXT NOT NULL,created REAL NOT NULL,UNIQUE(fragment_id,version));
CREATE TABLE IF NOT EXISTS comparisons(
 id TEXT PRIMARY KEY,left_fragment TEXT NOT NULL REFERENCES fragments(id),
 right_fragment TEXT NOT NULL REFERENCES fragments(id),relation TEXT NOT NULL,
 dimensions TEXT NOT NULL,reason TEXT NOT NULL,limitations TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS trajectories(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,revision INTEGER NOT NULL,plan TEXT NOT NULL,created REAL NOT NULL);
"""


class Store:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("ATLAS_HOME", Path.home()/".fkn-atlas")).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home/"objects").mkdir(exist_ok=True)
        (self.home/"derived").mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.home/"atlas.sqlite", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def resource(self, url, role="linked"):
        from .providers import provider
        url = canonical(url)
        ident = uid("resource", url)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO resources VALUES(?,?,?,?,?)",
                            (ident, url, provider(url), role, time.time()))
        return ident

    def object_path(self, sha):
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("Invalid SHA-256")
        return self.home/"objects"/sha[:2]/sha

    def snapshot(self, resource_id, data, mime, final_url=None):
        sha = digest(data)
        target = self.object_path(sha)
        target.parent.mkdir(exist_ok=True)
        if target.exists():
            if digest(target.read_bytes()) != sha:
                raise ValueError("Existing object failed integrity check")
        else:
            temp = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
            with temp.open("xb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, target)
        sid = uid(resource_id, sha)
        url = final_url or self.rows("SELECT url FROM resources WHERE id=?", (resource_id,))[0]["url"]
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?)", (sha, len(data)))
            self.db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?)",
                            (sid, resource_id, sha, mime, time.time(), url))
        return sid

    def attempt(self, rid, status, http_status=None, error=None, sid=None):
        with self.db:
            self.db.execute("INSERT INTO attempts(resource_id,at,status,http_status,error,snapshot_id) VALUES(?,?,?,?,?,?)",
                            (rid,time.time(),status,http_status,error,sid))

    def enqueue(self, rid, refresh=False):
        ident = uid("fetch",rid)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(id,resource_id,state,due) VALUES(?,?,'pending',?)",(ident,rid,time.time()))
            if refresh:
                self.db.execute("UPDATE jobs SET state='pending',tries=0,due=?,token=NULL,lease_until=NULL WHERE id=? AND state!='running'",(time.time(),ident))
        return ident

    def claim(self, lease_seconds=180, allowed_hosts=None):
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            eligible = self.db.execute("SELECT j.*,r.url FROM jobs j JOIN resources r ON r.id=j.resource_id WHERE (state IN ('pending','retry') AND due<=?) OR (state='running' AND lease_until<?) ORDER BY due",(now,now)).fetchall()
            row = next((r for r in eligible if allowed_hosts is None or urlsplit(r['url']).hostname in allowed_hosts),None)
            if not row:
                self.db.commit()
                return None
            token = uuid.uuid4().hex
            self.db.execute("UPDATE jobs SET state='running',tries=tries+1,lease_until=?,token=? WHERE id=?",(now+lease_seconds,token,row["id"]))
            self.db.commit()
            return dict(self.db.execute("SELECT * FROM jobs WHERE id=?",(row["id"],)).fetchone())
        except BaseException:
            self.db.rollback()
            raise

    def finish(self, job, state, error=None, delay=0):
        with self.db:
            changed = self.db.execute("UPDATE jobs SET state=?,last_error=?,due=?,lease_until=NULL,token=NULL WHERE id=? AND token=?",
                                     (state,error,time.time()+delay,job["id"],job["token"])).rowcount
        if not changed:
            raise RuntimeError("Stale worker lease")

    def verify(self):
        bad = [r["sha"] for r in self.rows("SELECT sha FROM blobs")
               if not self.object_path(r["sha"]).exists() or digest(self.object_path(r["sha"]).read_bytes()) != r["sha"]]
        return {"sqlite":self.db.execute("PRAGMA integrity_check").fetchone()[0],
                "foreign_keys":len(self.rows("PRAGMA foreign_key_check")),"bad_objects":bad,
                "objects":self.db.execute("SELECT count(*) FROM blobs").fetchone()[0]}
