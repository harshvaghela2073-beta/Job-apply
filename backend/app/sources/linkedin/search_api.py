"""LinkedIn's own job-data request (``voyagerJobsDashJobCards``), reused for the job search.

Rendering a logged-in results page took about 5 minutes on 2026-10-14 (the tab keeps a CPU core
busy) and showed only ~7 of its 25 jobs. So each location's first results page is rendered once, the
list's own request is captured from the page's network traffic (``selectors.SEARCH_CAPTURE``), and
the other role groups and pages are fetched by calling that request again through the logged-in
context (``BrowserSession.api_get``) with the keywords, ``start`` and ``count`` changed.

Two URL shapes are understood (the captured one is used as it is otherwise; its decoration id and
filters are LinkedIn's own):

- REST: ``/voyager/api/voyagerJobsDashJobCards?decorationId=com.linkedin.voyager.dash.deco.jobs.
  search.JobSearchCardsCollection-220&count=25&q=jobSearch&query=(origin:JOB_SEARCH_PAGE_JOB_FILTER,
  keywords:<kw>,locationUnion:(geoId:105214831),selectedFilters:(sortBy:List(DD),timePostedRange:
  List(r86400),experience:List(2,3,4)),spellCorrectionEnabled:true)&start=0``
- GraphQL: ``/voyager/api/graphql?variables=(count:25,start:0,query:(...same...))&queryId=
  voyagerJobsDashJobCards.<hash>``

``query`` / ``variables`` are Rest.li 2.0 objects: ``(key:value,...)``, nested in parentheses, with
reserved characters inside strings percent-encoded. **Needs a live check**: the exact URL wasn't in
the saved search page (LinkedIn keeps it out of the HTML), so the two shapes above come from
LinkedIn's web client as it was known; whichever the page really sends is captured and reused.
"""

import re
from collections.abc import Iterable
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from app.browser.driver import CapturedResponse
from app.sources.linkedin import selectors as S

_HOST = re.compile(r"(^|\.)linkedin\.com$")


def _params(query: str) -> list[tuple[str, str]]:
    """The URL's query parameters, raw (not decoded): Rest.li values must keep their encoding."""
    pairs = []
    for part in query.split("&"):
        if part:
            key, _, value = part.partition("=")
            pairs.append((key, value))
    return pairs


def _split_top(body: str) -> list[str]:
    """``a:b,c:(d:e,f:g),h:List(x,y)`` -> ``["a:b", "c:(d:e,f:g)", "h:List(x,y)"]``."""
    parts, depth, current = [], 0, []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced parentheses")
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if depth != 0:
        raise ValueError("unbalanced parentheses")
    if current:
        parts.append("".join(current))
    return parts


def parse_object(text: str) -> list[tuple[str, str]]:
    """A Rest.li object ``(key:value,...)`` as ordered ``(key, raw value)`` pairs. A value that
    was percent-encoded as a whole (``%28...%29``) is decoded once first."""
    if text.startswith("%28"):
        text = unquote(text)
    if not (text.startswith("(") and text.endswith(")")):
        raise ValueError("not a Rest.li object")
    pairs = []
    for part in _split_top(text[1:-1]):
        key, colon, value = part.partition(":")
        if not colon or not key:
            raise ValueError(f"not a key:value pair: {part[:40]}")
        pairs.append((key, value))
    return pairs


def format_object(pairs: list[tuple[str, str]]) -> str:
    return "(" + ",".join(f"{key}:{value}" for key, value in pairs) + ")"


def _put(pairs: list[tuple[str, str]], key: str, value: str, after: str | None = None) -> list[tuple[str, str]]:
    """Set ``key`` (in place when present, else after ``after`` / at the end)."""
    if any(k == key for k, _ in pairs):
        return [(k, value if k == key else v) for k, v in pairs]
    index = next((i + 1 for i, (k, _) in enumerate(pairs) if k == after), len(pairs))
    return [*pairs[:index], (key, value), *pairs[index:]]


def restli_string(text: str) -> str:
    """A string value inside a Rest.li object in a URL: every reserved character encoded."""
    return quote(text, safe="")


def _with_keywords(query_value: str, keywords: str) -> str:
    pairs = [(k, v) for k, v in parse_object(query_value) if k != "currentJobId"]  # the job shown on the page
    return format_object(_put(pairs, "keywords", restli_string(keywords), after="origin"))


def is_linkedin(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return bool(_HOST.search(host)) and urlsplit(url).scheme == "https"


def is_listing_request(url: str) -> bool:
    """Whether ``url`` is the job search's list request (not a single-card or other request)."""
    if S.SEARCH_CAPTURE not in url or not is_linkedin(url) or "/voyager/api/" not in url:
        return False
    params = dict(_params(urlsplit(url).query))
    if params.get("q") == "jobSearch" and "query" in params:
        return True
    variables = unquote(params.get("variables", ""))
    return S.SEARCH_CAPTURE in params.get("queryId", "") and "query:(" in variables


def pick_template(captured: Iterable[CapturedResponse]) -> str | None:
    """The captured list request to reuse: an answered (200) one first, the full card collection
    ("...CardsCollection") before a "...Lite" one."""
    found = [response for response in captured if is_listing_request(response.url)]
    found.sort(key=lambda response: (response.status != 200, "lite" in response.url.lower()))
    return found[0].url if found else None


def rewrite(template: str, keywords: str, start: int, count: int = S.SEARCH_PAGE_SIZE) -> str:
    """The template request for other keywords and another page. Raises ``ValueError`` when the
    template can't be understood."""
    parts = urlsplit(template)
    params = _params(parts.query)
    keys = [key for key, _ in params]
    if "query" in keys:  # REST
        params = [
            (key, _with_keywords(value, keywords) if key == "query" else value)
            for key, value in params
        ]
        params = _put(_put(params, "count", str(count)), "start", str(start))
    elif "variables" in keys:  # GraphQL
        rewritten = []
        for key, value in params:
            if key == "variables":
                variables = parse_object(value)
                query = next((v for k, v in variables if k == "query"), None)
                if query is None:
                    raise ValueError("no query in the variables")
                variables = _put(variables, "query", _with_keywords(query, keywords))
                variables = _put(_put(variables, "count", str(count)), "start", str(start))
                value = format_object(variables)
            rewritten.append((key, value))
        params = rewritten
    else:
        raise ValueError("neither query nor variables in the request")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(f"{k}={v}" for k, v in params), ""))


def headers(csrf_token: str) -> dict[str, str]:
    return {**S.SEARCH_API_HEADERS, "csrf-token": csrf_token}
