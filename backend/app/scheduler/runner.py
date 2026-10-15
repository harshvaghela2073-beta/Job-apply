"""Running the scanners, each run recorded as a ``ScanRun`` row (docs/API_CONTRACT.md, "Scans" and
"Separate LinkedIn and Naukri scans"). The scanners form two groups, LinkedIn and Naukri
(``GROUPS``): within a group one run at a time, its scanners one after another; the two groups run
independently, each in its own thread (each platform has its own browser worker). The scanners only
collect jobs; the appliers (``naukri_apply``, ``linkedin_apply``) and the senders (``email_send``,
``linkedin_outreach``) act only while their switch is on. A scanner whose pause switch is on
(``PAUSES``) sits out the run without opening a browser."""

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func
from sqlmodel import Session, col, select

from app.browser import BrowserPool
from app.config import Settings
from app.models import Platform, ScanKind, ScanRun, ScanStatus
from app.scanner.base import ScanContext, ScanOutcome, Scanner
from app.scanner.matching import PrefsMissing, load_prefs
from app.services.app_settings import (
    PAUSE_LINKEDIN,
    PAUSE_LINKEDIN_JOBS,
    PAUSE_LINKEDIN_POSTS,
    PAUSE_NAUKRI,
    PAUSE_NAUKRI_JOBS,
    get_flag,
    run_interval,
)
from app.services.connections import ConnectionService
from app.timeutils import to_utc, utcnow

logger = logging.getLogger(__name__)


def default_scanners() -> list[Scanner]:
    from app.scanner.email_send import EmailSender
    from app.sources.linkedin.easy_apply import LinkedInApplier
    from app.sources.linkedin.jobs import LinkedInJobsScanner
    from app.sources.linkedin.outreach import LinkedInOutreach
    from app.sources.linkedin.posts import LinkedInPostsScanner
    from app.sources.naukri.apply import NaukriApplier
    from app.sources.naukri.scanner import NaukriJobsScanner

    # LinkedIn first in every run (2026-10-14); the same order as ScanKind.
    return [
        LinkedInJobsScanner(),
        LinkedInApplier(),
        LinkedInPostsScanner(),
        EmailSender(),
        LinkedInOutreach(),
        NaukriJobsScanner(),
        NaukriApplier(),
    ]


# The scan groups (2026-10-15), each run on its own schedule; the email sender belongs to LinkedIn
# (its pause switch is ``pause_linkedin``). Within a group the scanners run in ScanKind order.
GROUPS: dict[Platform, tuple[ScanKind, ...]] = {
    Platform.linkedin: (
        ScanKind.linkedin_jobs,
        ScanKind.linkedin_apply,
        ScanKind.linkedin_posts,
        ScanKind.email_send,
        ScanKind.linkedin_outreach,
    ),
    Platform.naukri: (ScanKind.naukri_jobs, ScanKind.naukri_apply),
}
GROUP_OF: dict[ScanKind, Platform] = {kind: platform for platform, kinds in GROUPS.items() for kind in kinds}


def group_of(kind: ScanKind | str) -> Platform:
    """The scan group (platform) a scanner belongs to."""
    return GROUP_OF[ScanKind(kind)]


# Failures caused by this computer (offline, asleep, Chrome not starting) don't count for backoff.
LOCAL_PROBLEM_MARKERS = (
    "err_internet_disconnected",
    "err_name_not_resolved",
    "err_network_changed",
    "err_network_io_suspended",
    "err_address_unreachable",
    "err_connection_reset",
    "browser couldn't start",
    "browsertype.launch",
    "couldn't reach the mail server",  # email_send: offline, or the mail server unreachable
    "interrupted: the backend stopped",  # a restart of this app, not the site
)


def is_local_problem(message: str | None) -> bool:
    text = (message or "").lower()
    return any(marker in text for marker in LOCAL_PROBLEM_MARKERS)


RATE_LIMIT_WAIT_MINUTES = 120
BACKOFF_CAP_MINUTES = 240

LINKEDIN_PAUSED = (PAUSE_LINKEDIN, "LinkedIn is paused.")
NAUKRI_PAUSED = (PAUSE_NAUKRI, "Naukri is paused.")
# The pause switches of each scanner with the message a paused run records, the platform switch first
# (docs/API_CONTRACT.md, "Pause switches"). The Naukri scanners also check ``pause_naukri`` themselves.
PAUSES: dict[ScanKind, tuple[tuple[str, str], ...]] = {
    ScanKind.linkedin_jobs: (LINKEDIN_PAUSED, (PAUSE_LINKEDIN_JOBS, "The LinkedIn job search is paused.")),
    ScanKind.linkedin_apply: (LINKEDIN_PAUSED,),
    ScanKind.linkedin_posts: (LINKEDIN_PAUSED, (PAUSE_LINKEDIN_POSTS, "The hiring-post search is paused.")),
    ScanKind.email_send: (LINKEDIN_PAUSED,),
    ScanKind.linkedin_outreach: (LINKEDIN_PAUSED,),
    ScanKind.naukri_jobs: (NAUKRI_PAUSED, (PAUSE_NAUKRI_JOBS, "The Naukri search is paused.")),
    ScanKind.naukri_apply: (NAUKRI_PAUSED,),
}


