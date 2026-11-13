"""Conservative stdlib HTML link extraction. Does not infer course identity."""
from __future__ import annotations
from dataclasses import dataclass
from html.parser import HTMLParser
import re

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

@dataclass(frozen=True)
class ExtractedLink:
    href: str
    text: str
    context: str

@dataclass(frozen=True)
class HtmlPage:
    title: str
    links: list[ExtractedLink]

class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = 0
        self.heading_level: str | None = None
        self.heading_parts: list[str] = []
        self.current_heading = ""
        self.anchors: list[dict] = []
        self.active_anchors: list[dict] = []
        self.rows: dict[int, list[str]] = {}
        self.row_id: int | None = None
        self.next_row_id = 0
        self.suppress = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self.suppress += 1
            return
        if self.suppress:
            return
        if tag == "title":
            self.in_title += 1
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.heading_level = tag
            self.heading_parts = []
        if tag == "tr":
            self.next_row_id += 1
            self.row_id = self.next_row_id
            self.rows[self.row_id] = []
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                a = {"href": href, "parts": [], "heading": self.current_heading,
                     "row": self.row_id}
                self.anchors.append(a)
                self.active_anchors.append(a)

    def handle_data(self, data: str) -> None:
        if self.suppress:
            return
        if self.in_title:
            self.title_parts.append(data)
        if self.heading_level is not None:
            self.heading_parts.append(data)
        if self.row_id is not None:
            self.rows[self.row_id].append(data)
        for anchor in self.active_anchors:
            anchor["parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self.suppress = max(0, self.suppress - 1)
            return
        if self.suppress:
            return
        if tag == "title":
            self.in_title = max(0, self.in_title - 1)
        if tag == self.heading_level:
            self.current_heading = clean(" ".join(self.heading_parts))[:200]
            self.heading_level = None
        if tag == "tr":
            self.row_id = None
        if tag == "a" and self.active_anchors:
            self.active_anchors.pop()

    def result(self) -> HtmlPage:
        links = []
        for a in self.anchors:
            row = clean(" ".join(self.rows.get(a["row"], [])))[:250]
            pieces = [a["heading"], row]
            context = " | ".join(x for x in pieces if x)
            links.append(ExtractedLink(a["href"], clean(" ".join(a["parts"]))[:250], context))
        return HtmlPage(clean(" ".join(self.title_parts))[:350], links)

def extract_links(html: str) -> HtmlPage:
    parser = LinkParser()
    parser.feed(html)
    parser.close()
    return parser.result()
