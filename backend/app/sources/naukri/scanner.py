"""``naukri_jobs``: Naukri job search for jobs posted today.

- Searches (2026-10-21): one per role in ``search_roles``, in priority order, in each location of
  ``search_locations`` ("Remote" uses Naukri's remote filter), newest first. Naukri's experience
  filter (one round of searches per level of ``search_experience_order``), salary filter (the bands
  holding ``search_salary_lpa``) and freshness filter (1 day) are sent once ``selectors.py`` names
  them: they need a live check. Each search reads pages until a job from before today, a page of
  already-stored jobs, or its share of the page budget.
- Recommended jobs (2026-10-21): with the saved login, Naukri's logged-in "Recommended Jobs" list is
  read too (one page), with the same rules. When nothing on it can be read, the page is saved to
  ``data/diagnostics/``; it never fails the run.
- The search always runs in a guest browser: its results already say which jobs apply on the
  company site (``companyApplyJob``), and on 2026-10-13 the logged-in search page returned nothing
  the parser could read. The saved login is used for the recommended list and when the results
  don't say the apply type (HTML fallback): then the job pages are opened logged in to read their
  apply button ("Apply on company site" is only shown to logged-in users). When Naukri isn't
  connected the run message says so.
- While the ``pause_naukri`` setting is on (2026-10-14), the run is skipped without opening a browser.
- Job fit (``app.scanner.fit``, 2026-10-15): a job that matches but whose company is on the skip list
  or whose title is for another stack or too senior isn't stored; the run message counts them.
- The run message starts with "Search: N jobs read; Recommended: M; K matched." (2026-10-21).
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import quote, urlencode

from sqlmodel import Session, col, select

from app.browser import GUEST, BrowserSession, BrowserUnavailable
from app.models import ApplyType, Job, Platform, ProfileField, ScanKind, ScanStatus
from app.scanner.base import (
    Candidate,
    Pacer,
    ScanBlocked,
    ScanContext,
    ScanOutcome,
    ScanReport,
    capture_failure,
    truncate,
)
from app.scanner.fit import LEFT_OUT, SEARCH_SALARY_KEY, left_out_note, search_left_out, target_salary_lpa
from app.scanner.matching import (
    RoleMatcher,
    SearchPrefs,
    estimate_posted_at,
    is_remote_location,
    is_today,
    match_title,
    priority,
)
from app.scanner.store import finish_scan
from app.services.app_settings import PAUSE_NAUKRI, get_flag
from app.sources.naukri import selectors as S
from app.sources.naukri.login import NAUKRI_LOGIN
from app.sources.naukri.parse import (
    NaukriJob,
    block_reason,
    parse_job_page_apply,
    parse_recommended,
    parse_search,
)

logger = logging.getLogger(__name__)

NOT_CONNECTED_NOTE = (
    "Naukri isn't connected, so the search ran without a login; apply types come from the "
    "search results."
)
LOGIN_EXPIRED = "The saved Naukri login has expired. Connect again."
RECOMMENDED_KIND = "naukri_recommended"  # diagnostics file name


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# ---------------------------------------------------------------------------
# Search URLs


@dataclass(frozen=True)
class Search:
    role: str
    location: str
    experience: int | None = None  # minimum years, for Naukri's experience filter


def search_plan(prefs: SearchPrefs) -> list[Search]:
    """The searches, most important first: per experience level (only when Naukri's experience
    filter is on; otherwise one round), per role, per location."""
    levels = tuple(dict.fromkeys(prefs.experience_years)) if S.EXPERIENCE_PARAM else (None,)
    plan: list[Search] = []
    for years in levels:
        for role in prefs.roles:
            for location in prefs.locations:
                search = Search(role, location, years)
                if search not in plan:
                    plan.append(search)
    return plan


def salary_range(value: str | None) -> tuple[float, float] | None:
    """``search_salary_lpa`` as (low, high) lakhs: "16-18 LPA" -> (16, 18), "16 LPA" -> (16, 16)."""
    low = target_salary_lpa(value)
    if low is None:
        return None
    parts = re.split(r"\s*(?:-|–|\bto\b)\s*", value or "", maxsplit=1)
    high = target_salary_lpa(parts[1]) if len(parts) > 1 else None
    return low, high if high is not None and high >= low else low


def salary_bands(target: tuple[float, float]) -> list[str]:
    """Naukri's salary bands that hold any of ``target`` (low, high lakhs): (16, 18) -> ["15to25"]."""
    low, high = target
    return [f"{lo}to{hi}" for lo, hi in S.SALARY_BANDS if lo <= high and hi > low]


