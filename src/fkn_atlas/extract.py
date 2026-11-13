"""HTML evidence extraction; headings are context, never inferred course identities."""
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
    ordinal: int = 0


@dataclass(frozen=True)
class HtmlPage:
    title: str
    links: list[ExtractedLink]
    base_href: str | None = None


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.in_title = False
        self.heading_level = None
        self.headings = {}
        self.heading_parts = []
        self.anchors = []
        self.active = None
        self.blocks = []
        self.suppress = 0
        self.base_href = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.suppress += 1
            return
        if self.suppress:
            return
        attrs = dict(attrs)
        if tag == "base" and self.base_href is None:
            self.base_href = attrs.get("href")
        if tag == "title":
            self.in_title = True
        if re.fullmatch(r"h[1-6]", tag):
            self.heading_level = int(tag[1])
            self.headings = {k: v for k, v in self.headings.items() if k < self.heading_level}
            self.heading_parts = []
        if tag in ("tr", "li", "p"):
            # HTML permits omitted closing tags for list items, rows and paragraphs.
            for i in range(len(self.blocks) - 1, -1, -1):
                if self.blocks[i]["tag"] == tag:
                    del self.blocks[i:]
                    break
            self.blocks.append({"tag": tag, "parts": []})
        if tag == "a":
            self.active = None
            if attrs.get("href"):
                row = next((b for b in reversed(self.blocks) if b["tag"] == "tr"), None)
                block = row or (self.blocks[-1] if self.blocks else None)
                self.active = {"href": attrs["href"], "parts": [], "headings": list(self.headings.values()), "block": block}
                self.anchors.append(self.active)

    def handle_data(self, data):
        if self.suppress:
            return
        if self.in_title:
            self.title_parts.append(data)
        if self.heading_level is not None:
            self.heading_parts.append(data)
        for block in self.blocks:
            block["parts"].append(data)
        if self.active is not None:
            self.active["parts"].append(data)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.suppress = max(0, self.suppress - 1)
            return
        if self.suppress:
            return
        if tag == "title":
            self.in_title = False
        if self.heading_level is not None and tag == f"h{self.heading_level}":
            self.headings[self.heading_level] = clean(" ".join(self.heading_parts))
            self.heading_level = None
        if tag == "a":
            self.active = None
        for i in range(len(self.blocks) - 1, -1, -1):
            if self.blocks[i]["tag"] == tag:
                del self.blocks[i:]
                break

    def result(self):
        links = []
        for i, a in enumerate(self.anchors):
            block = clean(" ".join(a["block"]["parts"])) if a["block"] else ""
            context = " | ".join(x for x in a["headings"] + [block] if x)
            links.append(ExtractedLink(a["href"], clean(" ".join(a["parts"])), context, i))
        return HtmlPage(clean(" ".join(self.title_parts)), links, self.base_href)


def extract_links(html: str) -> HtmlPage:
    parser = LinkParser()
    parser.feed(html)
    parser.close()
    return parser.result()
