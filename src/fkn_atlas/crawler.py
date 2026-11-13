"""Pilot crawler. Non-exhaustive. No external-provider crawling or media downloads."""
from __future__ import annotations
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
import hashlib
import time

from .extract import extract_links
from .storage import Store
from .urls import normalize_url, provider_for, in_wiki_scope

USER_AGENT = "FKNAtlasResearch/0.1 (personal-educational; slow-crawl)"
MAX_BYTES = 4 * 1024 * 1024

class RestrictRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not in_wiki_scope(newurl):
            raise ValueError(f"Redirect outside allowed Wiki scope: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

class Fetcher:
    def __init__(self):
        self.opener = build_opener(RestrictRedirect)
        self.robot = RobotFileParser()
        self.robot_status = "unchecked"

    def load_robots(self) -> None:
        url = "https://fruitdet.ru/robots.txt"
        try:
            with self.opener.open(Request(url, headers={"User-Agent": USER_AGENT}), timeout=12) as response:
                data = response.read(512 * 1024).decode("utf-8", errors="replace")
            self.robot.parse(data.splitlines())
            self.robot_status = "parsed"
        except HTTPError as exc:
            if exc.code == 404:
                self.robot_status = "absent_404"
            else:
                self.robot_status = f"unavailable_http_{exc.code}"
                raise RuntimeError(f"Unable to check robots.txt: HTTP {exc.code}") from exc
        except Exception as exc:
            self.robot_status = "unavailable"
            raise RuntimeError(f"Unable to check robots.txt: {exc}") from exc

    def get_page(self, url: str) -> tuple[str, int]:
        if self.robot_status == "unchecked":
            raise RuntimeError("robots.txt must be checked first")
        if self.robot_status == "parsed" and not self.robot.can_fetch(USER_AGENT, url):
            raise PermissionError(f"Disallowed by robots.txt: {url}")
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        with self.opener.open(req, timeout=20) as resp:
            ctype = resp.headers.get_content_type()
            if ctype not in {"text/html", "application/xhtml+xml"}:
                raise ValueError(f"Not HTML: {ctype}")
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("HTML exceeds 4 MiB safety limit")
            return body.decode(charset, errors="replace"), resp.status


def process_html(store: Store, source_url: str, html: str, depth: int,
                 scope_host: str, max_depth: int) -> int:
    doc = extract_links(html)
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    store.record_page(source_url, doc.title, "fetched", 200, digest, None)
    processed = 0
    for link in doc.links:
        absolute = normalize_url(link.href, source_url)
        if not absolute:
            continue
        internal = in_wiki_scope(absolute, host=scope_host)
        store.record_link(source_url, absolute, link.text, link.context,
                          provider_for(absolute), internal)
        processed += 1
        if internal and depth < max_depth:
            store.enqueue(absolute, depth + 1)
    return processed


def run_demo(store: Store, fixture_dir: Path) -> dict:
    base = "https://fixture.local/wiki/"
    mapping = {
        base + "Start": "start.html",
        base + "Archive": "archive.html",
        base + "Linear_Algebra_2023": "linear_algebra.html",
    }
    for url in mapping:
        store.enqueue(url, 0 if url.endswith("Start") else 1)
    store.reset_unfinished()
    for url, fname in mapping.items():
        html = (fixture_dir / fname).read_text(encoding="utf-8")
        process_html(store, url, html, 0, "fixture.local", 2)
        store.mark_done(url)
    return store.stats()


def run_scout(store: Store, max_pages: int = 8, max_depth: int = 2,
              delay: float = 1.5) -> dict:
    if not (1 <= max_pages <= 30):
        raise ValueError("Pilot max_pages must be 1..30; implement reviewed scaling separately")
    if not (0 <= max_depth <= 4 and delay >= 1.0):
        raise ValueError("Pilot requires max_depth 0..4 and delay >= 1s")
    f = Fetcher()
    f.load_robots()
    for seed in (
        "https://fruitdet.ru/wiki/Wiki_%D0%A4%D0%9A%D0%9D",
        "https://fruitdet.ru/wiki/Wiki_%D0%A4%D0%9A%D0%9D/%D0%90%D1%80%D1%85%D0%B8%D0%B2",
    ):
        store.enqueue(seed, 0)
    store.reset_unfinished()
    visited = 0
    while visited < max_pages:
        job = store.next_url()
        if job is None:
            break
        url, depth = job
        if not in_wiki_scope(url):
            store.mark_failed(url, "Out of scope")
            continue
        visited += 1
        try:
            html, status = f.get_page(url)
            process_html(store, url, html, depth, "fruitdet.ru", max_depth)
            store.mark_done(url)
            print(f"[OK] {url} depth={depth}")
        except Exception as exc:
            status = exc.code if isinstance(exc, HTTPError) else None
            msg = f"{type(exc).__name__}: {exc}"
            store.record_page(url, "", "failed", status, None, msg)
            store.mark_failed(url, msg)
            print(f"[ERROR] {url}: {msg}")
        time.sleep(delay)
    return store.stats()
