"""Parsers for LinkedIn's job search (logged in; the public cards are kept for reference) and
logged-in hiring posts."""

import json
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import unquote, urlparse

from app.browser.driver import PageResult
from app.sources.html import first, select_any, soup, text_of, visible_text
from app.sources.linkedin import selectors as S

_JOB_URN = re.compile(r"urn:li:jobPosting:(\d+)")
_JOB_ID_IN_URL = re.compile(r"/jobs/view/(?:[^/?#]*?-)?(\d{6,})")
_POST_URN = re.compile(r"urn:li:(?:activity|ugcPost|share):\d+")
_FORM_LINK = re.compile(
    r"(?:https?://)?(?:docs\.google\.com/forms/[^\s\"'<>()\[\]{}]+|forms\.gle/[A-Za-z0-9_-]+)", re.I
)
_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_NOT_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
_SHORT_LINK = re.compile(r"https?://(?:" + "|".join(re.escape(h) for h in S.SHORT_LINK_HOSTS) + r")/[A-Za-z0-9_-]+")
_COMPACT_TIME = re.compile(r"\b(\d+)\s*(mo|yr|m|h|d|w|y)\b\s*(?:•|·|ago)")
_PROFILE_SLUG = re.compile(r"^/in/([^/?#]+)")


# ---------------------------------------------------------------------------
# Public job search


@dataclass
class JobCard:
    job_id: str
    title: str
    company: str
    location: str
    url: str
    posted_label: str | None = None
    posted_attr: str | None = None
    benefit: str | None = None


def parse_job_cards(html: str) -> list[JobCard]:
    doc = soup(html)
    cards: list[JobCard] = []
    seen: set[str] = set()
    for card in select_any(doc, S.JOB_CARD):
        job_id = None
        urn = card.get("data-entity-urn") or ""
        match = _JOB_URN.search(urn)
        link = first(card, S.JOB_CARD_LINK)
        if match:
            job_id = match.group(1)
        elif link is not None:
            match = _JOB_ID_IN_URL.search(link.get("href", ""))
            job_id = match.group(1) if match else None
        title = text_of(first(card, S.JOB_CARD_TITLE)) or text_of(link)
        if not job_id or not title or job_id in seen:
            continue
        seen.add(job_id)
        time_tag = first(card, S.JOB_CARD_TIME)
        cards.append(
            JobCard(
                job_id=job_id,
                title=title,
                company=text_of(first(card, S.JOB_CARD_COMPANY)),
                location=text_of(first(card, S.JOB_CARD_LOCATION)),
                url=S.JOB_VIEW_URL.format(id=job_id),
                posted_label=text_of(time_tag) or None,
                posted_attr=time_tag.get("datetime") if time_tag is not None else None,
                benefit=text_of(first(card, S.JOB_CARD_BENEFIT)) or None,
            )
        )
    return cards


def parse_result_count(html: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text_of(first(soup(html), S.RESULT_COUNT)))
    return int(digits) if digits else None


def guest_block_reason(result: PageResult) -> str | None:
    if result.status in S.BLOCK_STATUSES:
        return f"LinkedIn returned HTTP {result.status} to the public job search"
    url = result.url.lower()
    for marker in S.GUEST_BLOCK_URL_MARKERS:
        if marker in url:
            return f"LinkedIn redirected the public job search to {marker.strip('/')}"
    return None


# ---------------------------------------------------------------------------
# Logged-in job search (needs a live check)
#
# The page embeds LinkedIn's own (Voyager) JSON in <code id="bpr-guid-..."> elements, and the list
# fetches more cards from voyagerJobsDashJobCards. Both are read first; the rendered cards fill in
# what the JSON doesn't have. Field names below are the ones LinkedIn's JSON used in 2024-2026:
# JobPostingCard (jobPostingTitle, primaryDescription = company, secondaryDescription = location,
# footerItems with LISTED_DATE / EASY_APPLY_TEXT) and JobPosting (title, formattedLocation,
# listedAt, applyMethod: ...OnsiteApply = Easy Apply, ...OffsiteApply = company site).

