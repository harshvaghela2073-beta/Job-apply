"""LinkedIn connection notes with the saved login (docs/API_CONTRACT.md, "Automatic email and LinkedIn
outreach"; decided by the candidate on 2026-10-14).

**Who** (``LinkedInOutreach``, scan kind ``linkedin_outreach``, after ``email_send``, while the
``auto_send_linkedin_notes`` switch is on): only people hiring for one of the candidate's roles in
Bengaluru or remote in India (``app.sources.linkedin.hiring``, 2026-10-27), for jobs found in the
last ``OUTREACH_JOB_DAYS`` days, from

1. "Meet the hiring team" on the LinkedIn job pages of jobs in Bengaluru or remote in India. A job
   page is opened once (``Job.contact_checked_at``), at most ``JOB_PAGES_PER_RUN`` per run; Easy
   Apply also records the team from the page it already has open.
2. the authors of hiring posts that ``linkedin_posts`` judged hiring (not a job seeker, a job alert
   or a reshare; Bengaluru or remote in India; a hiring headline or "we're hiring"; company known).

Each job keeps one person (``Job.contact_name`` / ``contact_profile_url``): the job poster first,
never a 1st-degree connection or someone whose headline says they're looking for a job. Jobs that
were skipped or failed are left out.

**The note:** a ``linkedin_message`` template with both ``[Job title]`` and ``[Company]``
(``build_note``), with ``[Name]`` (first name) filled too. A job whose role or company isn't known
is skipped. Over 300 characters with every template -> that person is skipped.

**Sending** (``send_connection_note``), from the person's profile: Connect (or More -> Connect) ->
"Add a note" -> fill -> Send; success is the "Invitation sent" toast, "Pending" on the profile, or
the dialog closing with no error (the invitation page of the 2026-10-15 layout shows neither).
Errors are read from the dialog and LinkedIn's toasts only, never the whole page.

- No Connect (already connected, pending, follow-only), or LinkedIn wants their email: skipped,
  recorded as ``failed`` with the reason so the profile isn't visited again.
- "Weekly invitation limit" stops the run and pauses notes for ``LIMIT_PAUSE``; no personalized
  notes left this month stops them until next month. Neither ever sends an invitation without the
  note.
- A login wall or security check stops the run and marks LinkedIn disconnected.

**Limits:** at most ``OUTREACH_MAX_PER_RUN`` per run and ``OUTREACH_DAILY_CAP`` per day, under
``CONNECTION_NOTES_MONTHLY_CAP`` a month; never the same profile twice (any earlier connection note
or InMail to it, whatever its status); a random ``NOTE_WAIT_SECONDS`` pause after each note sent.
InMails are never sent automatically.

**Recording:** each note is an ``OutreachMessage`` (kind ``connection_note``, ``sent`` or ``failed``)
linked to the job, written as soon as it is sent.

Selectors and texts live in ``selectors.py``; they need a live check.
"""

import logging
from urllib.parse import quote
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy import and_, not_, or_
from sqlmodel import Session, col, select

from app.browser import BrowserSession, BrowserUnavailable
from app.browser.driver import PageResult
from app.models import (
    CONNECTION_NOTE_MAX_CHARS,
    ApplyType,
    Job,
    JobStatus,
    OutreachKind,
    OutreachMessage,
    OutreachStatus,
    Platform,
    ScanKind,
    ScanRun,
    ScanStatus,
    Template,
)
from app.scanner.base import LoginWall, Pacer, ScanBlocked, ScanContext, ScanOutcome, capture_failure
from app.services.app_settings import AUTO_SEND_LINKEDIN_NOTES, get_flag
from app.services.outreach import (
    DraftProblem,
    MissingDetail,
    applied_with_easy_apply,
    choose_note_template,
    clean_job_title,
    fill_placeholders,
    first_name,
    is_hiring_post,
    known_company,
    mentions_easy_apply,
    names_role_and_company,
    note_templates,
    ready_text,
    sent_this_month,
    sent_today,
)
from app.sources.linkedin.hiring import team_outreach_problem
from app.sources.linkedin import selectors as S
from app.sources.linkedin.easy_apply import LOGIN_EXPIRED, SECURITY_CHECK, check_job_page
from app.sources.linkedin.login import LINKEDIN_LOGIN
from app.sources.linkedin.parse import (
    HiringPerson,
    normalize_profile_url,
    parse_hiring_team,
    pick_contact,
    profile_slug,
    read_profile,
)
from app.timeutils import local_day_bounds, local_month_bounds, to_utc, utcnow

