"""Provider classification and public metadata routes; no login or scraping bypass."""
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


def provider(url):
    host = (urlsplit(url).hostname or "").lower()
    if host in {"fruitdet.ru", "wiki.cs.hse.ru", "wikics.site"}:
        return "mediawiki"
    if host in {"disk.yandex.ru", "yadi.sk", "disk.yandex.com"}:
        return "yandex_disk"
    if host in {"youtu.be", "www.youtube.com", "youtube.com"}:
        return "youtube"
    if host in {"drive.google.com", "docs.google.com"}:
        return "google_drive"
    if host in {"github.com", "gitlab.com", "raw.githubusercontent.com"}:
        return "code_host"
    if host in {"www.dropbox.com", "dropbox.com", "dl.dropboxusercontent.com"}:
        return "dropbox"
    return "local" if url.startswith("local:") else "web"


def metadata_route(url):
    kind = provider(url)
    if kind == "yandex_disk":
        return "https://cloud-api.yandex.net/v1/disk/public/resources?" + urlencode({"public_key":url,"limit":100,"offset":0})
    if kind == "youtube":
        return "https://www.youtube.com/oembed?" + urlencode({"url":url,"format":"json"})
    return None


def download_route(url):
    # Official public-link download switch; never changes permissions or access keys.
    # https://help.dropbox.com/share/force-download
    p=urlsplit(url)
    if provider(url)=="dropbox" and p.hostname in {"www.dropbox.com","dropbox.com"} and p.path.startswith(("/s/","/scl/fi/")):
        params=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k not in {"dl","raw"}]
        return urlunsplit((p.scheme,p.netloc,p.path,urlencode(params+[("dl","1")]),""))
    return url


def capability(url):
    return {"provider":provider(url),"metadata_url":metadata_route(url),
            "content_analyzed":False,"video":"Import original video + timed .vtt transcript; speech/board not inferred from metadata",
            "archives":"Inventory only; no extraction or execution","access":"Public URLs only; never bypass login/DRM/captcha"}
