"""Bounded, single-worker Wiki reconnaissance. No external media downloads."""
from __future__ import annotations
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser
import codecs
import json
import math
import time

from .extract import extract_links
from .storage import Store
from .urls import normalize_url, provider_for, in_wiki_scope, scope_reason

USER_AGENT = "FKNAtlasResearch/0.2 (personal-educational; slow-crawl)"
MAX_BYTES = 4 * 1024 * 1024
SEEDS = (
    "https://fruitdet.ru/wiki/Wiki_%D0%A4%D0%9A%D0%9D",
    "https://fruitdet.ru/wiki/Wiki_%D0%A4%D0%9A%D0%9D/%D0%90%D1%80%D1%85%D0%B8%D0%B2",
)
ROBOTS_URL = "https://fruitdet.ru/robots.txt"


@dataclass(frozen=True)
class FetchResult:
    body: bytes
    final_url: str
    status: int = 200
    content_type: str = "text/html"
    encoding: str = "utf-8"

    @property
    def text(self):
        return self.body.decode(self.encoding, errors="replace")


class FetchFailure(Exception):
    def __init__(self, message, *, status=None, retryable=True, retry_after=0, body=None, state="fetch_error"):
        super().__init__(message)
        self.status, self.retryable, self.retry_after = status, retryable, retry_after
        self.body, self.state = body, state


def retry_after_seconds(value, now=None):
    if not value:
        return 0
    now = time.time() if now is None else now
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 0


class RestrictRedirect(HTTPRedirectHandler):
    def __init__(self, fetcher):
        self.fetcher = fetcher

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = normalize_url(urljoin(req.full_url, newurl))
        if self.fetcher.loading_robots:
            p = urlsplit(target or "")
            allowed = p.hostname == "fruitdet.ru" and p.scheme in ("https", "http") and (p.port is None or (p.scheme, p.port) in (("http", 80), ("https", 443))) and p.path.lower() == "/robots.txt" and not p.query
        else:
            allowed = target is not None and in_wiki_scope(target)
        if not allowed:
            raise FetchFailure("Redirect outside allowed scope", status=code, retryable=False, state="redirect_blocked")
        if not self.fetcher.loading_robots:
            self.fetcher.check_robot(target)
        self.fetcher.redirects.append({"from": req.full_url, "to": target, "status": code})
        self.fetcher.wait()
        return super().redirect_request(req, fp, code, msg, headers, target)


