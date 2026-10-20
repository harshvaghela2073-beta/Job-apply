"""``linkedin_jobs``: LinkedIn job search **with the saved LinkedIn login** (docs/API_CONTRACT.md,
"LinkedIn search and Easy Apply"). Since 2026-10-13 logged-out searches get HTTP 429 or a login
wall from the candidate's connection, so the public pages are no longer used.

- Runs at most every ``LINKEDIN_SEARCH_INTERVAL_MINUTES`` (60): a scan in between records ``skipped``
  with "Next LinkedIn search after HH:MM" (Asia/Kolkata). Skipped with a message when LinkedIn isn't
  connected; a login wall or security check marks LinkedIn as disconnected and stops the run.
- Every search runs first with LinkedIn's "Easy Apply" filter (``f_AL=true``: the jobs the tool can
  apply to; a job it lists is Easy Apply even when its card doesn't say), then without it (company-site
  jobs for the dashboard). Added 2026-10-15: unfiltered results were mostly company-site jobs.
- One search per location and role group: jobs from the past 24 hours (``f_TPR=r86400``), newest
  first, at the experience levels from ``search_experience_order`` (1+ years -> Entry, 2+ -> Entry and
  Associate, 3+ -> Associate and Mid-Senior: ``f_E=2,3,4``). "Remote" searches ``location=India``
  with ``f_WT=2``. Roles are OR-ed in groups of at most 4 roles / 100 characters.
- **Fast path (``search_api.py``).** Rendering a results page took ~5 minutes on 2026-10-14 and
  showed ~7 of its 25 jobs. So per location only the first search is rendered; its own job-data
  request (``voyagerJobsDashJobCards``) is captured from the page's traffic and called again through
  the logged-in context for the other role groups and pages (keywords, ``start``, ``count=25``
  changed). The rendered page is also re-read through the request when it showed only part of its
  list. If the request isn't seen, answers non-200, is redirected or can't be parsed, that location's
  pages are rendered as before, and the run message says so. HTTP 429 / 999 stops the run.
- Rendered pages are read from LinkedIn's embedded JSON and the rendered cards (the list is scrolled
  so its lazy cards render); a card whose footer hasn't rendered doesn't say whether it's Easy Apply.
  A job is ``easy_apply`` when LinkedIn marks it Easy Apply, otherwise ``company_site``.
- Budget per run: ``MAX_RENDERS`` rendered pages and ``MAX_API_CALLS`` job-data requests, with
  2-6 s pauses between all of them: the first page of every search, then further pages
  (``start=25``) in priority order. Each call is logged with its location, role group, start, jobs
  read and source (api / page).
- The usual matching rules (``app.scanner.matching``): role (with synonyms), not an internship,
  minimum experience at most 3 years, a preferred location, posted today. A job whose date the page
  doesn't give is kept: the search itself covers only the past 24 hours.
- Job fit (``app.scanner.fit``, 2026-10-15): a job that matches but whose company is on the skip list
  or whose title is for another stack or too senior isn't stored; the run message counts them
  ("12 left out: 7 another stack, 5 senior.").
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func
from sqlmodel import Session, col, select

from app.browser import BrowserSession, BrowserUnavailable
from app.models import ApplyType, Platform, ScanKind, ScanRun, ScanStatus
from app.scanner.base import (
    Candidate,
    LoginWall,
    Pacer,
    ScanBlocked,
    ScanContext,
    ScanOutcome,
    ScanReport,
    capture_failure,
)
from app.scanner.fit import LEFT_OUT, left_out_note, search_left_out
from app.scanner.matching import (
    RoleMatcher,
    SearchPrefs,
    estimate_posted_at,
    experience_phrase,
    is_remote_location,
    is_today,
    match_title,
    normalize,
    parse_date_attr,
    priority,
)
from app.scanner.store import finish_scan
from app.services.app_settings import LINKEDIN_SEARCH_INTERVAL, get_minutes
from app.sources.linkedin import search_api
from app.sources.linkedin import selectors as S
from app.sources.linkedin.login import LINKEDIN_LOGIN, csrf_token
from app.sources.linkedin.parse import (
    SearchJob,
    SearchPage,
    parse_search_data,
    parse_search_page,
    search_block_reason,
)
from app.timeutils import to_utc

logger = logging.getLogger(__name__)

# Rendered results pages per run (~5 minutes each on 2026-10-14): one per location normally; the
# rest are fallbacks when LinkedIn's job-data request doesn't work.
# Native Chrome renders a results page in ~16 s (2026-10-15; ~5 minutes under Rosetta before).
MAX_RENDERS = 12
MAX_API_CALLS = 12  # job-data requests per run (25 jobs each)
# A scheduled scan a little early (scans run every 30 minutes) still counts as due.
INTERVAL_SLACK = timedelta(minutes=2)
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
NOT_CONNECTED = "LinkedIn isn't connected, so jobs weren't searched. Connect LinkedIn to include them."
LOGIN_EXPIRED = "The saved LinkedIn login has expired. Connect again."
SECURITY_CHECK = "LinkedIn asked for a security check. Connect again and complete it in the window."


def levels_for_years(years: int) -> tuple[str, ...]:
    if years <= 1:
        return ("2",)
    if years == 2:
        return ("2", "3")
    if years == 3:
        return ("3", "4")
    return ("4",)


def experience_levels(prefs: SearchPrefs) -> list[tuple[str, int]]:
    """``[(f_E code, experience bucket), ...]`` in priority order, each level once."""
    levels: dict[str, int] = {}
    for bucket, years in enumerate(prefs.experience_years):
        for code in levels_for_years(years):
            levels.setdefault(code, bucket)
    return list(levels.items())


def keywords(roles: tuple[str, ...]) -> str:
    return " OR ".join(f'"{role}"' if " " in role else role for role in roles)


def keyword_chunks(roles: tuple[str, ...], max_terms: int = 4, max_chars: int = 100) -> list[tuple[str, ...]]:
    chunks: list[tuple[str, ...]] = []
    current: list[str] = []
    for role in roles:
        if current and (len(current) >= max_terms or len(keywords((*current, role))) > max_chars):
            chunks.append(tuple(current))
            current = []
        current.append(role)
    if current:
        chunks.append(tuple(current))
    return chunks


def search_params(roles: tuple[str, ...], location: str, levels: list[str]) -> list[tuple[str, str]]:
    if is_remote_location(location):
        params = [("keywords", keywords(roles)), ("location", S.REMOTE_SEARCH_LOCATION), S.REMOTE_PARAM]
    else:
        name = S.CITY_SEARCH_NAMES.get(normalize(location), f"{location}, India")
        params = [("keywords", keywords(roles)), ("location", name)]
    return [*params, S.SEARCH_TIME_WINDOW, S.SEARCH_SORT, ("f_E", ",".join(levels))]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def page_url(params: list[tuple[str, str]], start: int = 0) -> str:
    if start:
        params = [*params, ("start", str(start))]
    return f"{S.JOB_SEARCH_URL}?{urlencode(params, quote_via=quote)}"


# A run started from the app ("Run now") searches again once the last search is this old (2026-10-15:
# the candidate asked for a scan now; the hourly gap stays for scheduled runs).
MANUAL_MIN_INTERVAL_MINUTES = 15


def search_interval(ctx: ScanContext) -> int:
    """Minutes between LinkedIn searches: the interval set in the app (or LINKEDIN_SEARCH_INTERVAL_MINUTES),
    or 15 minutes for a run started from the app."""
    with Session(ctx.engine) as session:
        interval = get_minutes(session, LINKEDIN_SEARCH_INTERVAL, ctx.settings.linkedin_search_interval_minutes)
    return min(interval, MANUAL_MIN_INTERVAL_MINUTES) if ctx.manual else interval


def next_search_after(
    engine: Engine, now: datetime, interval_minutes: int, kind: ScanKind = ScanKind.linkedin_jobs
) -> datetime | None:
    """When the next search may start, or None when it may run now. Runs that searched (``ok`` or
    ``failed``) count; skipped ones don't."""
    with Session(engine) as session:
        last = session.exec(
            select(func.max(ScanRun.started_at)).where(
                ScanRun.kind == kind,
                col(ScanRun.status).in_([ScanStatus.ok, ScanStatus.failed]),
            )
        ).one()
    if last is None:
        return None
    due = to_utc(last) + timedelta(minutes=interval_minutes)
    if to_utc(now) >= due - INTERVAL_SLACK:
        return None
    return due