logger = logging.getLogger(__name__)

OUTREACH_MAX_PER_RUN = 3
OUTREACH_DAILY_CAP = 12
NOTE_WAIT_SECONDS = (60.0, 180.0)
MAX_PAGE_LOADS = 12  # job pages + profiles per run
JOB_PAGES_PER_RUN = 6
MAX_FAILED_IN_A_ROW = 2
DIALOG_WAIT_SECONDS = 8
CONFIRM_TIMEOUT_SECONDS = 12
SEND_SETTLE_SECONDS = 3  # no error this long after the dialog closed: sent
LIMIT_PAUSE = timedelta(hours=24)
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
# Statuses of jobs whose people get a note (not skipped by the rules, not failed).
OUTREACH_JOB_STATUSES = (JobStatus.found, JobStatus.queued, JobStatus.applied, JobStatus.needs_manual)
WEEKLY_LIMIT_STOP = "LinkedIn says the weekly invitation limit is reached"
NOTE_LIMIT_STOP = "LinkedIn says this month's personalized notes are used up"
NO_CONNECT = "LinkedIn shows no Connect button (already connected, invitation pending, or follow-only)."


class NoteResult(StrEnum):
    sent = "sent"
    sent_without_note = "sent_without_note"  # LinkedIn sent the invitation as soon as Connect was clicked
    would_send = "would_send"  # dry run: the note was filled and Send found; nothing was sent
    no_connect = "no_connect"
    too_long = "too_long"
    email_required = "email_required"
    unavailable = "unavailable"
    weekly_limit = "weekly_limit"
    note_limit = "note_limit"
    failed = "failed"


SENT_RESULTS = (NoteResult.sent, NoteResult.sent_without_note)
STOP_RESULTS = (NoteResult.weekly_limit, NoteResult.note_limit)
# Recorded as "failed" so the profile isn't visited again; the person is simply skipped.
SKIP_RESULTS = (NoteResult.no_connect, NoteResult.email_required, NoteResult.unavailable)


@dataclass
class NoteAttempt:
    result: NoteResult
    detail: str
    note: str = ""
    name: str | None = None  # as the profile page shows it
    buttons: list[str] = field(default_factory=list)  # buttons seen on the profile and in the dialog
    clicked_send: bool = False


# ---------------------------------------------------------------------------
# One profile


def check_profile_page(result: PageResult) -> None:
    """Raise ``LoginWall`` / ``ScanBlocked`` for a login wall, a security check or a block."""
    if LINKEDIN_LOGIN.is_security_check(result.url):
        raise LoginWall("LinkedIn showed a security check; stopped.")
    if LINKEDIN_LOGIN.is_login_wall(result.url):
        raise LoginWall("LinkedIn redirected to its login page; the saved login has expired.")
    if result.status in S.BLOCK_STATUSES:
        raise ScanBlocked(f"LinkedIn returned HTTP {result.status} to the profile page; stopped.")


class _Seen(list):
    def add(self, labels) -> None:
        for label in labels:
            if label and label not in self:
                self.append(label)


def _dismiss(browser: BrowserSession) -> None:
    """Close the invitation dialog without sending."""
    for _ in range(3):
        if not read_profile(browser.html()).dialog:
            return
        if not browser.click_role("button", S.DISMISS_NAME, within=S.INVITE_DIALOG_SCOPE):
            return
        browser.wait(1.0)


def _click_own_connect_link(browser: BrowserSession, slug: str | None, seen: _Seen) -> bool:
    """The 2026-10-15 layout: click this person's Connect link (by its vanityName), first on the page,
    else in the "More" menu. Never another person's link."""
    if not slug:
        return False
    selector = f"{S.CUSTOM_INVITE_LINK}[href*='vanityName={slug}' i]"
    view = read_profile(browser.html(), slug)
    if not view.new_layout:
        return False
    if view.connect_href and browser.click(selector):
        return True
    if not browser.click_role("button", S.MORE_NAME):  # the first "More" is the top card's
        return False
    browser.wait(1.0)
    view = read_profile(browser.html(), slug)
    seen.add(view.buttons)
    if view.connect_href and browser.click(selector):
        return True
    browser.click_role("button", S.MORE_NAME)  # close the menu
    return False