class Fetcher:
    def __init__(self, delay=1.5):
        self.delay = delay
        self.last_request = None
        self.robot = RobotFileParser()
        self.robot_status = "unchecked"
        self.loading_robots = False
        self.redirects = []
        self.opener = build_opener(RestrictRedirect(self))

    def wait(self):
        if self.last_request is not None:
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()

    def request(self, url, limit):
        self.wait()
        self.redirects = []
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain", "Accept-Encoding": "identity"})
        try:
            with self.opener.open(req, timeout=20) as resp:
                body = resp.read(limit + 1)
                if len(body) > limit:
                    raise FetchFailure("Response exceeds byte limit", status=resp.status, retryable=False, state="too_large")
                if resp.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                    raise FetchFailure("Unexpected compressed response", status=resp.status, retryable=False, body=body, state="unsupported_encoding")
                encoding = resp.headers.get_content_charset() or "utf-8"
                try:
                    codecs.lookup(encoding)
                except LookupError:
                    raise FetchFailure("Unknown response charset", status=resp.status, retryable=False, body=body, state="unsupported_encoding")
                return FetchResult(body, resp.geturl(), resp.status, resp.headers.get_content_type(), encoding)
        except HTTPError as exc:
            with exc:
                body = exc.read(limit + 1)
                # Partial oversized bodies are not presented as complete snapshots.
                body = body if len(body) <= limit else None
                raise FetchFailure(f"HTTP {exc.code}", status=exc.code,
                    retryable=exc.code in (408, 425, 429) or exc.code >= 500,
                    retry_after=retry_after_seconds(exc.headers.get("Retry-After")), body=body,
                    state="http_error") from exc
        except (URLError, TimeoutError, OSError) as exc:
            # Error class is useful; arbitrary exception text may contain signed URLs.
            raise FetchFailure(f"Network failure ({type(exc).__name__})", state="network_error") from exc

    def load_robots(self):
        self.loading_robots = True
        try:
            result = self.request(ROBOTS_URL, 512 * 1024)
            if result.status != 200:
                raise FetchFailure("Unexpected robots HTTP status", status=result.status, body=result.body)
            if result.content_type not in ("text/plain", "text/html") or result.text.lstrip().lower().startswith(("<!doctype html", "<html")):
                raise FetchFailure("robots response is not a usable rules file", status=result.status, body=result.body)
            self.robot.parse(result.text.splitlines())
            self.robot_status = "parsed"
            crawl_delay = self.robot.crawl_delay(USER_AGENT) or self.robot.crawl_delay("*") or 0
            rate = self.robot.request_rate(USER_AGENT) or self.robot.request_rate("*")
            self.delay = max(self.delay, crawl_delay, rate.seconds / rate.requests if rate and rate.requests else 0)
            return result
        except FetchFailure as exc:
            if exc.status in (404, 410):
                self.robot_status = f"absent_{exc.status}"
                return FetchResult(exc.body or b"", ROBOTS_URL, exc.status, "text/plain")
            self.robot_status = "unavailable"
            raise
        finally:
            self.loading_robots = False

    def check_robot(self, url):
        if self.robot_status not in ("parsed", "absent_404", "absent_410"):
            raise FetchFailure("robots must be successfully checked first", retryable=False, state="robots_unchecked")
        if self.robot_status == "parsed" and not self.robot.can_fetch(USER_AGENT, url):
            raise FetchFailure("Disallowed by robots.txt", retryable=False, state="robots_denied")

    def get_page(self, url):
        self.redirects = []
        if not in_wiki_scope(url):
            raise FetchFailure("URL outside allowed scope", retryable=False, state="out_of_scope")
        self.check_robot(url)
        result = self.request(url, MAX_BYTES)
        if result.status != 200:
            raise FetchFailure("Incomplete or unexpected HTML response", status=result.status, retryable=False, body=result.body, state="incomplete_response")
        if result.content_type not in {"text/html", "application/xhtml+xml"}:
            raise FetchFailure("Response is not HTML", status=result.status, retryable=False, body=result.body, state="not_html")
        return result


def process_html(store: Store, source_url: str, html: str, depth: int,
                 scope_host: str, max_depth: int, *, raw=None, final_url=None,
                 status=200, content_type="text/html", encoding="utf-8", origin="demo", stage="page", redirects=None,
                 observed_at=None, evidence_details=None):
    doc = extract_links(html)
    raw = html.encode("utf-8") if raw is None else raw
    final_url = final_url or source_url
    base = normalize_url(doc.base_href, final_url) if doc.base_href else final_url
    base = base or final_url
    with store.transaction():
        snapshot_id = store.snapshot(source_url, final_url, raw, doc.title, status, content_type, encoding, origin, observed_at)
        processed = 0
        for link in doc.links:
            absolute = normalize_url(link.href, base)
            if not absolute:
                store.exclude_link(snapshot_id, link.ordinal, "invalid_unsupported_or_sensitive_url")
                continue
            reason = scope_reason(absolute, host=scope_host)
            internal = reason is None
            store.record_link(source_url, absolute, link.text, link.context, provider_for(absolute), internal,
                snapshot_id, link.ordinal, link.href, reason)
            processed += 1
            if internal and depth < max_depth:
                store.enqueue(absolute, depth + 1)
        store.event(source_url, stage, "fetched", status, snapshot_id=snapshot_id,
            details={"final_url": final_url, "origin": origin, "links": processed, "redirects": redirects or [], **(evidence_details or {})}, observed_at=observed_at)
        store.mark_done(source_url)
    return processed