@dataclass
class _Query:
    location: str
    chunk: int  # which role group
    roles: tuple[str, ...]
    params: list[tuple[str, str]]
    next_start: int = 0
    total: int | None = None
    done: bool = False
    loaded: bool = False  # at least one page was read
    refetch: bool = False  # the rendered first page is fetched again through the job-data request
    easy_only: bool = False  # LinkedIn's "Easy Apply" filter (f_AL=true) is on

    @property
    def scope(self) -> str:
        """The location, and whether only Easy Apply jobs are searched: a captured job-data request is
        reused only within its own scope (its filters stay in it)."""
        return f"{self.location} (Easy Apply only)" if self.easy_only else self.location

    @property
    def has_more(self) -> bool:
        if self.done or not self.loaded:
            return False
        return self.total is None or self.next_start < self.total


@dataclass
class _Run:
    """One search run's budget, captured requests and what was read."""

    pacer: Pacer
    max_renders: int = MAX_RENDERS
    max_api_calls: int = MAX_API_CALLS
    renders: int = 0
    api_calls: int = 0
    templates: dict[str, str] = field(default_factory=dict)  # query scope -> captured list request
    broken: dict[str, str] = field(default_factory=dict)  # query scope -> why its request failed
    notes: list[str] = field(default_factory=list)
    # job id -> (merged job, the searches that found it, how it was first read: "api" / "page")
    jobs: dict[str, tuple[SearchJob, list[_Query], str]] = field(default_factory=dict)
    listed_without_details: int = 0
    unseen: list[str] = field(default_factory=list)  # scopes whose results page didn't show the request

    def source(self, query: _Query) -> str | None:
        """How the query's next page is read ("api" / "page"), or None when the budget is used up."""
        if query.scope in self.templates and query.scope not in self.broken:
            return "api" if self.api_calls < self.max_api_calls else None
        return "page" if self.renders < self.max_renders else None

    def add(self, page: SearchPage, query: _Query, source: str) -> int:
        """Keep the page's readable jobs (merged with earlier reads of the same job). Returns how
        many were new to this run."""
        new = 0
        for job in page.readable:
            known = self.jobs.get(job.job_id)
            if known is None:
                self.jobs[job.job_id] = (job, [query], source)
                new += 1
            else:
                known[0].merge(job)
                if query not in known[1]:
                    known[1].append(query)
        return new


