"""Small HTML helpers for the parsers (BeautifulSoup with the stdlib parser)."""

import html as html_lib
import re
from collections.abc import Iterable

from bs4 import BeautifulSoup, Tag


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def first(node: Tag | BeautifulSoup, selectors: Iterable[str]) -> Tag | None:
    """The first element matching any selector, trying the selectors in order."""
    for selector in selectors:
        found = node.select_one(selector)
        if found is not None:
            return found
    return None


def select_any(node: Tag | BeautifulSoup, selectors: Iterable[str]) -> list[Tag]:
    """Elements matching the first selector that matches anything."""
    for selector in selectors:
        found = node.select(selector)
        if found:
            return found
    return []


def text_of(node: Tag | None) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)


def visible_text(html: str) -> str:
    """Page text without scripts, styles and hidden elements, whitespace collapsed. LinkedIn keeps its
    translations in ``<code style="display: none">`` blobs ("Sorry, something went wrong ..."), which
    were taken for an error after every connection note until 2026-10-27."""
    doc = soup(html)
    for tag in doc(["script", "style", "noscript", "template"]):
        tag.decompose()
    for tag in doc.find_all(lambda t: t.has_attr("hidden") or _HIDDEN_STYLE.search(t.get("style") or "")):
        if not tag.decomposed:
            tag.decompose()
    return " ".join(doc.get_text(" ", strip=True).split())


def strip_tags(html_fragment: str | None) -> str:
    if not html_fragment:
        return ""
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", html_fragment)).split())
