"""Parsers for Naukri's search results (JSON API, with an HTML fallback) and job pages."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urljoin, urlsplit

from app.browser.driver import PageResult
from app.models import ApplyType
from app.scanner.applying import ApplyAttempt, ApplyResult
from app.scanner.matching import parse_min_experience
from app.sources.html import first, select_any, soup, strip_tags, text_of, visible_text
from app.sources.naukri import selectors as S

_JOB_ID_IN_URL = re.compile(r"-(\d{6,})(?:[/?#]|$)")


@dataclass
class NaukriJob:
    job_id: str
    title: str
    company: str
    url: str
    location: str = ""
    experience: str | None = None
    min_experience: float | None = None
    posted_label: str | None = None
    created_at: datetime | None = None  # exact, from the JSON API
    todays_job: bool | None = None
    company_apply: bool | None = None  # None: unknown (HTML fallback)
    apply_redirect_url: str | None = None
    skills: str = ""
    description: str = ""


@dataclass
class SearchPage:
    jobs: list[NaukriJob]
    total: int | None = None
    logged_in: bool | None = None
    from_api: bool = True


def _placeholder(job: dict, kind: str) -> str:
    for item in job.get("placeholders") or []:
        if item.get("type") == kind:
            return str(item.get("label") or "")
    return ""


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_search_api(body: str) -> SearchPage | None:
    """The ``/jobapi/v3/search`` JSON, or None if ``body`` isn't that JSON."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("jobDetails"), list):
        return None
    total = data.get("noOfJobs")
    logged_in = data.get("isLoggedIn")
    return SearchPage(
        jobs=_api_jobs(data["jobDetails"]),
        total=total if isinstance(total, int) else None,
        logged_in=logged_in if isinstance(logged_in, bool) else None,
    )


def _api_jobs(items: list) -> list[NaukriJob]:
    """Jobs from a ``jobDetails`` list of Naukri's job API."""
    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("jobId") or "").strip()
        title = (item.get("title") or "").strip()
        if not job_id or not title:
            continue
        created = item.get("createdDate")
        experience = item.get("experienceText") or _placeholder(item, "experience") or None
        min_experience = _float(item.get("minimumExperience"))
        if min_experience is None:
            min_experience = parse_min_experience(experience)
        company_apply = item.get("companyApplyJob")
        jobs.append(
            NaukriJob(
                job_id=job_id,
                title=title,
                company=(item.get("companyName") or "").strip(),
                url=urljoin(S.BASE_URL, item.get("jdURL") or f"/job-listings-{job_id}"),
                location=_placeholder(item, "location"),
                experience=experience,
                min_experience=min_experience,
                posted_label=item.get("footerPlaceholderLabel"),
                created_at=(
                    datetime.fromtimestamp(created / 1000, tz=UTC)
                    if isinstance(created, (int, float))
                    else None
                ),
                todays_job=item.get("todaysJob"),
                company_apply=bool(company_apply) if company_apply is not None else None,
                apply_redirect_url=item.get("applyRedirectUrl") or None,
                skills=item.get("tagsAndSkills") or "",
                description=strip_tags(item.get("jobDescription")),
            )
        )
    return jobs


def _job_lists(data, depth: int = 0):
    """Every ``jobDetails`` list in a JSON value, nested up to 4 levels deep."""
    if depth > 4:
        return
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "jobDetails" and isinstance(value, list):
                yield value
            else:
                yield from _job_lists(value, depth + 1)
    elif isinstance(data, list):
        for value in data:
            yield from _job_lists(value, depth + 1)


def parse_search_html(html: str) -> SearchPage:
    """Result cards from the rendered page (fallback when the JSON wasn't captured)."""
    doc = soup(html)
    cards = select_any(doc, S.CARD)
    if not cards:  # unknown card markup: use each job link's parent block
        cards = [link.parent for link in doc.select(S.CARD_LINK) if link.parent is not None]
    jobs: list[NaukriJob] = []
    seen: set[str] = set()
    for card in cards:
        link = first(card, S.CARD_TITLE)
        href = link.get("href") if link is not None else None
        if not href:
            continue
        url = urljoin(S.BASE_URL, href.split("?")[0])
        job_id = str(card.get("data-job-id") or "")
        if not job_id:
            match = _JOB_ID_IN_URL.search(url)
            job_id = match.group(1) if match else ""
        title = text_of(link) or link.get("title", "")
        if not job_id or not title or job_id in seen:
            continue
        seen.add(job_id)
        experience = text_of(first(card, S.CARD_EXPERIENCE)) or None
        jobs.append(
            NaukriJob(
                job_id=job_id,
                title=title,
                company=text_of(first(card, S.CARD_COMPANY)),
                url=url,
                location=text_of(first(card, S.CARD_LOCATION)),
                experience=experience,
                min_experience=parse_min_experience(experience),
                posted_label=text_of(first(card, S.CARD_POSTED)) or None,
                description=text_of(first(card, S.CARD_DESCRIPTION)),
            )
        )
    return SearchPage(jobs=jobs, from_api=False)


def parse_search(result: PageResult) -> SearchPage:
    """Prefer the captured JSON; fall back to the HTML cards."""
    for response in result.captured:
        if S.SEARCH_API in response.url and response.status == 200:
            page = parse_search_api(response.body)
            if page is not None:
                return page
    return parse_search_html(result.html)