def _click_connect(browser: BrowserSession, name: str | None, seen: _Seen, slug: str | None = None) -> bool:
    """Click "Connect" on the top card, or "More" -> "Connect". The name-specific pattern comes
    first so another person's Connect button (e.g. "People also viewed") is never clicked."""
    if _click_own_connect_link(browser, slug, seen):
        return True
    patterns = ([rf"^\s*invite {re.escape(name)} to connect\s*$"] if name else []) + [S.CONNECT_NAME]
    for pattern in [*patterns, S.CONNECT_TEXT_NAME]:
        if browser.click_role("button", pattern, within=S.PROFILE_TOP_CARD_SCOPE):
            return True
    if not browser.click_role("button", S.MORE_NAME, within=S.PROFILE_TOP_CARD_SCOPE):
        return False
    browser.wait(1.0)
    seen.add(read_profile(browser.html()).buttons)
    for role in S.MENU_ROLES:
        for pattern in [*patterns, S.CONNECT_TEXT_NAME]:
            if browser.click_role(role, pattern, within=S.PROFILE_TOP_CARD_SCOPE):
                return True
        if name and browser.click_role(role, patterns[0]):  # a menu rendered outside the top card
            return True
    browser.click_role("button", S.MORE_NAME, within=S.PROFILE_TOP_CARD_SCOPE)  # close the menu
    return False


def _wait_for(browser: BrowserSession, done: Callable, seconds: float, seen: _Seen, slug: str | None = None):
    view = read_profile(browser.html(), slug)
    waited = 0.0
    while not done(view) and waited < seconds:
        browser.wait(1.0)
        waited += 1.0
        view = read_profile(browser.html(), slug)
    seen.add(view.dialog_buttons)
    return view