def search_url(
    role: str,
    location: str,
    page: int = 1,
    *,
    experience: int | None = None,
    salary: tuple[float, float] | None = None,
) -> str:
    """Search URL for one role in one location ("Remote" uses the remote filter), newest first, with
    the experience / salary / freshness filters that ``selectors.py`` names."""
    path = f"{_slug(role)}-jobs"
    params: list[tuple[str, str]] = [("k", role.lower())]
    if is_remote_location(location):
        params.append(S.REMOTE_PARAM)
    else:
        path += f"-in-{_slug(location)}"
        params.append(("l", location.lower()))
    if S.EXPERIENCE_PARAM and experience is not None:
        params.append((S.EXPERIENCE_PARAM, str(experience)))
    if S.SALARY_PARAM and salary is not None:
        params += [(S.SALARY_PARAM, band) for band in salary_bands(salary)]
    if S.FRESHNESS_PARAM:
        params.append(S.FRESHNESS_PARAM)
    params.append(S.SORT_BY_DATE)
    if page > 1:
        path += f"-{page}"
    return f"{S.BASE_URL}/{path}?{urlencode(params, quote_via=quote)}"


def search_salary(ctx: ScanContext) -> tuple[float, float] | None:
    with Session(ctx.engine) as session:
        row = session.exec(select(ProfileField).where(ProfileField.key == SEARCH_SALARY_KEY)).first()
    return salary_range(row.value if row else None)


# ---------------------------------------------------------------------------
# Scanner


@dataclass
class NaukriReport(ScanReport):
    search_read: set[str] = field(default_factory=set)  # ids of the jobs the searches read
    recommended_read: int | None = None  # jobs read from the recommended list; None: not opened
    left_out: dict[str, set[str]] = field(default_factory=lambda: {key: set() for key in LEFT_OUT})
    # Today's jobs the role / experience / location rules left out (2026-10-21: 140 read, 1 matched):
    # reason -> job id -> title, logged with examples so the filters can be tuned.
    not_matched: dict[str, dict[str, str]] = field(
        default_factory=lambda: {"role": {}, "experience": {}, "location": {}}
    )

    def summary(self) -> list[str]:
        """"Search: N jobs read; Recommended: M; K matched." and the job-fit counts."""
        parts = [f"Search: {len(self.search_read)} jobs read"]
        if self.recommended_read is not None:
            parts.append(f"Recommended: {self.recommended_read}")
        parts.append(f"{len(self.candidates)} matched")
        notes = ["; ".join(parts) + "."]
        if note := left_out_note({key: len(ids) for key, ids in self.left_out.items()}):
            notes.append(note)
        labels = {"role": "not one of your roles", "experience": "more experience than yours", "location": "outside Bengaluru"}
        counts = [f"{len(jobs)} {labels[why]}" for why, jobs in self.not_matched.items() if jobs]
        if counts:
            notes.append("Today's jobs not matched: " + ", ".join(counts) + ".")
        for why, jobs in self.not_matched.items():
            if jobs:
                logger.info("Naukri jobs not matched (%s): %s", why, " | ".join(list(jobs.values())[:12]))
        return notes