_SEARCH_JOB_ID = re.compile(r"jobPosting(?:Card)?:\(?(\d{6,})")
_JSON_ID_KEYS = (
    "jobPostingUrn", "*jobPosting", "jobPosting", "preDashNormalizedJobPostingUrn", "entityUrn", "trackingUrn",
    "objectUrn",
)
_POSTING_KEYS = ("jobPostingTitle", "formattedLocation", "listedAt", "applyMethod", "footerItems")


@dataclass
class SearchJob:
    job_id: str
    title: str = ""
    company: str = ""
    location: str = ""
    listed_at: datetime | None = None
    posted_label: str | None = None
    posted_attr: str | None = None
    easy_apply: bool | None = None  # None: the page didn't say
    insight: str | None = None  # "Actively reviewing applicants", ...

    @property
    def url(self) -> str:
        return S.JOB_VIEW_URL.format(id=self.job_id)

    def merge(self, other: "SearchJob") -> None:
        for name in ("title", "company", "location", "listed_at", "posted_label", "posted_attr", "insight"):
            if not getattr(self, name) and getattr(other, name):
                setattr(self, name, getattr(other, name))
        if other.easy_apply is True or (self.easy_apply is None and other.easy_apply is not None):
            self.easy_apply = other.easy_apply


@dataclass
class SearchPage:
    jobs: list[SearchJob] = field(default_factory=list)
    total: int | None = None
    no_results: bool = False
    json_jobs: int = 0  # jobs read from JSON (diagnostics)
    dom_jobs: int = 0  # jobs read from the rendered cards
    # Job ids the JSON's result list names (``*jobPostingCard`` / ``*elements`` urns), in order.
    listed: list[str] = field(default_factory=list)

    @property
    def readable(self) -> list[SearchJob]:
        return [job for job in self.jobs if job.title]

    @property
    def missing(self) -> list[str]:
        """Listed jobs the data gave no details for (a "Lite" list: cards fetched separately)."""
        titled = {job.job_id for job in self.jobs if job.title}
        return [job_id for job_id in self.listed if job_id not in titled]


def _dedupe_text(text: str) -> str:
    """LinkedIn repeats texts for screen readers: "Backend Developer Backend Developer" -> once."""
    text = " ".join(text.split())
    words = text.split(" ")
    half = len(words) // 2
    if len(words) % 2 == 0 and half and words[:half] == words[half:]:
        return " ".join(words[:half])
    return text


def _json_text(value) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return " ".join(value["text"].split())
    return ""


def _json_job_id(item: dict) -> str | None:
    for key in _JSON_ID_KEYS:
        value = item.get(key)
        if isinstance(value, str):
            match = _SEARCH_JOB_ID.search(value)
            if match:
                return match.group(1)
    value = item.get("jobPostingId")
    if isinstance(value, (int, str)) and str(value).isdigit():
        return str(value)
    return None


def _company_name(value, depth: int = 0) -> str:
    """The first "name" inside companyDetails (any nesting LinkedIn uses)."""
    if depth > 4:
        return ""
    if isinstance(value, dict):
        if isinstance(value.get("name"), str):
            return value["name"].strip()
        for inner in value.values():
            found = _company_name(inner, depth + 1)
            if found:
                return found
    return ""


def _epoch(value) -> datetime | None:
    if isinstance(value, (int, float)) and value > 10**11:  # milliseconds
        return datetime.fromtimestamp(value / 1000, UTC)
    return None