def send_connection_note(
    browser: BrowserSession,
    profile_url: str,
    note: str | Callable[[str | None], str],
    *,
    dry_run: bool = False,
    kind: str = ScanKind.linkedin_outreach.value,
) -> NoteAttempt:
    """Open a profile with the saved login and send a connection request with ``note`` (or, in a
    dry run, fill the note and close the dialog without sending). ``note`` may be a function of the
    name the profile shows. Raises ``LoginWall`` / ``ScanBlocked``."""
    result = browser.goto(profile_url, wait_for=S.PROFILE_READY, timeout=40)
    check_profile_page(result)
    slug = profile_slug(profile_url)
    view = read_profile(browser.html(), slug)
    if not view.buttons and not view.unavailable:
        browser.wait(2.0)  # the top card's buttons render late on slow loads
        view = read_profile(browser.html(), slug)
    seen = _Seen()
    seen.add(view.buttons)
    name = view.name
    text = note(name) if callable(note) else note

    def attempt(result: NoteResult, detail: str, *, clicked: bool = False) -> NoteAttempt:
        return NoteAttempt(result, detail, text, name, list(seen), clicked)

    if len(text) > CONNECTION_NOTE_MAX_CHARS:
        return attempt(NoteResult.too_long, f"The note has {len(text)} characters; LinkedIn allows {CONNECTION_NOTE_MAX_CHARS}.")
    if view.unavailable:
        return attempt(NoteResult.unavailable, "LinkedIn says this profile isn't available.")
    if view.pending:
        return attempt(NoteResult.no_connect, "An invitation to this person is already pending.")
    sent_before = view.sent
    if view.weekly_limit:
        return attempt(NoteResult.weekly_limit, WEEKLY_LIMIT_STOP + ".")
    if view.new_layout and slug:
        # The 2026-10-15 layout opens the invitation inside LinkedIn's hidden "preload" frame, which the
        # page's HTML doesn't show (129 notes failed that way on 2026-10-20, Connect also sits under
        # "More" for many people): open that person's invitation page itself instead of clicking.
        browser.goto(S.CUSTOM_INVITE_URL.format(slug=quote(slug)), timeout=40)
        view = _wait_for(
            browser, lambda v: v.dialog or v.note_box or v.weekly_limit or v.pending, DIALOG_WAIT_SECONDS, seen, slug
        )
        if not (view.dialog or view.note_box or view.weekly_limit or view.pending):
            return attempt(NoteResult.no_connect, NO_CONNECT + capture_failure(browser, kind))
    else:
        if not _click_connect(browser, name, seen, slug):
            # Nothing readable on the top card usually means a changed page layout: keep the page.
            saved = "" if view.buttons else capture_failure(browser, kind)
            return attempt(NoteResult.no_connect, NO_CONNECT + saved)
        view = _wait_for(
            browser, lambda v: v.dialog or v.weekly_limit or (v.sent and not sent_before) or v.pending,
            DIALOG_WAIT_SECONDS, seen, slug,
        )
    if view.weekly_limit:
        _dismiss(browser)
        return attempt(NoteResult.weekly_limit, WEEKLY_LIMIT_STOP + "; nothing was sent.")
    if view.email_required:
        _dismiss(browser)
        return attempt(NoteResult.email_required, "LinkedIn asks for this person's email address to connect; skipped.")
    if not view.dialog:
        if (view.sent and not sent_before) or view.pending:
            return attempt(
                NoteResult.sent_without_note,
                "LinkedIn sent the invitation as soon as Connect was clicked, without the note.",
                clicked=True,
            )
        note_text = capture_failure(browser, kind)
        return attempt(NoteResult.failed, "The invitation dialog didn't open after Connect." + note_text)

    if not view.note_box:
        if not browser.click_role("button", S.ADD_NOTE_NAME, within=S.INVITE_DIALOG_SCOPE):
            note_text = capture_failure(browser, kind)
            _dismiss(browser)
            return attempt(NoteResult.failed, 'The invitation dialog has no "Add a note" button; nothing was sent.' + note_text)
        view = _wait_for(browser, lambda v: v.note_box or v.note_limit or v.weekly_limit, DIALOG_WAIT_SECONDS, seen, slug)
    if view.note_limit:
        _dismiss(browser)
        return attempt(NoteResult.note_limit, NOTE_LIMIT_STOP + "; nothing was sent.")
    if view.weekly_limit:
        _dismiss(browser)
        return attempt(NoteResult.weekly_limit, WEEKLY_LIMIT_STOP + "; nothing was sent.")
    if not view.note_box:
        note_text = capture_failure(browser, kind)
        _dismiss(browser)
        return attempt(NoteResult.failed, "The note box didn't open; nothing was sent." + note_text)

    fields = browser.form_fields(S.INVITE_DIALOG_SCOPE, S.NOTE_HINTS)
    box = next((f for f in fields if f.get("dom_id") in S.NOTE_TEXTAREA_IDS), None) or next(
        (f for f in fields if f.get("type") == "textarea"), None
    )
    if box is None or not browser.fill_field(box, text, S.NOTE_HINTS):
        note_text = capture_failure(browser, kind)
        _dismiss(browser)
        return attempt(
            NoteResult.failed,
            "The note couldn't be filled in (LinkedIn may allow fewer characters); nothing was sent." + note_text,
        )
    view = read_profile(browser.html(), slug)
    seen.add(view.dialog_buttons)
    has_send = any(re.search(S.SEND_NAME, label, re.I) for label in view.dialog_buttons)
    if dry_run:
        _dismiss(browser)
        if not has_send:
            return attempt(NoteResult.failed, "Filled the note but found no Send button (dry run; nothing was sent).")
        return attempt(NoteResult.would_send, "Filled the note and found Send; closed the dialog without sending (dry run).")
    if not browser.click_role("button", S.SEND_NAME, within=S.INVITE_DIALOG_SCOPE):
        note_text = capture_failure(browser, kind)
        _dismiss(browser)
        return attempt(NoteResult.failed, "The Send button couldn't be clicked; nothing was sent." + note_text)

    waited = 0.0
    closed = 0.0  # seconds since the invitation dialog closed
    while waited < CONFIRM_TIMEOUT_SECONDS:
        browser.wait(1.0)
        waited += 1.0
        view = read_profile(browser.html(), slug)
        seen.add(view.buttons)
        if view.weekly_limit:
            _dismiss(browser)
            return attempt(NoteResult.weekly_limit, WEEKLY_LIMIT_STOP + "; the note may not have been sent.", clicked=True)
        if view.error:
            _dismiss(browser)
            return attempt(NoteResult.failed, f'LinkedIn said "{view.error}" after Send.', clicked=True)
        if (view.sent and not sent_before) or view.pending:
            return attempt(NoteResult.sent, "Sent the connection request with the note.", clicked=True)
        if not (view.dialog or view.note_box):
            # The invitation page (2026-10-15 layout) shows no toast and no "Pending": the dialog closing
            # with no error after a short wait is the confirmation (replies to these notes came back).
            closed += 1.0
            if closed >= SEND_SETTLE_SECONDS:
                return attempt(NoteResult.sent, "Sent the connection request with the note (the dialog closed).", clicked=True)
    note_text = capture_failure(browser, kind)
    return attempt(NoteResult.failed, "Clicked Send but LinkedIn showed no confirmation." + note_text, clicked=True)