def run_demo(store: Store, fixture_dir: Path):
    store.bind_mode("demo")
    base = "https://fixture.local/wiki/"
    mapping = {base + "Start": "start.html", base + "Archive": "archive.html", base + "Linear_Algebra_2023": "linear_algebra.html"}
    store.enqueue(base + "Start", 0)
    store.reset_unfinished()
    while job := store.next_url():
        url, depth = job
        if url not in mapping:
            store.event(url, "fixture", "not_supplied")
            store.mark_failed(url, "Fixture not supplied; no network request made", retryable=False)
            continue
        raw = (fixture_dir / mapping[url]).read_bytes()
        process_html(store, url, raw.decode("utf-8"), depth, "fixture.local", 2, raw=raw, origin="demo", stage="fixture")
    return store.stats()


def run_scout(store: Store, max_pages=8, max_depth=2, delay=1.5, *, fetcher=None):
    if not (1 <= max_pages <= 12):
        raise ValueError("Pilot max_pages must be 1..12")
    if not (0 <= max_depth <= 4 and math.isfinite(delay) and delay >= 1.0):
        raise ValueError("Pilot requires max_depth 0..4 and finite delay >= 1s")
    store.bind_mode("scout")
    for seed in SEEDS:
        store.enqueue(seed, 0)
    store.reset_unfinished()
    if float(store.setting("host_not_before", "0")) > time.time():
        return {**store.stats(), "run_status": "host_backoff", "attempted_this_run": 0}
    due = store.db.execute("SELECT 1 FROM queue WHERE state='queued' AND attempts<3 AND depth<=? AND not_before<=? LIMIT 1", (max_depth, time.time())).fetchone()
    if not due:
        return {**store.stats(), "run_status": "no_due_jobs", "attempted_this_run": 0}
    f = fetcher or Fetcher(delay)
    try:
        robots = f.load_robots()
        store.event(ROBOTS_URL, "robots", f.robot_status, robots.status, body=robots.body, details={"final_url": robots.final_url})
    except FetchFailure as exc:
        store.event(ROBOTS_URL, "robots", "unavailable", exc.status, str(exc), exc.body)
        store.set_setting("host_not_before", time.time() + max(60, exc.retry_after))
        return {**store.stats(), "run_status": "robots_unavailable", "attempted_this_run": 0}
    visited = 0
    while visited < max_pages and (job := store.next_url(max_depth)):
        url, depth = job
        visited += 1
        try:
            if not in_wiki_scope(url):
                raise FetchFailure("Out of scope", retryable=False, state="out_of_scope")
            result = f.get_page(url)
            process_html(store, url, result.text, depth, "fruitdet.ru", max_depth, raw=result.body,
                final_url=result.final_url, status=result.status, content_type=result.content_type,
                encoding=result.encoding, origin="scout", redirects=getattr(f, "redirects", []))
            print(f"[OK] {url} depth={depth}")
        except FetchFailure as exc:
            with store.transaction():
                store.record_page(url, "", exc.state, exc.status, None, str(exc))
                store.event(url, "page", exc.state, exc.status, str(exc), exc.body,
                    details={"retryable": exc.retryable, "retry_after_seconds": exc.retry_after, "redirects": getattr(f, "redirects", [])})
                store.mark_failed(url, str(exc), exc.retryable, exc.retry_after)
            print(f"[ERROR] {url}: {exc}")
            # Stop all requests to the host when it explicitly signals throttling/unavailability.
            if exc.status in (429, 503) or exc.retry_after:
                store.set_setting("host_not_before", time.time() + max(60, exc.retry_after))
                break
        except Exception as exc:
            # Preserve checkpoint and diagnostic type, then fail visibly on programming/storage errors.
            store.event(url, "processing", "error", error=type(exc).__name__)
            store.mark_failed(url, type(exc).__name__)
            raise
    return {**store.stats(), "run_status": "finished", "attempted_this_run": visited}
