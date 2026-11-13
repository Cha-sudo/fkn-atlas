"""Conservative URL identity and a read-only Wiki crawl boundary."""
from __future__ import annotations
import re
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit, urlunsplit, quote, unquote

TRACKING = {"si", "fbclid", "gclid", "yclid", "mc_cid", "mc_eid"}
MEDIA_EXT = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".zip", ".7z", ".mp4", ".mkv", ".txt", ".ipynb", ".csv"}
SENSITIVE = {"token", "access_token", "auth", "authorization", "signature", "sig", "key", "api_key", "password"}


def normalize_url(raw: str, base: str | None = None) -> str | None:
    if not raw or not raw.strip() or any(ord(c) < 32 for c in raw):
        return None
    try:
        p = urlsplit(urljoin(base, raw.strip()) if base else raw.strip())
        if p.scheme.lower() not in ("http", "https") or not p.hostname or p.username is not None or p.password is not None:
            return None
        scheme, host, port = p.scheme.lower(), p.hostname.lower().rstrip("."), p.port
        host = host.encode("idna").decode("ascii") if ":" not in host else f"[{host}]"
        if port is not None and (scheme, port) not in (("http", 80), ("https", 443)):
            host += f":{port}"
        # Decode unreserved ASCII only: %2F identifies a different URL from /.
        def escape(m):
            ch = chr(int(m[1], 16))
            return ch if ch.isascii() and (ch.isalnum() or ch in "-._~") else "%" + m[1].upper()
        path = re.sub(r"%([0-9a-fA-F]{2})", escape, p.path or "/")
        path = re.sub(r"%(?![0-9A-Fa-f]{2})", "%25", path)
        path = quote(path, safe="/%~:@!$&'()*+,;=-._")
        # Keep query order and encoding: some external services depend on them.
        filtered = []
        for part in p.query.split("&") if p.query else []:
            key = unquote(part.split("=", 1)[0]).lower()
            if key in SENSITIVE or key.startswith(("x-amz-", "x-goog-")):
                return None
            if key not in TRACKING and not key.startswith("utm_"):
                filtered.append(quote(part, safe="%=+/:?@!$'()*;,~-._"))
        return urlunsplit((scheme, host, path, "&".join(filtered), ""))
    except (ValueError, UnicodeError):
        return None


def scope_reason(url: str, host: str = "fruitdet.ru") -> str | None:
    try:
        p = urlsplit(url)
        if p.scheme not in ("https", "http") or p.hostname != host or p.username is not None:
            return "outside_wiki_origin"
        if p.port is not None and (p.scheme, p.port) not in (("https", 443), ("http", 80)):
            return "nonstandard_port"
        path = unquote(p.path)
        if "\\" in path or any(part in (".", "..") for part in path.split("/")):
            return "unsafe_path"
        if not (path == "/wiki" or path.startswith("/wiki/")):
            return "outside_wiki_path"
        if p.query:
            return "wiki_query_not_crawled"
        name = path.removeprefix("/wiki/").split("/", 1)[0].casefold()
        if any(name.startswith(prefix + ":") for prefix in (
            "special", "служебная", "file", "файл", "media", "медиа", "user", "участник", "talk", "обсуждение"
        )):
            return "service_namespace"
        if PurePosixPath(path.lower()).suffix in MEDIA_EXT:
            return "non_html_resource"
        return None
    except ValueError:
        return "invalid_url"


def in_wiki_scope(url: str, host: str = "fruitdet.ru") -> bool:
    return scope_reason(url, host) is None


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
    if PurePosixPath(unquote(p.path).lower()).suffix in MEDIA_EXT:
        return "direct_file"
    if host in {"fruitdet.ru", "wiki.cs.hse.ru"}:
        return "wiki"
    return "web"