def _json_job(item: dict) -> SearchJob | None:
    job_id = _json_job_id(item)
    if job_id is None:
        return None
    type_name = str(item.get("$type", "")).lower()
    # A job card / posting itself (not one of the entities that only point at a job).
    posting = type_name.endswith((".jobposting", ".jobpostingcard")) or any(key in item for key in _POSTING_KEYS)
    if not posting:
        return None
    job = SearchJob(job_id)
    job.title = _json_text(item.get("jobPostingTitle")) or _json_text(item.get("title"))
    job.company = (
        _json_text(item.get("primaryDescription"))
        or _json_text(item.get("companyName"))
        or _company_name(item.get("companyDetails"))
    )
    job.location = _json_text(item.get("secondaryDescription")) or _json_text(item.get("formattedLocation"))
    job.listed_at = _epoch(item.get("listedAt"))
    footer = item.get("footerItems")
    if isinstance(footer, list) and footer:
        kinds = [str(f.get("type", "")) for f in footer if isinstance(f, dict)]
        texts = [_json_text(f.get("text")).lower() for f in footer if isinstance(f, dict)]
        job.easy_apply = "EASY_APPLY_TEXT" in kinds or any(S.EASY_APPLY_TEXT in t for t in texts)
        for f in footer:
            if isinstance(f, dict) and f.get("type") == "LISTED_DATE" and job.listed_at is None:
                job.listed_at = _epoch(f.get("timeAt"))
    apply = item.get("applyMethod")
    if isinstance(apply, dict):
        described = (str(apply.get("$type", "")) + " " + " ".join(apply)).lower()
        if "onsiteapply" in described or "easyapplyurl" in described:
            job.easy_apply = True
        elif ("offsiteapply" in described or "companyapplyurl" in described) and job.easy_apply is None:
            job.easy_apply = False
    insight = item.get("relevanceInsight") or item.get("tertiaryDescription")
    if isinstance(insight, dict) and isinstance(insight.get("text"), dict):
        insight = insight["text"]
    job.insight = _json_text(insight) or None
    return job


_CARD_KEYS = ("*jobPostingCard", "jobPostingCardUrn")
_CARD_LISTS = ("*elements", "prefetchJobPostingCardUrns")


def _listed_ids(item: dict) -> list[str]:
    """Job ids a result-list entry points at (``jobCardUnion: {"*jobPostingCard": urn}``, or the
    ``*elements`` urns of a collection)."""
    ids = []
    for key in _CARD_KEYS:
        value = item.get(key)
        if isinstance(value, str):
            match = _SEARCH_JOB_ID.search(value)
            if match:
                ids.append(match.group(1))
    for key in _CARD_LISTS:
        values = item.get(key)
        for value in values if isinstance(values, list) else ():
            match = _SEARCH_JOB_ID.search(value) if isinstance(value, str) else None
            if match:
                ids.append(match.group(1))
    return ids


def _json_documents(html: str, captured: list[str]):
    for code in soup(html).select(S.SEARCH_JSON_BLOCKS):
        text = code.get_text().strip()
        if text[:1] in ("{", "["):
            try:
                yield json.loads(text)
            except ValueError:
                continue
    for body in captured:
        try:
            yield json.loads(body)
        except (TypeError, ValueError):
            continue


def _walk(document):
    queue = deque([document])
    while queue:
        item = queue.popleft()
        if isinstance(item, dict):
            yield item
            queue.extend(v for v in item.values() if isinstance(v, (dict, list)))
        elif isinstance(item, list):
            queue.extend(v for v in item if isinstance(v, (dict, list)))


def _json_total(item: dict) -> int | None:
    paging = item.get("paging")
    if not isinstance(paging, dict) or not isinstance(paging.get("total"), int):
        return None
    elements = item.get("*elements", item.get("elements"))
    if elements is None or "jobposting" not in json.dumps(elements)[:5000].lower():
        return None
    return paging["total"]