class LinkedInJobsScanner:
    kind = ScanKind.linkedin_jobs
    platform = Platform.linkedin

    def run(self, ctx: ScanContext) -> ScanOutcome:
        due = next_search_after(ctx.engine, ctx.now, search_interval(ctx))
        if due is not None:
            return ScanOutcome(
                status=ScanStatus.skipped, message=f"Next LinkedIn search after {due.astimezone(LOCAL_TZ):%H:%M}."
            )
        ctx.connections.sync_from_chrome(self.platform)
        if not ctx.connections.login_usable(self.platform):
            return ScanOutcome(status=ScanStatus.skipped, message=NOT_CONNECTED)
        profile_dir = ctx.browsers.profile_dir(self.platform)

        def task(driver) -> ScanReport | None:
            browser = driver.open(profile_dir=profile_dir, headless=ctx.settings.scan_headless)
            try:
                # No li_at cookie means logged out (no page load needed to tell).
                if not LINKEDIN_LOGIN.has_auth_cookie(browser.cookies()):
                    return None
                return self.scan(browser, ctx)
            finally:
                browser.close()

        report = ctx.browsers.worker(self.platform.value).submit(task).result()
        if report is None:
            ctx.connections.record_login_lost(self.platform, LOGIN_EXPIRED)
            return ScanOutcome(status=ScanStatus.skipped, message=f"{LOGIN_EXPIRED} Jobs weren't searched.")
        return finish_scan(ctx, self.platform, report)

    def queries(self, prefs: SearchPrefs) -> list[_Query]:
        """One search per location and role group, most important first: first with LinkedIn's
        "Easy Apply" filter (the jobs the tool can apply to; 2026-10-15), then without it (company-site
        jobs for the dashboard)."""
        codes = [code for code, _ in experience_levels(prefs)]
        return [
            _Query(
                location, index, roles,
                search_params(roles, location, codes) + ([S.EASY_APPLY_PARAM] if easy_only else []),
                easy_only=easy_only,
            )
            for easy_only in (True, False)
            for location in prefs.locations
            for index, roles in enumerate(keyword_chunks(prefs.roles))
        ]

    def scan(self, browser: BrowserSession, ctx: ScanContext) -> ScanReport:
        report = ScanReport()
        run = _Run(Pacer(min(MAX_RENDERS + MAX_API_CALLS, ctx.settings.scan_max_page_loads), ctx.wait_range, ctx.stop))
        matcher = RoleMatcher(ctx.prefs.roles)
        queries = self.queries(ctx.prefs)
        out_of_budget = False
        try:
            # The first page of every search (per location: the first one rendered, which shows
            # LinkedIn's job-data request; the others through that request), then more pages in
            # priority order while the budget lasts.
            for query in queries:
                if ctx.stop.is_set():
                    break
                if not self._load(browser, query, ctx, run, report):
                    out_of_budget = out_of_budget or not ctx.stop.is_set()
            while not ctx.stop.is_set():
                pending = [q for q in queries if q.has_more]
                query = next((q for q in pending if run.source(q) is not None), None)
                if query is None:
                    out_of_budget = out_of_budget or bool(pending)
                    break
                if not self._load(browser, query, ctx, run, report):
                    out_of_budget = not ctx.stop.is_set()
                    break
        except BrowserUnavailable as error:
            report.failure = str(error)
        except ScanBlocked as error:
            # A login wall is self-explanatory; other blocks get a screenshot of what was on screen.
            note = "" if isinstance(error, LoginWall) else capture_failure(browser, self.kind.value)
            report.failure = str(error) + note
        except Exception as error:  # noqa: BLE001 - keep what was found so far
            logger.exception("LinkedIn job search stopped")
            text = str(error).strip()
            report.failure = (
                f"The search stopped: {text.splitlines()[0] if text else type(error).__name__}"
                + capture_failure(browser, self.kind.value)
            )
        # Job ids: undated, of unknown apply type, or left out by a job-fit rule (``LEFT_OUT`` keys).
        stats: dict[str, set[str]] = {"undated": set(), "unknown_type": set(), **{key: set() for key in LEFT_OUT}}
        for job, found_by, _ in run.jobs.values():
            # Each search that found it (Bengaluru / Remote match differently); the store keeps the
            # most important candidate.
            for query in found_by:
                candidate = self._candidate(job, query, ctx, matcher, stats, easy=any(q.easy_only for q in found_by))
                if candidate is not None:
                    report.candidates.append(candidate)
        if run.unseen:
            pages = "results page" if len(run.unseen) == 1 else "results pages"
            run.notes.insert(0, (
                f"LinkedIn's job-data request wasn't seen on the {_join(run.unseen)} {pages}; "
                "their pages are rendered instead." if len(run.unseen) > 1 else
                f"LinkedIn's job-data request wasn't seen on the {run.unseen[0]} {pages}; its pages are rendered instead."
            ))
        report.notes.extend(run.notes)
        if out_of_budget and any(q.has_more or not q.loaded for q in queries):
            report.notes.append(
                f"Stopped at this run's limit ({run.renders} rendered page(s), {run.api_calls} job-data request(s)); "
                "some of today's jobs may be missed."
            )
        if run.jobs:
            by_api = sum(1 for _, _, source in run.jobs.values() if source == "api")
            report.notes.append(
                f"Read {len(run.jobs)} jobs ({by_api} through LinkedIn's job-data request in {run.api_calls} call(s), "
                f"{len(run.jobs) - by_api} from {run.renders} rendered page(s)); "
                f"{len({c.external_id for c in report.candidates})} matched your roles, experience and locations."
            )
        if note := left_out_note({key: len(stats[key]) for key in LEFT_OUT}):
            report.notes.append(note)
        if run.listed_without_details:
            report.notes.append(
                f"{run.listed_without_details} listed job(s) came without details (title, company) and were skipped."
            )
        if stats["undated"]:
            report.notes.append(f"{len(stats['undated'])} matching job(s) had no date on the page (kept: the search covers 24 hours).")
        if stats["unknown_type"]:
            report.notes.append(
                f"{len(stats['unknown_type'])} matching job(s) didn't say whether they are Easy Apply; listed as company-site jobs."
            )
        report.page_loads = run.pacer.loads
        return report

    def _load(self, browser, query: _Query, ctx: ScanContext, run: _Run, report: ScanReport) -> bool:
        """Read the query's next page. False when the budget is used up (or the run is stopping)."""
        source = run.source(query)
        if source == "page" and query.loaded and query.next_start == 0:
            query.next_start = S.SEARCH_PAGE_SIZE  # its first page was rendered already
            return True
        if source is None or not run.pacer.next_load():
            return False
        start = query.next_start
        page = None
        if source == "api":
            run.api_calls += 1
            page, problem = self._fetch(browser, query, start, run)
            if page is None:
                run.broken[query.scope] = problem
                run.notes.append(
                    f"LinkedIn's job-data request didn't work for {query.scope} ({problem}); "
                    "its pages are rendered instead (slower)."
                )
                logger.info("LinkedIn job-data request for %s failed (%s); rendering instead", query.scope, problem)
                if query.loaded and start == 0:
                    query.next_start = S.SEARCH_PAGE_SIZE  # re-reading a rendered page: nothing to render
                    return True
                if run.renders >= run.max_renders or not run.pacer.next_load():
                    return False
                source = "page"
        if source == "page":
            run.renders += 1
            page = self._render(browser, query, start, ctx, run, report)
        assert page is not None
        readable = page.readable
        new = run.add(page, query, source)
        if source == "api":
            run.listed_without_details += len(page.missing)
        logger.info(
            "LinkedIn search %s, role group %d, start %d: %d jobs read (%d new), source %s, total %s%s",
            query.scope, query.chunk + 1, start, len(readable), new, source, page.total,
            f", {len(page.missing)} listed without details" if source == "api" and page.missing else "",
        )
        first_page = not query.loaded
        query.loaded = True
        if page.total is not None:
            query.total = page.total
        expected = min(S.SEARCH_PAGE_SIZE, page.total) if page.total is not None else S.SEARCH_PAGE_SIZE
        if (
            source == "page" and start == 0 and not query.refetch
            and query.scope in run.templates and query.scope not in run.broken
            and len(readable) < expected
        ):
            # The rendered page showed only part of its list: read it again through the request.
            query.refetch = True
            query.next_start = 0
            return True
        query.next_start = start + S.SEARCH_PAGE_SIZE
        if not readable:
            query.done = True
            if first_page and not page.no_results and source == "page":
                # Usually a changed page layout or a half-loaded page, not "no jobs today".
                raise ScanBlocked("LinkedIn's job search showed no readable jobs (the page layout may have changed)")
            return True
        if query.total is None and len(readable) < S.SEARCH_PAGE_SIZE:
            query.done = True
        return True

    def _render(self, browser, query: _Query, start: int, ctx: ScanContext, run: _Run, report: ScanReport) -> SearchPage:
        result = browser.goto(
            page_url(query.params, start), wait_for=S.SEARCH_READY, capture=S.SEARCH_CAPTURE, timeout=45
        )
        if LINKEDIN_LOGIN.is_security_check(result.url):
            report.login_lost = SECURITY_CHECK
            raise LoginWall("LinkedIn showed a security check; stopped.")
        if LINKEDIN_LOGIN.is_login_wall(result.url):
            report.login_lost = LOGIN_EXPIRED
            raise LoginWall("LinkedIn redirected to its login page; stopped.")
        reason = search_block_reason(result)
        if reason:
            raise ScanBlocked(reason + "; stopped.")
        report.login_ok = True
        template = search_api.pick_template(result.captured)
        if template and query.scope not in run.templates and query.scope not in run.broken:
            run.templates[query.scope] = template
            logger.info("LinkedIn job-data request for %s: %s", query.scope, template)
        elif not template and query.scope not in run.templates and query.scope not in run.broken:
            run.broken[query.scope] = "the page didn't show it"
            run.unseen.append(query.scope)
        if not browser.scroll_list(S.SEARCH_LIST_ITEM, S.SEARCH_SCROLL_STEPS):
            browser.wait(2.0)  # no card yet: give the list a moment, then try once more
            browser.scroll_list(S.SEARCH_LIST_ITEM, S.SEARCH_SCROLL_STEPS)
        return parse_search_page(browser.html(), [c.body for c in result.captured])

    def _fetch(self, browser, query: _Query, start: int, run: _Run) -> tuple[SearchPage | None, str]:
        """One page through the captured job-data request: ``(page, "")`` or ``(None, why not)``.
        Raises ``ScanBlocked`` on HTTP 429 / 999 (rendering instead would only hit the limit harder)."""
        token = csrf_token(browser)
        if not token:
            return None, "no JSESSIONID cookie for its csrf-token"
        try:
            url = search_api.rewrite(run.templates[query.scope], keywords(query.roles), start)
        except ValueError as error:
            return None, f"its URL couldn't be rewritten: {error}"
        try:
            response = browser.api_get(url, search_api.headers(token))
        except Exception as error:  # noqa: BLE001 - timeout, network
            return None, f"the call failed: {type(error).__name__}"
        if response.status in S.BLOCK_STATUSES and response.status != 403:
            raise ScanBlocked(f"LinkedIn returned HTTP {response.status} to the job-data request; stopped.")
        if response.status != 200:
            return None, f"HTTP {response.status}"
        if LINKEDIN_LOGIN.is_login_wall(response.url) or response.url.split("?")[0] != url.split("?")[0]:
            return None, "it was redirected"
        if response.json is None:
            return None, "the answer wasn't JSON"
        page = parse_search_data(response.json)
        if not page.readable and not page.no_results:
            return None, "no readable jobs in the answer"
        return page, ""

    def _candidate(
        self, job: SearchJob, query: _Query, ctx: ScanContext, matcher: RoleMatcher, stats, *, easy: bool = False
    ) -> Candidate | None:
        """``easy``: an "Easy Apply" search listed the job, so it is Easy Apply even when its card
        didn't say."""
        prefs = ctx.prefs
        posted = job.listed_at or estimate_posted_at(job.posted_label, ctx.now) or parse_date_attr(job.posted_attr)
        if posted is not None and not is_today(posted, ctx.now):
            return None
        match = match_title(prefs, matcher, job.title)
        if match is None:
            return None
        remote = is_remote_location(query.location)
        location_index = prefs.location_index(job.location, remote=remote)
        if location_index is None:
            return None
        if why := search_left_out(prefs, job.title, job.company):
            stats[why].add(job.job_id)
            return None
        location = job.location or query.location
        if remote and "remote" not in location.lower():
            location = f"{location} (Remote)"
        if posted is None:
            stats["undated"].add(job.job_id)
        if job.easy_apply is None and not easy:
            stats["unknown_type"].add(job.job_id)
        easy = bool(job.easy_apply) or (job.easy_apply is None and easy)
        description = " · ".join(part for part in ("Easy Apply" if easy else None, job.insight) if part)
        return Candidate(
            platform=self.platform,
            external_id=job.job_id,
            title=job.title,
            company=job.company or "Unknown company",
            url=job.url,
            apply_type=ApplyType.easy_apply if easy else ApplyType.company_site,
            priority=priority(match.bucket, match.role_index, location_index),
            matched_role=match.role,
            location=location,
            short_description=description,
            posted_at=posted,
            experience=experience_phrase(job.title),
        )
