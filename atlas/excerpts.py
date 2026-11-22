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


def render_excerpts(markup, store, destination):
    if "<!-- excerpt " not in markup:
        return markup
    try:
        import pymupdf
    except ImportError:
        pymupdf = None

    def render(match):
        kind, sha, page_number, left, top, right, bottom, description = match.groups()
        source = (store.home / "derived" / "video-frames" / f"{sha}.png"
                  if kind == "frame" else store.object_path(sha))
        if not source.is_file():
            return ""
        data = source.read_bytes()
        if digest(data) != sha:
            warnings.warn(f"Excerpt skipped: SHA-256 mismatch for {source.name}", stacklevel=2)
            return ""
        originals = Path(destination) / "originals"
        originals.mkdir(exist_ok=True)
        if kind == "frame":
            original = originals / f"{sha}.png"
            if not original.is_file() or digest(original.read_bytes()) != sha:
                original.write_bytes(data)
            page_link = f'<a class="source-page" href="originals/{original.name}">Кадр целиком</a>'
            if pymupdf is None:
                return page_link
        elif kind == "board":
            if pymupdf is None:
                return ""
            # Serve a rasterized board. Original SVG markup stays in the private store.
            original = originals / f"{sha}.png"
            page_link = f'<a class="source-page" href="originals/{original.name}">Доска целиком</a>'
        else:
            original = originals / f"{sha}.pdf"
            if not original.is_file() or digest(original.read_bytes()) != sha:
                original.write_bytes(data)
            page_link = (f'<a class="source-page" href="originals/{original.name}#page={page_number}">'
                         f'Страница {page_number} целиком</a>')
            if pymupdf is None:
                return page_link
        rect = pymupdf.Rect(*map(int, (left, top, right, bottom)))
        filetype = {"board": "svg", "frame": "png", None: "pdf"}[kind]
        with pymupdf.open(stream=data, filetype=filetype) as document:
            page = document[int(page_number) - 1]
            if int(page_number) < 1 or rect.is_empty or not page.rect.contains(rect):
                raise ValueError(f"Invalid excerpt on page {page_number} of {sha}")
            if kind == "board" and not original.is_file():
                page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).save(original)
            pixels = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=rect, alpha=False)
        name = f"{sha}-p{page_number}-{left}-{top}-{right}-{bottom}.png"
        folder = Path(destination) / "excerpts"
        folder.mkdir(exist_ok=True)
        (folder / name).write_bytes(pixels.tobytes("png"))
        url = f"excerpts/{name}"
        return (page_link + '<figure class="source-image">'
                f'<a href="{url}" title="Открыть изображение в полном размере">'
                f'<img src="{url}" width="{pixels.width}" height="{pixels.height}" '
                f'loading="lazy" alt="{html.escape(description, quote=True)}"></a></figure>')

    return EXCERPT.sub(render, markup)