def _dom_jobs(html: str) -> list[SearchJob]:
    doc = soup(html)
    jobs: dict[str, SearchJob] = {}
    for selector in S.SEARCH_CARD:
        for card in doc.select(selector):
            job_id = next((card.get(attr) for attr in S.SEARCH_CARD_ID_ATTRS if card.get(attr)), None)
            link = first(card, S.SEARCH_CARD_LINK)
            if not job_id and link is not None:
                match = _JOB_ID_IN_URL.search(link.get("href", ""))
                job_id = match.group(1) if match else None
            if not job_id or not str(job_id).isdigit():
                continue
            title = _dedupe_text(text_of(first(card, S.SEARCH_CARD_TITLE)))
            if not title and link is not None:
                title = _dedupe_text(link.get("aria-label") or text_of(link))
            title = re.sub(r"\s+with verification$", "", title)
            job = SearchJob(str(job_id), title=title)
            job.company = _dedupe_text(text_of(first(card, S.SEARCH_CARD_COMPANY)))
            job.location = _dedupe_text(text_of(first(card, S.SEARCH_CARD_LOCATION)))
            card_text = text_of(card).lower()
            footer = [item for selector in S.SEARCH_CARD_FOOTER for item in card.select(selector)]
            footer_rendered = any(S.SEARCH_CARD_GHOST not in " ".join(item.get("class") or []) for item in footer)
            if S.EASY_APPLY_TEXT in card_text:
                job.easy_apply = True
            elif title and footer_rendered:
                job.easy_apply = False
            time_tag = first(card, S.SEARCH_CARD_TIME)
            if time_tag is not None:
                job.posted_label = text_of(time_tag) or None
                job.posted_attr = time_tag.get("datetime")
            job.insight = text_of(first(card, S.SEARCH_CARD_INSIGHT)) or None
            if job.job_id in jobs:
                jobs[job.job_id].merge(job)
            else:
                jobs[job.job_id] = job
    return list(jobs.values())


def _read_json(documents, page: SearchPage) -> dict[str, SearchJob]:
    jobs: dict[str, SearchJob] = {}
    totals: list[int] = []
    found: list[tuple[int, int, SearchJob]] = []
    for document in documents:
        for item in _walk(document):
            total = _json_total(item)
            if total is not None:
                totals.append(total)
            for job_id in _listed_ids(item):
                if job_id not in page.listed:
                    page.listed.append(job_id)
            job = _json_job(item)
            if job is not None:
                # Cards (jobPostingTitle) first: their title, company and location are the ones shown.
                found.append((0 if "jobPostingTitle" in item else 1, len(found), job))
    for _, _, job in sorted(found, key=lambda entry: entry[:2]):
        if job.job_id in jobs:
            jobs[job.job_id].merge(job)
        else:
            jobs[job.job_id] = job
    page.json_jobs = sum(1 for job in jobs.values() if job.title)
    if totals:
        page.total = max(totals)
    return jobs


def _answer_total(document: object) -> int | None:
    """``paging.total`` of the answer's own collection (``data`` for REST, ``data.data.<name>`` for
    GraphQL), also when the list is empty."""
    data = document.get("data") if isinstance(document, dict) else None
    candidates = [data] if isinstance(data, dict) else []
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        candidates += [v for v in data["data"].values() if isinstance(v, dict)]
    for item in candidates:
        paging = item.get("paging")
        if isinstance(paging, dict) and isinstance(paging.get("total"), int):
            return paging["total"]
    return None


def parse_search_data(document: object) -> SearchPage:
    """Jobs in an answer of LinkedIn's job-data request (``search_api``), already parsed JSON."""
    page = SearchPage()
    jobs = _read_json([document], page)
    page.jobs = list(jobs.values())
    if page.total is None:
        page.total = _answer_total(document)
    page.no_results = page.total == 0 and not page.listed
    return page


def parse_search_page(html: str, captured: list[str] | tuple[str, ...] = ()) -> SearchPage:
    """Jobs on a logged-in search results page: the embedded / captured JSON first, then the
    rendered cards. Jobs keep the order they were first seen in."""
    page = SearchPage()
    jobs = _read_json(_json_documents(html, list(captured)), page)
    dom = _dom_jobs(html)
    page.dom_jobs = sum(1 for job in dom if job.title)
    for job in dom:
        if job.job_id in jobs:
            jobs[job.job_id].merge(job)
        else:
            jobs[job.job_id] = job
    page.jobs = list(jobs.values())
    doc = soup(html)
    if page.total is None:
        digits = re.sub(r"[^\d]", "", text_of(first(doc, S.SEARCH_RESULT_COUNT)).split(" result")[0])
        page.total = int(digits) if digits else None
    for code in doc.select(S.SEARCH_JSON_BLOCKS):
        code.decompose()
    lowered = text_of(doc.body or doc).lower()
    page.no_results = first(doc, S.SEARCH_NO_RESULTS) is not None or any(t in lowered for t in S.SEARCH_NO_RESULTS_TEXT)
    return page


