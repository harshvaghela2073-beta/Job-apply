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


def visible_text(html: str) -> str:
    """Page text without scripts and styles, whitespace collapsed."""
    doc = soup(html)
    for tag in doc(["script", "style", "noscript", "template"]):
        tag.decompose()
    return " ".join(doc.get_text(" ", strip=True).split())


def strip_tags(html_fragment: str | None) -> str:
    if not html_fragment:
        return ""
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", html_fragment)).split())
