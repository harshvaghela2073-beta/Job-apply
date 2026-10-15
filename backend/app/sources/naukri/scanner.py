"""``naukri_jobs``: Naukri job search for jobs posted today.

- One search per location in ``search_locations``, with every role in ``search_roles`` as a
  comma-separated keyword list, newest first. "Remote" uses Naukri's remote filter. Pages are read
  until a job from before today, a page of already-stored jobs, or the page budget.
- The search always runs in a guest browser: its results already say which jobs apply on the
  company site (``companyApplyJob``), and on 2026-10-13 the logged-in search page returned nothing
  the parser could read. The saved login is used only when the results don't say (HTML fallback):
  then the job pages are opened logged in to read their apply button ("Apply on company site" is
  only shown to logged-in users). When Naukri isn't connected the run message says so.
- While the ``pause_naukri`` setting is on (2026-10-14), the run is skipped without opening a browser.
"""

import logging
import re
from datetime import timedelta
from urllib.parse import quote, urlencode

from sqlmodel import Session, col, select

from app.browser import GUEST, BrowserSession, BrowserUnavailable
from app.models import ApplyType, Job, Platform, ScanKind, ScanStatus
from app.scanner.base import Candidate, Pacer, ScanBlocked, ScanContext, ScanOutcome, ScanReport, truncate
from app.scanner.matching import (
    RoleMatcher,
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
from app.sources.naukri.parse import NaukriJob, block_reason, parse_job_page_apply, parse_search

logger = logging.getLogger(__name__)

NOT_CONNECTED_NOTE = (
    "Naukri isn't connected, so the search ran without a login; apply types come from the "
    "search results."
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def search_url(roles: tuple[str, ...], location: str, page: int = 1) -> str:
    """Search URL for all ``roles`` in one location ("Remote" uses the remote filter), newest first."""
    path = f"{_slug(' '.join(roles))}-jobs"
    params: list[tuple[str, str]] = [("k", ", ".join(role.lower() for role in roles))]
    if is_remote_location(location):
        params.append(S.REMOTE_PARAM)
    else:
        path += f"-in-{_slug(location)}"
        params.append(("l", location.lower()))
    params.append(S.SORT_BY_DATE)
    if page > 1:
        path += f"-{page}"
    return f"{S.BASE_URL}/{path}?{urlencode(params, quote_via=quote)}"


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

        def search(driver) -> ScanReport:
            browser = driver.open(profile_dir=None, headless=ctx.settings.scan_headless)
            try:
                return self.scan(browser, ctx, known=known, pacer=pacer)
            finally:
                browser.close()

        report = ctx.browsers.worker(GUEST).submit(search).result()
        if report.unconfirmed and use_login and not report.failure:
            profile_dir = ctx.browsers.profile_dir(self.platform)

            def confirm(driver) -> None:
                browser = driver.open(profile_dir=profile_dir, headless=ctx.settings.scan_headless)
                try:
                    self.confirm_apply_types(browser, ctx, pacer, report)
                finally:
                    browser.close()

            ctx.browsers.worker(self.platform.value).submit(confirm).result()
        if report.unconfirmed:
            report.notes.append(
                f"{len(report.unconfirmed)} job(s) are listed as company-site jobs without confirming the apply button."
            )
        if not use_login:
            report.notes.append(NOT_CONNECTED_NOTE)
        report.page_loads = pacer.loads
        return finish_scan(ctx, self.platform, report)

    def scan(
        self, browser: BrowserSession, ctx: ScanContext, *, known: set[str], pacer: Pacer | None = None
    ) -> ScanReport:
        """Search every location (guest browser). Jobs whose apply type the results don't say end
        up in ``report.unconfirmed``."""
        report = ScanReport()
        pacer = pacer or ctx.pacer()
        prefs = ctx.prefs
        matcher = RoleMatcher(prefs.roles)
        cut_short = False
        try:
            for position, location in enumerate(prefs.locations):
                remote = is_remote_location(location)
                # Split what's left of the budget over the locations still to search.
                allowance = -(-(pacer.max_loads - pacer.loads) // (len(prefs.locations) - position))
                more = True
                for page_no in range(1, allowance + 1):
                    if not pacer.next_load():
                        break
                    try:
                        result = browser.goto(
                            search_url(prefs.roles, location, page_no),
                            capture=S.SEARCH_API,
                            wait_for=S.CARD_LINK,
                            timeout=60,
                        )
                    except Exception as error:
                        if "Timeout" in type(error).__name__:
                            raise ScanBlocked("Naukri didn't answer within 60 seconds; stopped.") from error
                        raise
                    page = parse_search(result)
                    today = matched = 0
                    if not page.jobs:
                        reason = block_reason(result)
                        logger.info("Naukri %s page %d: no jobs read (%s)", location, page_no, reason or result.url)
                        if reason:
                            raise ScanBlocked(reason + "; stopped.")
                        more = False
                        break
                    reached_older = False
                    for job in page.jobs:
                        posted = job.created_at or estimate_posted_at(job.posted_label, ctx.now)
                        if posted is None and job.todays_job:
                            posted = ctx.now
                        if not is_today(posted, ctx.now):
                            reached_older = True
                            continue
                        today += 1
                        candidate = self.candidate(job, ctx, matcher, location, remote, posted)
                        if candidate is None:
                            continue
                        matched += 1
                        report.candidates.append(candidate)
                        if job.company_apply is None:
                            report.unconfirmed.append(candidate)
                    logger.info(
                        "Naukri %s page %d: %d jobs (%s), %d from today, %d matched",
                        location, page_no, len(page.jobs), "API" if page.from_api else "HTML", today, matched,
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

    def candidate(
        self,
        job: NaukriJob,
        ctx: ScanContext,
        matcher: RoleMatcher,
        searched_location: str,
        remote: bool,
        posted,
    ) -> Candidate | None:
        prefs = ctx.prefs
        match = match_title(prefs, matcher, job.title, min_experience=job.min_experience)
        if match is None:
            return None
        location_index = prefs.location_index(job.location, remote=remote)
        if location_index is None:
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
                report.login_lost = "The saved Naukri login has expired. Connect again."
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