def find_hiring_team(browser: BrowserSession, job_url: str) -> list[HiringPerson]:
    """The "Meet the hiring team" people on a logged-in job page. Raises ``LoginWall`` /
    ``ScanBlocked``."""
    result = browser.goto(job_url, wait_for=S.HIRING_TEAM_READY, timeout=40)
    check_job_page(result)
    people = parse_hiring_team(browser.html())
    if not people:
        browser.scroll()  # the section can render only once scrolled into view
        people = parse_hiring_team(browser.html())
    return people


# ---------------------------------------------------------------------------
# The note


@dataclass(frozen=True)
class JobInfo:
    id: int
    title: str
    company: str
    url: str
    apply_type: ApplyType
    status: JobStatus
    contact_name: str | None = None
    contact_profile_url: str | None = None
    matched_role: str | None = None
    location: str = ""

    @classmethod
    def of(cls, job: Job) -> "JobInfo":
        return cls(
            job.id, job.title, job.company, job.url, ApplyType(job.apply_type), JobStatus(job.status),
            job.contact_name, job.contact_profile_url, job.matched_role, job.location or "",
        )


def build_note(templates: list[Template], job: JobInfo | Job | None, name: str | None) -> str:
    """The connection note for ``name`` about ``job``, naming its role and company (2026-10-27). Only
    templates with both ``[Job title]`` and ``[Company]`` are used: the chosen one first, then the
    others, each with the job's title and then its shorter matched role, until one fits in 300
    characters (else the first is returned and the caller skips it). Raises ``MissingDetail`` when
    the role or company isn't known, ``DraftProblem`` when no template names both or a placeholder is
    left."""
    usable = [t for t in templates if names_role_and_company(t)]
    template = choose_note_template(usable, job)
    if template is None:
        raise DraftProblem("No connection-note template has both [Job title] and [Company]")
    company = known_company(job.company, job.contact_name) if job is not None else None
    titles = []
    for title in (clean_job_title(job.title), clean_job_title(job.matched_role)) if job is not None else ():
        if title and title not in titles:
            titles.append(title)
    if not company or not titles:
        raise MissingDetail("The job's role or company isn't known")
    easy_apply_ok = applied_with_easy_apply(job)
    order = [template] + [t for t in usable if t is not template and (easy_apply_ok or not mentions_easy_apply(t))]
    notes = []
    for candidate in order:
        for title in titles:
            text = fill_placeholders(candidate.answer, job_title=title, company=company, name=first_name(name))
            text = ready_text(" ".join(text.split()), f'template "{candidate.question}"')
            if len(text) <= CONNECTION_NOTE_MAX_CHARS:
                return text
            notes.append(text)
    return notes[0]


# Until this moment every "no Connect button" came from LinkedIn's new profile layout, which the tool
# couldn't read (fixed on 2026-10-15): those people are tried once more.
NO_CONNECT_FIXED_AT = datetime(2026, 10, 15, 12, 20, tzinfo=UTC)
# Until this moment the invitation opened inside LinkedIn's hidden frame and the tool couldn't see it
# (fixed on 2026-10-21): notes that failed that way are tried once more too.
INVITE_FRAME_FIXED_AT = datetime(2026, 10, 21, 12, 30, tzinfo=UTC)
INVITE_FRAME_ERRORS = ("The invitation dialog didn't open", "The note box didn't open", "something went wrong")
# Recruiters of jobs found this many days back are written to (today's first).
OUTREACH_JOB_DAYS = 7


