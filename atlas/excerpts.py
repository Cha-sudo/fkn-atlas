"""Render verified PDF, board and lecture-frame excerpts for the private course reader."""
import html
import re
import warnings
from pathlib import Path

from .store import digest


# Coordinates are PDF points from the upper left. The adjacent source link also
# works when the original or the optional PDF renderer is unavailable locally.
EXCERPT = re.compile(
    r"<!-- excerpt (?:(board|frame) )?(?:(?:open|fold) )?([0-9a-f]{64}) (\d+) "
    r"(\d+) (\d+) (\d+) (\d+) \| ([^<>\n]+?) -->"
)


DOCUMENT = re.compile(r"<!-- document (?:(board) )?([0-9a-f]{64}) (\d+) \| ([^<>\n]+?) -->")


def render_excerpts(markup, store, destination, page_images=None):
    if "<!-- excerpt " not in markup and "<!-- document " not in markup:
        return markup
    try:
        import pymupdf
    except ImportError:
        pymupdf = None

    destination = Path(destination)
    if page_images is None:
        page_images = {}

    def read_source(kind, sha):
        source = (store.home / "derived" / "video-frames" / f"{sha}.png"
                  if kind == "frame" else store.object_path(sha))
        if not source.is_file():
            return None
        data = source.read_bytes()
        if digest(data) != sha:
            warnings.warn(f"Source skipped: SHA-256 mismatch for {source.name}", stacklevel=2)
            return None
        return data

    def copy_original(sha, data, extension):
        path = destination / "originals" / f"{sha}.{extension}"
        path.parent.mkdir(exist_ok=True)
        if not path.is_file() or digest(path.read_bytes()) != sha:
            path.write_bytes(data)
        return f"originals/{path.name}"

    def page_image(page, sha, number):
        key = (sha, number)
        if key not in page_images:
            scale = 1800 / max(page.rect.width, page.rect.height)
            pixels = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            folder = destination / "pages"
            folder.mkdir(exist_ok=True)
            name = f"{sha}-{number}.png"
            pixels.save(folder / name)
            page_images[key] = (f"pages/{name}", pixels.width, pixels.height)
        return page_images[key]

    def render_document(match):
        kind, sha, start, title = match.groups()
        data = read_source(kind, sha)
        if data is None:
            return ""
        title = html.escape(title, quote=True)
        original = None if kind else copy_original(sha, data, "pdf")
        if pymupdf is None:
            if kind:
                return ""
            return (f'<figure class="material-reader"><figcaption>{title}</figcaption>'
                    f'<object type="application/pdf" data="{original}#page={start}" aria-label="{title}">'
                    f'<a href="{original}">Открыть PDF</a></object></figure>')
        images = []
        with pymupdf.open(stream=data, filetype="svg" if kind else "pdf") as doc:
            if not 1 <= int(start) <= len(doc):
                raise ValueError(f"Invalid initial page {start} of {sha}")
            for number, page in enumerate(doc, 1):
                if kind:
                    url, width, height = board_image(page, sha)
                    original = url
                    alt = title
                else:
                    url, width, height = page_image(page, sha, number)
                    alt = f"{title}, страница {number} из {len(doc)}"
                images.append(f'<img src="{url}" width="{width}" height="{height}" loading="lazy" alt="{alt}">')
        label = "Доска в полном размере" if kind else "Открыть PDF"
        return (f'<figure class="material-reader" data-start="{start}"><figcaption>{title}</figcaption>'
                f'<div class="material-pages" tabindex="0" aria-label="{title}">{"".join(images)}</div>'
                f'<a class="material-file" href="{original}">{label}</a></figure>')

    def board_image(page, sha):
        key = (sha, "board")
        if key not in page_images:
            path = destination / "originals" / f"{sha}.png"
            path.parent.mkdir(exist_ok=True)
            pixels = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
            pixels.save(path)
            page_images[key] = (f"originals/{path.name}", pixels.width, pixels.height)
        return page_images[key]

    def render(match):
        kind, sha, page_number, left, top, right, bottom, description = match.groups()
        data = read_source(kind, sha)
        if data is None:
            return ""
        if kind == "board":
            if pymupdf is None:
                return ""
            page_url = f"originals/{sha}.png"
            label = "Доска целиком"
        elif kind == "frame":
            page_url = copy_original(sha, data, "png")
            label = "Кадр целиком"
        else:
            page_url = copy_original(sha, data, "pdf") + f"#page={page_number}"
            label = f"Страница {page_number} целиком"
        if pymupdf is None:
            return f'<a class="source-page" href="{page_url}">{label}</a>'
        rect = pymupdf.Rect(*map(int, (left, top, right, bottom)))
        filetype = {"board": "svg", "frame": "png", None: "pdf"}[kind]
        with pymupdf.open(stream=data, filetype=filetype) as document:
            page = document[int(page_number) - 1]
            if int(page_number) < 1 or rect.is_empty or not page.rect.contains(rect):
                raise ValueError(f"Invalid excerpt on page {page_number} of {sha}")
            if kind == "board":
                preview, _, _ = board_image(page, sha)
            elif kind == "frame":
                preview = page_url
            else:
                preview, _, _ = page_image(page, sha, int(page_number))
            pixels = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=rect, alpha=False)
        name = f"{sha}-p{page_number}-{left}-{top}-{right}-{bottom}.png"
        folder = Path(destination) / "excerpts"
        folder.mkdir(exist_ok=True)
        (folder / name).write_bytes(pixels.tobytes("png"))
        url = f"excerpts/{name}"
        page_link = f'<a class="source-page" href="{page_url}" data-preview="{preview}">{label}</a>'
        return (page_link + '<figure class="source-image">'
                f'<a href="{url}" title="Открыть изображение в полном размере">'
                f'<img src="{url}" width="{pixels.width}" height="{pixels.height}" '
                f'loading="lazy" alt="{html.escape(description, quote=True)}"></a></figure>')

    return EXCERPT.sub(render, DOCUMENT.sub(render_document, markup))