def search_block_reason(result: PageResult) -> str | None:
    """Why a logged-in search page counts as blocked (HTTP 429 / 999 / 403), or None."""
    if result.status in S.BLOCK_STATUSES:
        return f"LinkedIn returned HTTP {result.status} to the job search"
    return None


# ---------------------------------------------------------------------------
# Login


def account_name(html: str) -> str | None:
    doc = soup(html)
    for selector in S.ACCOUNT_NAME_IMAGES:
        image = doc.select_one(selector)
        if image is not None and (image.get("alt") or "").strip():
            name = image["alt"].strip()
            return re.sub(r"^(?:photo of|profile photo of)\s+", "", name, flags=re.I)[:80]
    return text_of(first(doc, S.ACCOUNT_NAME_TEXT))[:80] or None


def account_name_from_me(data: object) -> str | None:
    """First + last name from a voyager /me response (``included`` MiniProfile)."""
    if not isinstance(data, dict):
        return None
    candidates = []
    included = data.get("included")
    if isinstance(included, list):
        candidates.extend(item for item in included if isinstance(item, dict))
    for key in ("data", "miniProfile"):
        value = data.get(key)
        if isinstance(value, dict):
            candidates.append(value)
            mini = value.get("miniProfile") if isinstance(value.get("miniProfile"), dict) else None
            if mini:
                candidates.append(mini)
    for item in candidates:
        first, last = item.get("firstName"), item.get("lastName")
        if isinstance(first, str) and first.strip():
            return " ".join(part for part in (first.strip(), (last or "").strip()) if part)[:80]
    return None


def page_logged_in(html: str) -> bool:
    """Whether /feed/ shows the signed-in navigation or profile, and no "Sign in" / "Join now"."""
    doc = soup(html)
    if first(doc, S.LOGGED_OUT_MARKERS) is not None:
        return False
    for element in doc.select("a, button"):
        if text_of(element).lower() in S.LOGGED_OUT_TEXTS:
            return False
    return first(doc, S.LOGGED_IN_MARKERS) is not None


def chrome_login_cookie(name: str) -> bool:
    return name in S.AUTH_COOKIES


# ---------------------------------------------------------------------------
# Hiring posts


@dataclass
class Post:
    urn: str
    text: str
    author: str = ""
    headline: str = ""
    time_label: str | None = None
    links: list[str] = field(default_factory=list)
    author_url: str | None = None  # the author's profile (None for a company page or when not shown)
    # Where "Source post" points when the page shows no post URN (the SDUI layout): the author's
    # recent activity, or the company's posts page.
    link: str | None = None

    @property
    def url(self) -> str:
        return self.link or S.POST_URL.format(urn=self.urn)


def _post_containers(doc):
    containers = select_any(doc, S.POST_CONTAINER)
    if containers:
        return containers
    # Unknown markup: the first element (in document order) carrying each post URN.
    found = {}
    for tag in doc.find_all(True):
        for value in tag.attrs.values():
            value = " ".join(value) if isinstance(value, list) else str(value)
            match = _POST_URN.search(value)
            if match and match.group() not in found:
                found[match.group()] = tag
    return list(found.values())


_SDUI_KEY = re.compile(r"^expanded(.+?)FeedType_")
_DEGREE = re.compile(r"\s*(?:•|·)?\s*(?:1st|2nd|3rd\+?|Verified Profile|Premium|Following)\b.*$", re.I)


def _actor_link(wrapper):
    """The first link to the post's author (a member or a company) that shows a name."""
    for link in wrapper.select("a[href]"):
        href = link.get("href") or ""
        if ("/in/" in href or "/company/" in href or "/school/" in href or "/showcase/" in href) and link.get_text(strip=True):
            return link
    return None