def parse_recommended(result: PageResult) -> SearchPage:
    """The logged-in "Recommended Jobs" list: jobs from any captured job-API JSON (a ``jobDetails``
    list anywhere in it), else the job cards on the page (needs a live check)."""
    jobs: list[NaukriJob] = []
    seen: set[str] = set()
    for response in result.captured:
        if response.status != 200:
            continue
        try:
            data = json.loads(response.body)
        except (TypeError, ValueError):
            continue
        for items in _job_lists(data):
            for job in _api_jobs(items):
                if job.job_id not in seen:
                    seen.add(job.job_id)
                    jobs.append(job)
    if jobs:
        return SearchPage(jobs=jobs)
    return parse_search_html(result.html)


def parse_job_page_apply(html: str) -> ApplyType | None:
    """The apply type a logged-in job page shows: ``company_site`` for "Apply on company site",
    ``one_click`` for Naukri's own Apply (or Applied) button, None if neither is there."""
    doc = soup(html)
    if first(doc, S.COMPANY_SITE_BUTTON) is not None:
        return ApplyType.company_site
    for button in doc.select("button, a"):
        label = text_of(button).lower()
        if any(text in label for text in S.COMPANY_SITE_TEXTS):
            return ApplyType.company_site
    if first(doc, S.APPLIED_MARKER) is not None:
        return ApplyType.one_click
    for button in select_any(doc, S.APPLY_BUTTON):
        if text_of(button).lower() in ("apply", "applied", "apply now"):
            return ApplyType.one_click
    return None


def already_applied(html: str) -> bool:
    """The job page shows Naukri's "Applied" marker or button."""
    doc = soup(html)
    return first(doc, S.APPLIED_MARKER) is not None or any(
        text_of(b).strip().lower() == "applied" for b in select_any(doc, S.APPLY_BUTTON)
    )


def job_closed(html: str) -> str | None:
    """The page's "no longer accepting applications" / "expired" text, or None."""
    text = visible_text(html).lower()
    return next((marker for marker in S.APPLY_CLOSED_TEXT if marker in text), None)


def page_logged_out(html: str) -> bool:
    """The header shows Naukri's Login / Register buttons (the saved login is gone). False while the
    header is still loading."""
    doc = soup(html)
    if first(doc, S.HEADER_LOADING) is not None:
        return False
    if first(doc, S.LOGGED_OUT_MARKERS) is not None:
        return True
    texts = {text_of(e).lower() for e in doc.select("header a, header button, .nI-gNb-header a, .nI-gNb-header button")}
    return set(S.LOGGED_OUT_TEXTS) <= texts


def save_apply_codes(url: str) -> dict[str, int]:
    """Naukri's answer per job on the saveApply page: ``multiApplyResp={"<job id>":200}``."""
    try:
        values = parse_qs(urlsplit(url).query).get(S.SAVE_APPLY_RESPONSE_PARAM) or []
        data = json.loads(values[0]) if values else {}
    except (ValueError, IndexError):
        return {}
    if not isinstance(data, dict):
        return {}
    codes = {}
    for job_id, code in data.items():
        try:
            codes[str(job_id)] = int(code)
        except (TypeError, ValueError):
            continue
    return codes


APPLIED_DETAIL = "Applied with Naukri's one-click Apply."


def apply_outcome(html: str, url: str) -> ApplyAttempt | None:
    """What the page shows after clicking Apply (or answering the recruiter's questions): applied, the
    daily quota, or None while nothing has changed yet. The questions drawer is read elsewhere."""
    lowered_url = (url or "").lower()
    if any(marker in lowered_url for marker in S.APPLY_SUCCESS_URL):
        failed = {job: code for job, code in save_apply_codes(url).items() if code != 200}
        if failed:
            codes = ", ".join(str(code) for code in sorted(set(failed.values())))
            return ApplyAttempt(ApplyResult.no_confirmation, f"Naukri's apply page answered {codes} instead of 200.")
        return ApplyAttempt(ApplyResult.applied, APPLIED_DETAIL)
    text = visible_text(html).lower()
    if any(t in text for t in S.APPLY_SUCCESS_TEXT) or already_applied(html):
        return ApplyAttempt(ApplyResult.applied, APPLIED_DETAIL)
    if any(t in text for t in S.APPLY_QUOTA_TEXT):
        return ApplyAttempt(ApplyResult.quota_reached, "Naukri says today's apply limit is reached.")
    return None


def page_logged_in(html: str) -> bool:
    """Whether the member page shows a logged-in member: a profile widget or name, and no
    Login / Register buttons in the header."""
    doc = soup(html)
    if first(doc, S.HEADER_LOADING) is not None or first(doc, S.LOGGED_OUT_MARKERS) is not None:
        return False
    for element in doc.select("header a, header button, .nI-gNb-header a, .nI-gNb-header button"):
        if text_of(element).lower() in S.LOGGED_OUT_TEXTS:
            return False
    return first(doc, S.LOGGED_IN_MARKERS) is not None


def chrome_login_cookie(name: str) -> bool:
    return name in S.CHROME_LOGIN_COOKIES or name.lower().startswith(S.CHROME_LOGIN_COOKIE_PREFIXES)


def account_name(html: str) -> str | None:
    name = text_of(first(soup(html), S.ACCOUNT_NAME))
    return name[:80] or None


def block_reason(result: PageResult) -> str | None:
    """Why the page looks blocked, or None."""
    if result.status in S.BLOCK_STATUSES:
        return f"Naukri returned HTTP {result.status}"
    text = visible_text(result.html).lower()[:5000]
    for marker in S.BLOCK_TEXT:
        if marker in text:
            return f"Naukri showed a block page ({marker!r})"
    return None