def contacted_profiles(session: Session) -> set[str]:
    """Profiles that already have a connection note or InMail (any status): never written to again,
    except a "no Connect button" failure from before ``NO_CONNECT_FIXED_AT`` (tried once more)."""
    layout_bug = or_(
        and_(
            OutreachMessage.status == OutreachStatus.failed,
            col(OutreachMessage.error).contains(NO_CONNECT),
            col(OutreachMessage.created_at) < NO_CONNECT_FIXED_AT,
        ),
        and_(
            OutreachMessage.status == OutreachStatus.failed,
            or_(*(col(OutreachMessage.error).contains(text) for text in INVITE_FRAME_ERRORS)),
            col(OutreachMessage.created_at) < INVITE_FRAME_FIXED_AT,
        ),
    )
    urls = session.exec(
        select(OutreachMessage.recipient_profile_url).where(
            col(OutreachMessage.kind).in_([OutreachKind.connection_note, OutreachKind.inmail]),
            col(OutreachMessage.recipient_profile_url).is_not(None),
            not_(layout_bug),
        )
    ).all()
    return {normalized for url in urls if (normalized := normalize_profile_url(url))}


def record_note(engine, attempt: NoteAttempt, *, profile_url: str, name: str | None, job_id: int | None) -> OutreachMessage | None:
    """Store a note that was sent or failed (skips are stored as ``failed`` with the reason). Limits
    and too-long notes are not stored."""
    if attempt.result in SENT_RESULTS:
        status, error = OutreachStatus.sent, None
        if attempt.result == NoteResult.sent_without_note:
            error = attempt.detail
    elif attempt.result == NoteResult.failed:
        status, error = OutreachStatus.failed, attempt.detail
    elif attempt.result in SKIP_RESULTS:
        status, error = OutreachStatus.failed, f"Not sent: {attempt.detail}"
    else:
        return None
    now = utcnow()
    message = OutreachMessage(
        kind=OutreachKind.connection_note,
        job_id=job_id,
        recipient_name=name or attempt.name or profile_url,
        recipient_profile_url=profile_url,
        body=attempt.note,
        status=status,
        error=error,
        sent_at=now if status == OutreachStatus.sent else None,
    )
    with Session(engine) as session:
        session.add(message)
        session.commit()
        session.refresh(message)
    return message


def store_contact(engine, job_id: int, people: list[HiringPerson]) -> HiringPerson | None:
    """Remember the job's hiring-team contact (and that its page was checked)."""
    person = pick_contact(people)
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            return person
        job.contact_checked_at = utcnow()
        if person is not None and not job.contact_profile_url:
            job.contact_name, job.contact_profile_url = person.name, person.profile_url
        session.add(job)
        session.commit()
    return person


# ---------------------------------------------------------------------------
# The scheduled sender


@dataclass
class _Target:
    profile_url: str
    name: str
    job: JobInfo | None
    note: str


def limit_pause(engine, now: datetime) -> str | None:
    """Why notes sit out this run after LinkedIn's weekly or monthly limit, or None."""
    with Session(engine) as session:
        runs = session.exec(
            select(ScanRun)
            .where(
                ScanRun.kind == ScanKind.linkedin_outreach,
                col(ScanRun.started_at) >= to_utc(now) - timedelta(days=31),
                col(ScanRun.message).contains("Stopped: LinkedIn says"),
            )
            .order_by(col(ScanRun.started_at).desc())
            .limit(1)
        ).all()
    if not runs:
        return None
    run = runs[0]
    started = to_utc(run.started_at)
    if NOTE_LIMIT_STOP in (run.message or ""):
        resume, what = local_month_bounds(started)[1], "this month's personalized notes were used up"
    elif WEEKLY_LIMIT_STOP in (run.message or ""):
        resume, what = started + LIMIT_PAUSE, "the weekly invitation limit was reached"
    else:
        return None
    if to_utc(now) >= resume:
        return None
    at = resume.astimezone(LOCAL_TZ).strftime("%d %b %H:%M")
    return f"Paused: LinkedIn said {what}; the next try is after {at}."