def _parse_sdui_posts(doc) -> list[Post]:
    posts: list[Post] = []
    seen: set[str] = set()
    for wrapper in doc.select(S.SDUI_POST):
        key = _SDUI_KEY.match(wrapper.get("componentkey", ""))
        box = first(wrapper, S.SDUI_POST_TEXT)
        if key is None or box is None or key.group(1) in seen:
            continue
        seen.add(key.group(1))
        actor = _actor_link(wrapper)
        author = _DEGREE.sub("", " ".join(actor.get_text(" ").split())).strip() if actor else ""
        href = (actor.get("href") or "") if actor else ""
        author_url = normalize_profile_url(href) if "/in/" in href else None
        if author_url:
            link = author_url.rstrip("/") + "/recent-activity/all/"
        elif href:
            link = href.split("?")[0].rstrip("/") + "/posts/"
        else:
            link = None
        # "… more" is a button inside the text box (saved page, 2026-10-14); the full text is there.
        for button in box.select(", ".join(S.SDUI_POST_MORE)):
            button.decompose()
        flat = " ".join(wrapper.get_text(" ").split())
        time_match = _COMPACT_TIME.search(flat)
        headline = ""
        if author and time_match:
            start = flat.find(author)
            if start >= 0:
                between = flat[start + len(author) : time_match.start()]
                headline = _DEGREE.sub("", between).strip(" •·") if not between.strip().startswith(("•", "·")) else re.sub(
                    r"^[\s•·]*(?:1st|2nd|3rd\+?)?\s*", "", between
                ).strip()
        posts.append(
            Post(
                urn=f"sdui:{key.group(1)}",
                text=box.get_text("\n", strip=True),
                author=author,
                headline=headline,
                time_label=time_match.group(0) if time_match else None,
                links=[a.get("href", "") for a in box.select("a[href]")],
                author_url=author_url,
                link=link,
            )
        )
    return posts


def parse_posts(html: str) -> list[Post]:
    doc = soup(html)
    sdui = _parse_sdui_posts(doc)
    if sdui:
        return sdui
    posts: list[Post] = []
    seen: set[str] = set()
    for container in _post_containers(doc):
        urn_match = None
        for value in (container.get("data-urn"), container.get("data-chameleon-result-urn"), str(container)[:2000]):
            urn_match = _POST_URN.search(value or "")
            if urn_match:
                break
        if urn_match is None or urn_match.group() in seen:
            continue
        seen.add(urn_match.group())
        text_node = first(container, S.POST_TEXT)
        text = text_node.get_text("\n", strip=True) if text_node is not None else container.get_text("\n", strip=True)
        time_label = text_of(first(container, S.POST_TIME)) or None
        if time_label is None:
            compact = _COMPACT_TIME.search(container.get_text(" ", strip=True))
            time_label = compact.group(0) if compact else None
        posts.append(
            Post(
                urn=urn_match.group(),
                text=text,
                author=text_of(first(container, S.POST_ACTOR_NAME)),
                headline=text_of(first(container, S.POST_ACTOR_HEADLINE)),
                time_label=time_label,
                links=[a.get("href", "") for a in container.select("a[href]")],
                author_url=post_author_url(container),
            )
        )
    return posts


def post_author_url(container) -> str | None:
    """The post author's profile URL from the actor block; None when a company page posted it."""
    for selector in S.POST_ACTOR_LINK:
        for link in container.select(selector):
            href = link.get("href") or ""
            if "/in/" in href:
                return normalize_profile_url(href)
            if "/company/" in href or "/school/" in href or "/showcase/" in href:
                return None
    return None


def find_form_links(*texts: str) -> list[str]:
    links: list[str] = []
    for text in texts:
        for match in _FORM_LINK.finditer(unquote(text or "")):
            link = match.group().rstrip(".,;:!?")
            if not link.lower().startswith("http"):
                link = "https://" + link
            if link not in links:
                links.append(link)
    return links