class NaukriJobsScanner:
    kind = ScanKind.naukri_jobs
    platform = Platform.naukri

    def run(self, ctx: ScanContext) -> ScanOutcome:
        with Session(ctx.engine) as session:
            if get_flag(session, PAUSE_NAUKRI):
                return ScanOutcome(status=ScanStatus.skipped, message="Naukri is paused.")
        ctx.connections.sync_from_chrome(self.platform)
        use_login = ctx.connections.login_usable(self.platform)
        known = recent_ids(ctx, self.platform)
        pacer = ctx.pacer()

        def search(driver) -> NaukriReport:
            browser = driver.open(profile_dir=None, headless=ctx.settings.scan_headless)
            try:
                # One page load is kept for the recommended list.
                return self.scan(browser, ctx, known=known, pacer=pacer, reserve=1 if use_login else 0)
            finally:
                browser.close()

        report = ctx.browsers.worker(GUEST).submit(search).result()
        if use_login and not report.failure:
            profile_dir = ctx.browsers.profile_dir(self.platform)

            def logged_in(driver) -> None:
                browser = driver.open(profile_dir=profile_dir, headless=ctx.settings.scan_headless)
                try:
                    if self.read_recommended(browser, ctx, pacer, report) and report.unconfirmed:
                        self.confirm_apply_types(browser, ctx, pacer, report)
                finally:
                    browser.close()

            try:
                ctx.browsers.worker(self.platform.value).submit(logged_in).result()
            except Exception as error:  # noqa: BLE001 - the search results still count
                logger.warning("Naukri logged-in browser failed", exc_info=True)
                report.notes.append(f"The logged-in Naukri pages weren't read: {_first_line(error)}")
        if report.unconfirmed:
            report.notes.append(
                f"{len(report.unconfirmed)} job(s) are listed as company-site jobs without confirming the apply button."
            )
        if not use_login:
            report.notes.append(NOT_CONNECTED_NOTE)
        report.notes[:0] = report.summary()
        report.page_loads = pacer.loads
        return finish_scan(ctx, self.platform, report)

    def scan(
        self,
        browser: BrowserSession,
        ctx: ScanContext,
        *,
        known: set[str],
        pacer: Pacer | None = None,
        reserve: int = 0,
    ) -> NaukriReport:
        """Run every search of ``search_plan`` (guest browser), leaving ``reserve`` page loads of the
        budget unused. Jobs whose apply type the results don't say end up in ``report.unconfirmed``."""
        report = NaukriReport()
        pacer = pacer or ctx.pacer()
        prefs = ctx.prefs
        matcher = RoleMatcher(prefs.roles)
        plan = search_plan(prefs)
        salary = search_salary(ctx) if S.SALARY_PARAM else None
        cut_short = False
        try:
            for position, search in enumerate(plan):
                remote = is_remote_location(search.location)
                # Split what's left of the budget over the searches still to run.
                left = max(pacer.max_loads - reserve - pacer.loads, 0)
                allowance = -(-left // (len(plan) - position))
                more = True
                for page_no in range(1, allowance + 1):
                    if not pacer.next_load():
                        break
                    url = search_url(search.role, search.location, page_no, experience=search.experience, salary=salary)
                    try:
                        result = browser.goto(url, capture=S.SEARCH_API, wait_for=S.CARD_LINK, timeout=60)
                    except Exception as error:
                        if "Timeout" in type(error).__name__:
                            raise ScanBlocked("Naukri didn't answer within 60 seconds; stopped.") from error
                        raise
                    page = parse_search(result)
                    if not page.jobs:
                        reason = block_reason(result)
                        logger.info("Naukri %s in %s page %d: no jobs read (%s)", search.role, search.location,
                                    page_no, reason or result.url)
                        if reason:
                            raise ScanBlocked(reason + "; stopped.")
                        more = False
                        break
                    report.search_read.update(job.job_id for job in page.jobs)
                    today, matched, reached_older = self.collect(
                        page.jobs, ctx, matcher, report, search.location, remote
                    )
                    logger.info(
                        "Naukri %s in %s page %d: %d jobs (%s), %d from today, %d matched",
                        search.role, search.location, page_no, len(page.jobs), "API" if page.from_api else "HTML",
                        today, matched,
                    )
                    if (
                        reached_older
                        or len(page.jobs) < S.RESULTS_PER_PAGE
                        or all(job.job_id in known for job in page.jobs)
                    ):
                        more = False
                        break
                cut_short = cut_short or more
            if cut_short and not ctx.stop.is_set():
                report.notes.append(
                    f"Stopped at the {pacer.max_loads}-page limit; some of today's jobs may be missed."
                )
        except (ScanBlocked, BrowserUnavailable) as error:
            report.failure = str(error)
        except Exception as error:  # noqa: BLE001 - keep what was found so far
            logger.exception("Naukri scan stopped")
            report.failure = f"The scan stopped: {_first_line(error)}"
        report.page_loads = pacer.loads
        return report

    def collect(
        self,
        jobs: list[NaukriJob],
        ctx: ScanContext,
        matcher: RoleMatcher,
        report: NaukriReport,
        searched_location: str,
        remote: bool,
    ) -> tuple[int, int, bool]:
        """Add today's matching jobs to ``report`` (each job once). Returns (jobs from today, new
        matches, whether a job from before today was seen)."""
        today = matched = 0
        reached_older = False
        found = {candidate.external_id for candidate in report.candidates}
        for job in jobs:
            posted = job.created_at or estimate_posted_at(job.posted_label, ctx.now)
            if posted is None and job.todays_job:
                posted = ctx.now
            if not is_today(posted, ctx.now):
                reached_older = True
                continue
            today += 1
            if job.job_id in found:  # an earlier search (a more important one) found it
                continue
            candidate = self.candidate(
                job, ctx, matcher, searched_location, remote, posted, left_out=report.left_out, report=report
            )
            if candidate is None:
                continue
            matched += 1
            found.add(job.job_id)
            report.candidates.append(candidate)
            if job.company_apply is None:
                report.unconfirmed.append(candidate)
        return today, matched, reached_older

    def read_recommended(self, browser: BrowserSession, ctx: ScanContext, pacer: Pacer, report: NaukriReport) -> bool:
        """With the saved login: read Naukri's "Recommended Jobs" list into ``report``. Never raises;
        returns False when the logged-in pages should stop (a login wall or a block page)."""
        if not pacer.next_load():
            return True
        try:
            result = browser.goto(S.RECOMMENDED_URL, capture=S.RECOMMENDED_API, wait_for=S.CARD_LINK, timeout=45)
        except Exception as error:  # noqa: BLE001 - the search results still count
            logger.warning("Naukri recommended jobs didn't load", exc_info=True)
            report.notes.append(f"The recommended jobs didn't load: {_first_line(error)}")
            return True
        if NAUKRI_LOGIN.is_login_wall(result.url):
            report.login_lost = LOGIN_EXPIRED
            return False
        try:
            page = parse_recommended(result)
        except Exception:  # noqa: BLE001 - unexpected markup: treated as nothing read
            logger.warning("Naukri recommended jobs couldn't be parsed", exc_info=True)
            page = None
        jobs = page.jobs if page is not None else []
        report.recommended_read = len(jobs)
        if not jobs:
            if reason := block_reason(result):
                report.notes.append(f"Recommended jobs: {reason}; stopped.")
                return False
            note = capture_failure(browser, RECOMMENDED_KIND)
            report.notes.append("No recommended jobs could be read." + note)
            return True
        report.login_ok = True
        today, matched, _ = self.collect(jobs, ctx, RoleMatcher(ctx.prefs.roles), report, "", False)
        logger.info("Naukri recommended jobs: %d jobs, %d from today, %d matched", len(jobs), today, matched)
        return True

    def candidate(
        self,
        job: NaukriJob,
        ctx: ScanContext,
        matcher: RoleMatcher,
        searched_location: str,
        remote: bool,
        posted,
        *,
        left_out: dict[str, set[str]] | None = None,
        report: "NaukriReport | None" = None,
    ) -> Candidate | None:
        """The job to store, or None. ``left_out`` collects the ids of matching jobs a job-fit rule
        leaves out, per ``LEFT_OUT`` key."""
        prefs = ctx.prefs
        match = match_title(prefs, matcher, job.title, min_experience=job.min_experience)
        if match is None:
            if report is not None:
                why = "experience" if match_title(prefs, matcher, job.title, min_experience=0) else "role"
                report.not_matched[why][job.job_id] = f"{job.title} ({job.experience or '?'})"
            return None
        location_index = prefs.location_index(job.location, remote=remote)
        if location_index is None:
            if report is not None:
                report.not_matched["location"][job.job_id] = f"{job.title} ({job.location or '?'})"
            return None
        if why := search_left_out(prefs, job.title, job.company):
            if left_out is not None:
                left_out[why].add(job.job_id)
            return None
        location = job.location or searched_location
        if remote and "remote" not in location.lower():
            location = f"{location} (Remote)"
        if job.company_apply:
            apply_type = ApplyType.company_site
        elif job.company_apply is False:
            apply_type = ApplyType.one_click
        else:
            apply_type = ApplyType.company_site  # unconfirmed; see confirm_apply_types
        skills = ", ".join(s.strip() for s in job.skills.split(",")[:6] if s.strip())
        description = " · ".join(part for part in (skills, job.description) if part)
        return Candidate(
            platform=self.platform,
            external_id=job.job_id,
            title=job.title,
            company=job.company or "Unknown company",
            url=job.url,
            apply_type=apply_type,
            apply_url=job.apply_redirect_url if apply_type == ApplyType.company_site else None,
            priority=priority(match.bucket, match.role_index, location_index),
            matched_role=match.role,
            location=location,
            short_description=truncate(description),
            posted_at=posted,
            experience=job.experience,
        )

    def confirm_apply_types(self, browser: BrowserSession, ctx: ScanContext, pacer: Pacer, report: ScanReport) -> None:
        """With the saved login: open job pages whose apply type the search didn't say and read
        the apply button. Confirmed jobs leave ``report.unconfirmed``."""
        for candidate in sorted(report.unconfirmed, key=lambda c: c.priority):
            if not pacer.next_load():
                return
            try:
                result = browser.goto(candidate.url, wait_for=", ".join(S.APPLY_BUTTON), timeout=30)
            except Exception:  # noqa: BLE001 - leave it unconfirmed
                logger.warning("Naukri job page %s didn't load", candidate.external_id, exc_info=True)
                return
            if NAUKRI_LOGIN.is_login_wall(result.url):
                report.login_lost = LOGIN_EXPIRED
                return
            apply_type = parse_job_page_apply(result.html)
            if apply_type is not None:
                report.login_ok = True
                candidate.apply_type = apply_type
                candidate.apply_url = candidate.apply_url if apply_type == ApplyType.company_site else None
                report.unconfirmed.remove(candidate)


def recent_ids(ctx: ScanContext, platform: Platform) -> set[str]:
    """External ids of jobs found in the last two days (enough to stop paging at known jobs)."""
    since = ctx.now - timedelta(days=2)
    with Session(ctx.engine) as session:
        return set(
            session.exec(
                select(Job.external_id).where(Job.platform == platform, col(Job.found_at) >= since)
            ).all()
        )


def _first_line(error: Exception) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__