class LinkedInOutreach:
    """Runs last: connection notes to today's hiring-team people and post authors, while the switch is on."""

    kind = ScanKind.linkedin_outreach
    platform = Platform.linkedin

    def __init__(self, wait_range: tuple[float, float] = NOTE_WAIT_SECONDS, rng=random.uniform):
        self.wait_range = wait_range
        self.rng = rng
        self.pauses: list[float] = []  # the pauses taken in the last run (seconds)

    def _pause(self, browser: BrowserSession, ctx: ScanContext) -> bool:
        seconds = self.rng(*self.wait_range)
        self.pauses.append(seconds)
        remaining = seconds
        while remaining > 0:
            if ctx.stop.is_set():
                return False
            chunk = min(remaining, 5.0)
            browser.wait(chunk)
            remaining -= chunk
        return not ctx.stop.is_set()

    def run(self, ctx: ScanContext) -> ScanOutcome:
        self.pauses = []
        settings = ctx.settings
        with Session(ctx.engine) as session:
            if not get_flag(session, AUTO_SEND_LINKEDIN_NOTES):
                return ScanOutcome(status=ScanStatus.skipped, message="Automatic LinkedIn notes are off.")
        paused = limit_pause(ctx.engine, ctx.now)
        if paused:
            return ScanOutcome(status=ScanStatus.skipped, message=paused)
        with Session(ctx.engine) as session:
            templates = note_templates(session)
            if not templates:
                return ScanOutcome(
                    status=ScanStatus.skipped,
                    message="There is no LinkedIn message template for connection notes, so no notes were sent.",
                )
            today = sent_today(session, OutreachKind.connection_note, ctx.now)
            if today >= OUTREACH_DAILY_CAP:
                return ScanOutcome(
                    status=ScanStatus.skipped, message=f"Today's limit of {OUTREACH_DAILY_CAP} connection notes is reached."
                )
            month_cap = settings.connection_notes_monthly_cap
            month = sent_this_month(session, OutreachKind.connection_note, ctx.now)
            if month >= month_cap:
                return ScanOutcome(
                    status=ScanStatus.skipped, message=f"This month's limit of {month_cap} connection notes is reached."
                )
            remaining = min(OUTREACH_MAX_PER_RUN, OUTREACH_DAILY_CAP - today, month_cap - month)
            contacted = contacted_profiles(session)
            start, _ = local_day_bounds(to_utc(ctx.now) - timedelta(days=OUTREACH_JOB_DAYS - 1))
            todays = session.exec(
                select(Job)
                .where(
                    Job.platform == Platform.linkedin,
                    col(Job.status).in_(OUTREACH_JOB_STATUSES),
                    col(Job.found_at) >= start,
                )
                # Newest jobs first (their recruiters are hiring now), then the most important.
                .order_by(col(Job.found_at).desc(), col(Job.priority).asc().nulls_last(), col(Job.id))
            ).all()
            jobs = [JobInfo.of(job) for job in todays]
            unchecked = {job.id for job in todays if job.contact_checked_at is None}
        targets: list[_Target] = []
        skipped: dict[str, int] = {}
        problems: list[str] = []
        queued: set[str] = set()

        def add_target(url: str | None, name: str | None, job: JobInfo | None) -> None:
            url = normalize_profile_url(url)
            if url is None or url in contacted or url in queued:
                return
            try:
                note = build_note(templates, job, name)
            except MissingDetail:
                skipped["role or company not known"] = skipped.get("role or company not known", 0) + 1
                return
            except DraftProblem as error:
                if str(error) not in problems:
                    problems.append(str(error))
                return
            queued.add(url)
            if len(note) > CONNECTION_NOTE_MAX_CHARS:
                skipped["note over 300 characters"] = skipped.get("note over 300 characters", 0) + 1
                return
            targets.append(_Target(url, name or "", job, note))

        # Only jobs in Bengaluru or remote in India (2026-10-27); a hiring post's author was checked
        # when the post was read (hiring.py), so posts keep only authors who may be written to.
        jobs = [job for job in jobs if is_hiring_post(job.title) or team_outreach_problem(job.location, None) is None]
        for job in jobs:
            if job.contact_profile_url:
                add_target(job.contact_profile_url, job.contact_name, job)
        to_check = [
            job for job in jobs
            if job.id in unchecked and not job.contact_profile_url and "/jobs/view/" in job.url
        ][:JOB_PAGES_PER_RUN]
        if not targets and not to_check:
            message = "No new people to send a connection note to."
            if skipped or problems:
                message = self._message(0, 0, 0, skipped, problems, {}, None)
            return ScanOutcome(status=ScanStatus.ok, message=message)
        ctx.connections.sync_from_chrome(self.platform)
        if not ctx.connections.login_usable(self.platform):
            return ScanOutcome(status=ScanStatus.skipped, message="LinkedIn isn't connected, so no notes were sent.")

        profile_dir = ctx.browsers.profile_dir(self.platform)
        pacer = Pacer(min(MAX_PAGE_LOADS, settings.scan_max_page_loads), ctx.wait_range, ctx.stop)

        def task(driver):
            browser = driver.open(profile_dir=profile_dir, headless=settings.scan_headless)
            counts: dict[NoteResult, int] = {}
            checked = {"pages": 0, "people": 0}
            failure = login_lost = None
            try:
                if not LINKEDIN_LOGIN.has_auth_cookie(browser.cookies()):
                    raise LoginWall(LOGIN_EXPIRED)
                for job in to_check:
                    if len(targets) >= remaining or not pacer.next_load():
                        break
                    people = find_hiring_team(browser, job.url)
                    checked["pages"] += 1
                    person = store_contact(ctx.engine, job.id, people)
                    if person is not None:
                        checked["people"] += 1
                        add_target(person.profile_url, person.name, job)
                sent = 0
                clicked = False
                failed_in_a_row = 0
                for target in targets:
                    if sent >= remaining or ctx.stop.is_set():
                        break
                    if clicked and not self._pause(browser, ctx):  # after a note was sent
                        break
                    if not pacer.next_load():
                        break
                    attempt = send_connection_note(browser, target.profile_url, target.note, kind=self.kind.value)
                    clicked = attempt.clicked_send
                    counts[attempt.result] = counts.get(attempt.result, 0) + 1
                    record_note(
                        ctx.engine, attempt, profile_url=target.profile_url, name=target.name or attempt.name,
                        job_id=target.job.id if target.job else None,
                    )
                    logger.info("LinkedIn note to %s: %s %s", target.profile_url, attempt.result, attempt.detail)
                    if attempt.result in SENT_RESULTS:
                        sent += 1
                    if attempt.result in STOP_RESULTS:
                        failure = attempt.detail
                        break
                    failed_in_a_row = failed_in_a_row + 1 if attempt.result == NoteResult.failed else 0
                    if failed_in_a_row >= MAX_FAILED_IN_A_ROW:
                        failure = (
                            f"{MAX_FAILED_IN_A_ROW} notes in a row couldn't be sent; check the screenshots in data/diagnostics."
                        )
                        break
            except LoginWall as error:
                failure = str(error)
                login_lost = SECURITY_CHECK if "security check" in failure.lower() else LOGIN_EXPIRED
            except (ScanBlocked, BrowserUnavailable) as error:
                failure = str(error) + capture_failure(browser, self.kind.value)
            except Exception as error:  # noqa: BLE001 - keep what was done so far
                logger.exception("LinkedIn outreach stopped")
                text = str(error).strip()
                failure = f"Sending notes stopped: {text.splitlines()[0] if text else type(error).__name__}"
                failure += capture_failure(browser, self.kind.value)
            finally:
                browser.close()
            return counts, checked, failure, login_lost

        counts, checked, failure, login_lost = ctx.browsers.worker(self.platform.value).submit(task).result()
        if login_lost:
            ctx.connections.record_login_lost(self.platform, login_lost)
        sent = sum(counts.get(r, 0) for r in SENT_RESULTS)
        attempts = sum(counts.values())
        message = self._message(sent, checked["pages"], checked["people"], skipped, problems, counts, failure)
        nothing_done = not attempts and not checked["pages"]
        status = ScanStatus.failed if failure and nothing_done else ScanStatus.ok
        return ScanOutcome(status=status, found=attempts, new=sent, message=message, page_loads=pacer.loads)

    @staticmethod
    def _message(sent, pages, people, skipped, problems, counts, failure) -> str:
        parts = [f"Sent {sent} connection note{'' if sent == 1 else 's'}"]
        labels = {
            NoteResult.sent_without_note: "sent without the note (LinkedIn didn't offer one)",
            NoteResult.no_connect: "skipped (no Connect button: already connected, pending or follow-only)",
            NoteResult.email_required: "skipped (LinkedIn wants their email)",
            NoteResult.unavailable: "skipped (profile not available)",
            NoteResult.failed: "failed",
        }
        parts += [f"{counts[r]} {label}" for r, label in labels.items() if counts.get(r)]
        parts += [f"{count} skipped ({reason})" for reason, count in skipped.items()]
        message = ", ".join(parts) + "."
        if pages:
            message += f" Checked {pages} job page{'' if pages == 1 else 's'} for the hiring team ({people} to write to)."
        for problem in problems:
            message += f" {problem}; those people were skipped."
        if failure:
            message += f" Stopped: {failure}"
        return message