def pause_reason(session: Session, kind: ScanKind) -> str | None:
    """Why this scanner is paused by the user, or None."""
    for flag, message in PAUSES.get(kind, ()):
        if get_flag(session, flag):
            return message
    return None


class ScansRunning(RuntimeError):
    pass


class ScanService:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        browsers: BrowserPool,
        connections: ConnectionService,
        scanners: list[Scanner] | None = None,
    ):
        self.engine = engine
        self.settings = settings
        self.browsers = browsers
        self.connections = connections
        self.scanners = scanners if scanners is not None else default_scanners()
        # Random pause between page loads, in seconds (tests set 0).
        self.wait_range: tuple[float, float] = (2.0, 6.0)
        # Called with the group and its finish time after every group run, before the group counts
        # as idle again (the scheduler plans that group's next run from it).
        self.on_finished: Callable[[Platform, datetime], None] | None = None
        # Guards ``_running`` and ``_idle``: the request threads, the scheduler and both group
        # threads start and finish runs.
        self._lock = threading.Lock()
        self._running: set[Platform] = set()
        self._idle = threading.Event()  # set while no group is running
        self._idle.set()
        self._stop = threading.Event()
        self._held = 0  # > 0 while ``hold`` keeps new runs from starting

    @property
    def running(self) -> bool:
        """Any group is running."""
        with self._lock:
            return bool(self._running)

    def running_for(self, platform: Platform | str) -> bool:
        with self._lock:
            return Platform(platform) in self._running

    @contextmanager
    def hold(self) -> Iterator[None]:
        """No run starts inside this block (e.g. while the database is replaced). Raises
        ``ScansRunning`` when a group is running already."""
        with self._lock:
            if self._running:
                raise ScansRunning("A scan is running. Try again when it has finished.")
            self._held += 1
        try:
            yield
        finally:
            with self._lock:
                self._held -= 1

    def scanners_of(self, platform: Platform) -> list[Scanner]:
        """This group's scanners, in the order they were given (ScanKind order by default)."""
        return [scanner for scanner in self.scanners if group_of(scanner.kind) == platform]

    def startup(self) -> None:
        """Runs still marked ``running`` were cut off by a restart."""
        with Session(self.engine) as session:
            stale = session.exec(select(ScanRun).where(ScanRun.status == ScanStatus.running)).all()
            for run in stale:
                run.status = ScanStatus.failed
                run.finished_at = utcnow()
                run.message = "Interrupted: the backend stopped during this run."
                session.add(run)
            session.commit()

    def start(self, platform: Platform | str | None = None, *, manual: bool = False) -> list[Platform]:
        """Start a run of this group (None: every group that isn't running) in the background.
        Returns the groups started, empty when they are all already running. ``manual``: started
        from the app, so hourly scanners may run sooner (``ScanContext.manual``)."""
        wanted = list(Platform) if platform is None else [Platform(platform)]
        with self._lock:
            if self._stop.is_set() or self._held:
                return []
            started = [group for group in wanted if group not in self._running]
            self._running.update(started)
            if started:
                self._idle.clear()
        for group in started:
            threading.Thread(
                target=self._run_group, args=(group, manual), name=f"scan-{group.value}", daemon=True
            ).start()
        return started

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until no group is running. Returns False on timeout."""
        return self._idle.wait(timeout)

    def _run_group(self, platform: Platform, manual: bool) -> None:
        finished = None
        try:
            for scanner in self.scanners_of(platform):
                if self._stop.is_set():
                    break
                finished = self.run_one(scanner, manual=manual).finished_at
        except Exception:  # run_one records scanner errors; this is the database or a bug
            logger.exception("The %s scan run failed", platform)
        finally:
            finished = finished or utcnow()
            if self.on_finished is not None:
                try:
                    self.on_finished(platform, finished)
                except Exception:
                    logger.exception("on_finished failed")
            with self._lock:
                self._running.discard(platform)
                if not self._running:
                    self._idle.set()

    def run_one(self, scanner: Scanner, *, manual: bool = False) -> ScanRun:
        kind = ScanKind(scanner.kind)
        last_ok = self._last_ok_started_at(kind)
        backoff = self._backoff(kind)
        prefs = None
        with Session(self.engine) as session:
            run = ScanRun(kind=kind, status=ScanStatus.running)
            session.add(run)
            session.commit()
            session.refresh(run)
            run_id = run.id
            # The user's pause switch comes first: a paused scanner opens no browser.
            paused = pause_reason(session, kind)
            if paused:
                outcome = ScanOutcome(status=ScanStatus.skipped, message=paused)
            else:
                try:
                    prefs = load_prefs(session)
                except PrefsMissing as error:
                    outcome = ScanOutcome(status=ScanStatus.failed, message=str(error))
        if prefs is not None and backoff:
            outcome = ScanOutcome(status=ScanStatus.skipped, message=backoff)
        elif prefs is not None:
            ctx = ScanContext(
                engine=self.engine,
                settings=self.settings,
                prefs=prefs,
                now=run.started_at,
                browsers=self.browsers,
                connections=self.connections,
                stop=self._stop,
                last_ok_started_at=last_ok,
                wait_range=self.wait_range,
                manual=manual,
            )
            try:
                outcome = scanner.run(ctx)
            except Exception as error:  # noqa: BLE001 - recorded on the run
                logger.exception("Scanner %s failed", kind)
                outcome = ScanOutcome(status=ScanStatus.failed, message=f"The scan failed: {error}")
        with Session(self.engine) as session:
            run = session.get(ScanRun, run_id)
            run.finished_at = utcnow()
            run.status = outcome.status
            run.found = outcome.found
            run.new = outcome.new
            run.message = outcome.message
            session.add(run)
            session.commit()
            session.refresh(run)
        logger.info(
            "Scan %s: %s, found %d, new %d, %d page loads, %.0fs%s",
            kind,
            outcome.status,
            outcome.found,
            outcome.new,
            outcome.page_loads,
            (run.finished_at - run.started_at).total_seconds(),
            f" ({outcome.message})" if outcome.message else "",
        )
        return run

    def _backoff(self, kind: ScanKind) -> str | None:
        """Why this scanner sits out this run, or None.

        After an HTTP 429 the scanner waits at least ``RATE_LIMIT_WAIT_MINUTES``; after two or more
        failures in a row the wait doubles each time (its group's interval x 2, x 4, ...) up to
        ``BACKOFF_CAP_MINUTES``. Retrying a throttled site every interval keeps it throttled.
        """
        with Session(self.engine) as session:
            interval = run_interval(session, group_of(kind), self.settings.scan_interval_minutes)
            runs = session.exec(
                select(ScanRun)
                .where(
                    ScanRun.kind == kind,
                    col(ScanRun.status).in_([ScanStatus.ok, ScanStatus.failed]),
                    col(ScanRun.finished_at).is_not(None),
                )
                .order_by(col(ScanRun.finished_at).desc())
                .limit(20)
            ).all()
        failures = 0
        last = None
        for run in runs:
            if run.status != ScanStatus.failed or run.found > 0:
                break  # a run that found results proves the site works, even if it stopped early
            if is_local_problem(run.message):
                continue  # this computer was offline or asleep: says nothing about the site
            failures += 1
            last = last or run
        if failures == 0:
            return None
        rate_limited = "429" in (last.message or "")
        if not rate_limited and failures < 2:
            return None
        wait = interval * 2 ** (failures - 1)
        if rate_limited:
            wait = max(wait, RATE_LIMIT_WAIT_MINUTES)
        wait = min(wait, BACKOFF_CAP_MINUTES)
        finished = last.finished_at if last.finished_at.tzinfo else last.finished_at.replace(tzinfo=UTC)
        resume = finished + timedelta(minutes=wait)
        # A little slack so a run scheduled right at the boundary isn't skipped by a few seconds.
        if utcnow() >= resume - timedelta(minutes=2):
            return None
        reason = "the site answered HTTP 429 (too many requests)" if rate_limited else f"{failures} failed runs in a row"
        at = resume.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%H:%M")
        return f"Paused because {reason}; the next try is after {at}."

    def _last_ok_started_at(self, kind: ScanKind) -> datetime | None:
        with Session(self.engine) as session:
            return session.exec(
                select(func.max(ScanRun.started_at)).where(ScanRun.kind == kind, ScanRun.status == ScanStatus.ok)
            ).one()

    def last_finished_at(self, platform: Platform | str | None = None) -> datetime | None:
        """When the last run of this group (None: of any scanner) finished."""
        query = select(func.max(ScanRun.finished_at))
        if platform is not None:
            query = query.where(col(ScanRun.kind).in_(GROUPS[Platform(platform)]))
        with Session(self.engine) as session:
            last = session.exec(query).one()
        return to_utc(last) if last is not None else None

    def latest(self) -> list[ScanRun]:
        """The most recent run of each kind that has run, in ScanKind order."""
        runs = []
        with Session(self.engine) as session:
            for kind in ScanKind:
                run = session.exec(
                    select(ScanRun)
                    .where(ScanRun.kind == kind)
                    .order_by(col(ScanRun.started_at).desc(), col(ScanRun.id).desc())
                    .limit(1)
                ).first()
                if run is not None:
                    runs.append(run)
        return runs

    def shutdown(self) -> None:
        """Ask a running scan to stop at its next page load."""
        self._stop.set()