def find_emails(*texts: str) -> list[str]:
    emails: list[str] = []
    for text in texts:
        for match in _EMAIL.finditer(text or ""):
            email = match.group().rstrip(".").lower()
            if email.endswith(_NOT_EMAIL_SUFFIXES) or email in emails:
                continue
            emails.append(email)
    return emails


def find_short_links(*texts: str) -> list[str]:
    links: list[str] = []
    for text in texts:
        for match in _SHORT_LINK.finditer(text or ""):
            if match.group() not in links:
                links.append(match.group())
    return links


def company_from_headline(headline: str) -> str | None:
    """"Talent Acquisition at Acme | Hiring" -> "Acme"."""
    match = re.search(r"(?:\bat\b|@)\s*([A-Za-z0-9][\w&.,' -]{1,60}?)(?:\s*[|•·,(]|\s+-\s|$)", headline)
    if not match:
        return None
    company = match.group(1).strip(" .,-")
    return company or None


# ---------------------------------------------------------------------------
# People: "Meet the hiring team" on job pages and profile pages (needs a live check)


def normalize_profile_url(href: str | None) -> str | None:
    """Any link to a member profile -> ``https://www.linkedin.com/in/<slug>/`` (lowercase slug, no
    query); None when it isn't one."""
    if not href:
        return None
    href = href.strip()
    if href.startswith("/"):
        href = S.BASE_URL + href
    parsed = urlparse(href if "://" in href else "https://" + href)
    host = (parsed.hostname or "").lower()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    match = _PROFILE_SLUG.match(parsed.path)
    if not match:
        return None
    return S.PROFILE_URL.format(slug=match.group(1).lower())


def clean_person_name(text: str) -> str:
    """A person's name as LinkedIn shows it, without repeats, pronouns or badges."""
    text = _dedupe_text(text)
    text = re.sub(r"\((?:she|he|they)/[^)]*\)", " ", text, flags=re.I)
    text = re.sub(r"\b(?:verified|premium)(?: profile)?\b|[\u2713\u2714]", " ", text, flags=re.I)
    text = re.sub(r"[·•].*$", "", text)  # "Priya Sharma · 2nd"
    return " ".join(text.split()).strip(" ,-")


@dataclass
class HiringPerson:
    name: str
    profile_url: str
    headline: str = ""
    degree: str = ""  # "1st", "2nd", "3rd" when shown
    job_poster: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name, "profile_url": self.profile_url, "headline": self.headline,
            "degree": self.degree, "job_poster": self.job_poster,
        }


def _degree(text: str) -> str:
    match = re.search(r"\b(1st|2nd|3rd\+?)\b", text or "")
    return match.group(1) if match else ""


def _hiring_cards(doc) -> list:
    cards = select_any(doc, S.HIRING_TEAM_CARD)
    if cards:
        return cards
    # Unknown markup (the 2026-10-14 layout has generated class names): the profile links in the
    # nearest block around the "Meet the hiring team" heading text.
    for string in doc.find_all(string=lambda s: s is not None and s.strip().lower() == S.HIRING_TEAM_HEADING):
        box = string.parent
        for _ in range(8):
            if box is None:
                break
            links = [a for a in box.select(S.PROFILE_LINK) if a.get_text(strip=True)]
            if links:
                return links
            box = box.parent
    return []


_LINK_DEGREE = re.compile(r"[·•]\s*(1st|2nd|3rd\+?)\b\s*(.*)$", re.S)


