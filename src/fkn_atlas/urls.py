"""URL normalization, provider identification and crawl scope."""
from __future__ import annotations
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode, quote, unquote
from pathlib import PurePosixPath

TRACKING = {"si", "fbclid", "gclid", "yclid", "mc_cid", "mc_eid"}
MEDIA_EXT = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".zip", ".7z", ".mp4", ".mkv", ".txt", ".ipynb", ".csv"}

def normalize_url(raw: str, base: str | None = None) -> str | None:
    if not raw or not raw.strip():
        return None
    candidate = urljoin(base, raw.strip()) if base else raw.strip()
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return None
        # Do not preserve embedded credentials in saved URLs.
        if parsed.username is not None or parsed.password is not None:
            return None
        scheme = parsed.scheme.lower()
        host = parsed.hostname.lower().rstrip(".")
        port = parsed.port  # raises ValueError on malformed ports
        host = host if ":" not in host else f"[{host}]"
        if port and (scheme, port) not in (("http", 80), ("https", 443)):
            host += f":{port}"
        path = quote(unquote(parsed.path or "/"), safe="/~:@!$&'()*+,;=-._")
        filtered = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                    if k.lower() not in TRACKING and not k.lower().startswith("utm_")]
        query = urlencode(sorted(filtered), doseq=True)
        return urlunsplit((scheme, host, path, query, ""))
    except (ValueError, UnicodeError):
        return None

def in_wiki_scope(url: str, host: str = "fruitdet.ru") -> bool:
    p = urlsplit(url)
    return (p.scheme in ("https", "http") and p.hostname == host and
            (p.path == "/wiki" or p.path.startswith("/wiki/")) and
            not p.path.startswith("/wiki/Special:"))

def provider_for(url: str) -> str:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host in {"youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com", "youtube-nocookie.com"}:
        return "youtube"
    if host in {"disk.yandex.ru", "disk.yandex.com", "yadi.sk"}:
        return "yandex_disk"
    if host in {"drive.google.com", "docs.google.com"}:
        return "google_drive"
    if host in {"github.com", "raw.githubusercontent.com", "gitlab.com"}:
        return "git_repository"
    if host in {"fruitdet.ru", "wiki.cs.hse.ru"}:
        return "wiki"
    if PurePosixPath(p.path.lower()).suffix in MEDIA_EXT:
        return "direct_file"
    return "web"