def parse_hiring_team(html: str) -> list[HiringPerson]:
    """The people in "Meet the hiring team" on a logged-in job page, the job poster first."""
    doc = soup(html)
    people: list[HiringPerson] = []
    seen: set[str] = set()
    for card in _hiring_cards(doc):
        link = card if card.name == "a" else card.select_one(S.PROFILE_LINK)
        url = normalize_profile_url(link.get("href") if link is not None else None)
        if url is None or url in seen:
            continue
        name_node = first(card, S.HIRING_TEAM_NAME) if card.name != "a" else None
        name = clean_person_name(text_of(name_node) or text_of(link) or (link.get("aria-label") or ""))
        if not name:
            image = card.select_one("img[alt]")
            name = clean_person_name(image.get("alt", "") if image is not None else "")
        if not name:
            continue
        seen.add(url)
        poster = first(card, S.HIRING_TEAM_POSTER) if card.name != "a" else None
        headline = ""
        if card.name != "a":
            headline_node = first(card, S.HIRING_TEAM_HEADLINE)
            headline = text_of(headline_node) if headline_node is not None and headline_node is not poster else ""
        degree = _degree(text_of(first(card, S.HIRING_TEAM_DEGREE)) if card.name != "a" else "")
        if card.name == "a":
            # New layout: the link holds "Priya Sharma • 2nd Talent Acquisition at Acme".
            extra = _LINK_DEGREE.search(" ".join(card.get_text(" ").split()))
            if extra:
                degree = degree or _degree(extra.group(1))
                headline = headline or extra.group(2).strip()
        people.append(
            HiringPerson(
                name=name,
                profile_url=url,
                headline="" if headline.lower() == S.JOB_POSTER_TEXT else headline,
                degree=degree,
                job_poster=S.JOB_POSTER_TEXT in text_of(poster).lower() or S.JOB_POSTER_TEXT in text_of(card).lower(),
            )
        )
    return sorted(people, key=lambda person: not person.job_poster)


def pick_contact(people: list[HiringPerson | dict]) -> HiringPerson | None:
    """The person to write to: the first one (the job poster comes first) who isn't a 1st-degree
    connection already."""
    for person in people:
        if isinstance(person, dict):
            person = HiringPerson(**person)
        if person.degree != "1st" and person.profile_url:
            return person
    return None


def _button_label(button) -> str:
    return " ".join((button.get("aria-label") or text_of(button)).split())


@dataclass
class ProfileView:
    """What a profile page shows, for sending a connection note."""

    name: str | None = None
    unavailable: bool = False
    buttons: list[str] = field(default_factory=list)  # the top card's buttons (accessible names)
    pending: bool = False
    dialog: bool = False
    dialog_buttons: list[str] = field(default_factory=list)
    note_box: bool = False  # the note textarea is open
    sent: bool = False  # "Invitation sent" toast
    weekly_limit: bool = False
    note_limit: bool = False
    email_required: bool = False
    error: str = ""


def read_profile(html: str) -> ProfileView:
    doc = soup(html)
    view = ProfileView()
    top = first(doc, S.PROFILE_TOP_CARD)
    name = text_of(first(top if top is not None else doc, S.PROFILE_NAME))
    view.name = clean_person_name(name) or None
    if top is not None:
        for button in top.select("button, [role='button'], a[role='button']"):
            label = _button_label(button)
            if label and label not in view.buttons:
                view.buttons.append(label)
    view.pending = any(re.search(S.PENDING_NAME, label, re.I) for label in view.buttons)
    dialogs = [node for selector in S.INVITE_DIALOG for node in doc.select(selector)]
    for dialog in dialogs:
        view.dialog = True
        for button in dialog.select("button"):
            label = _button_label(button)
            if label and label not in view.dialog_buttons:
                view.dialog_buttons.append(label)
        if any(dialog.select_one(f"textarea#{i}") for i in S.NOTE_TEXTAREA_IDS) or dialog.select_one("textarea"):
            view.note_box = True
    page_text = visible_text(html).lower()
    dialog_text = " ".join(text_of(d) for d in dialogs).lower()
    view.unavailable = top is None and any(t in page_text for t in S.PROFILE_UNAVAILABLE_TEXT)
    view.sent = any(t in page_text for t in S.INVITE_SENT_TEXT) and ("sent" in page_text)
    view.weekly_limit = any(t in page_text for t in S.WEEKLY_LIMIT_TEXT)
    view.note_limit = any(t in dialog_text for t in S.NOTE_LIMIT_TEXT)
    view.email_required = any(t in dialog_text for t in S.EMAIL_REQUIRED_TEXT)
    view.error = next((t for t in S.INVITE_ERROR_TEXT if t in page_text), "")
    return view
