"""Request and response models for the HTTP API (see docs/API_CONTRACT.md)."""

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    field_validator,
)

from app.models import (
    ApplyType,
    ConnectionStatus,
    JobStatus,
    OutreachKind,
    OutreachStatus,
    Platform,
    ScanKind,
    ScanStatus,
    TemplateKind,
)
from app.timeutils import iso_utc

T = TypeVar("T")

# Datetimes are always emitted as UTC ISO 8601 with a trailing "Z".
UTCDatetime = Annotated[datetime, PlainSerializer(iso_utc, return_type=str)]
NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _normalize_text(value: str) -> str:
    """Use ``\\n`` line breaks and drop leading/trailing whitespace; inner line breaks are kept."""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _blank_to_none(value: str | None) -> str | None:
    return value or None


# Multi-line text (answers, message bodies, profile values).
TextStr = Annotated[str, AfterValidator(_normalize_text)]
NonBlankTextStr = Annotated[TextStr, StringConstraints(min_length=1)]
OptionalTextStr = Annotated[TextStr | None, AfterValidator(_blank_to_none)]


class Page(BaseModel, Generic[T]):
    """List envelope used by every list endpoint."""

    items: list[T]
    total: int


class _ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Jobs


class JobRead(_ORMModel):
    id: int
    platform: Platform
    external_id: str
    title: str
    company: str
    location: str
    url: str
    apply_url: str | None
    form_url: str | None
    contact_email: str | None
    post_url: str | None
    short_description: str
    apply_type: ApplyType
    status: JobStatus
    status_reason: str | None
    found_at: UTCDatetime
    applied_at: UTCDatetime | None
    updated_at: UTCDatetime
    posted_at: UTCDatetime | None
    experience: str | None
    matched_role: str | None
    priority: int | None


class AppSettingsRead(BaseModel):
    auto_apply_naukri: bool
    auto_apply_linkedin: bool
    auto_send_email: bool
    auto_send_linkedin_notes: bool
    pause_naukri: bool  # Naukri search and auto-apply are skipped while on
    # Per-feature pauses (2026-10-15); a paused scanner records "skipped" and opens no browser.
    pause_linkedin: bool  # every LinkedIn scanner, the email sender included
    pause_linkedin_jobs: bool
    pause_linkedin_posts: bool
    pause_naukri_jobs: bool
    # Minutes between LinkedIn scan runs, between Naukri scan runs, and between LinkedIn's job and
    # post searches (2026-10-15, "Separate LinkedIn and Naukri scans").
    linkedin_scan_interval_minutes: int
    naukri_scan_interval_minutes: int
    linkedin_search_interval_minutes: int


class AppSettingsUpdate(BaseModel):
    auto_apply_naukri: bool | None = None
    auto_apply_linkedin: bool | None = None
    auto_send_email: bool | None = None
    auto_send_linkedin_notes: bool | None = None
    pause_naukri: bool | None = None
    pause_linkedin: bool | None = None
    pause_linkedin_jobs: bool | None = None
    pause_linkedin_posts: bool | None = None
    pause_naukri_jobs: bool | None = None
    # Ranges: app.services.app_settings.MINUTES_RANGE (LinkedIn searches no more often than every 30 minutes).
    linkedin_scan_interval_minutes: int | None = Field(default=None, ge=10, le=240)
    naukri_scan_interval_minutes: int | None = Field(default=None, ge=10, le=240)
    linkedin_search_interval_minutes: int | None = Field(default=None, ge=30, le=720)


class ApplyAttemptRead(BaseModel):
    result: str
    detail: str
    questions: list[str]
    facts: dict
    job: "JobRead"


class JobPatch(BaseModel):
    # "found" is shown as Pending in the app: the job goes back to the list of jobs to apply to.
    status: Literal["applied", "skipped", "needs_manual", "found"]
    status_reason: str | None = None


# ---------------------------------------------------------------------------
# Outreach


class OutreachMessageRead(_ORMModel):
    id: int
    kind: OutreachKind
    job_id: int | None
    recipient_name: str
    recipient_profile_url: str | None
    recipient_email: str | None
    subject: str | None
    body: str
    status: OutreachStatus
    error: str | None
    created_at: UTCDatetime
    approved_at: UTCDatetime | None
    sent_at: UTCDatetime | None


class TestEmailRead(BaseModel):
    sent: bool
    to: str
    subject: str


class TestNoteRead(BaseModel):
    # sent | would_send | no_connect | too_long | weekly_limit | note_limit | email_required | failed
    result: str
    detail: str
    note: str
    buttons_found: list[str]
    profile_url: str
    name: str | None


class OutreachPatch(BaseModel):
    subject: str | None = None
    body: str | None = None

    @field_validator("body")
    @classmethod
    def _body_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("body must not be empty")
        return value


# ---------------------------------------------------------------------------
# Unanswered questions


class UnansweredQuestionRead(_ORMModel):
    id: int
    question: str
    platform: Platform
    job_id: int | None
    seen_at: UTCDatetime
    answered_at: UTCDatetime | None


class AnswerBody(BaseModel):
    answer: NonBlankTextStr


# ---------------------------------------------------------------------------
# Template Library


class TemplateRead(_ORMModel):
    id: int
    kind: TemplateKind
    question: str
    answer: str
    subject: str | None
    is_answered: bool
    created_at: UTCDatetime
    updated_at: UTCDatetime


class TemplateWrite(BaseModel):
    # Create: defaults to "answer". Update: left out keeps the current kind.
    kind: TemplateKind | None = None
    question: NonBlankStr
    # Empty (or "TBD") is allowed and means "no answer yet". Line breaks are kept.
    answer: TextStr
    # Blank means None. Update: left out keeps the current subject.
    subject: OptionalTextStr = None


# ---------------------------------------------------------------------------
# Dashboard


class CapUsage(BaseModel):
    used: int
    limit: int


class Caps(BaseModel):
    easy_apply_today: CapUsage
    naukri_apply_today: CapUsage
    connection_notes_month: CapUsage
    inmail_month: CapUsage


class DashboardSummary(BaseModel):
    applied_today: int
    applied_total: int
    applied_by_platform: dict[str, int]
    needs_action: dict[str, int]
    outreach_pending: int
    unanswered_questions: int
    caps: Caps


# ---------------------------------------------------------------------------
# Analysis


class ApplyTypeCounts(BaseModel):
    found: int
    applied: int  # status applied (for email: emailed)
    pending: int  # found - applied


class LinkedInAnalytics(BaseModel):
    easy_apply: ApplyTypeCounts
    company_site: ApplyTypeCounts
    google_form: ApplyTypeCounts
    email: ApplyTypeCounts
    connection_notes_sent: int


class NaukriAnalytics(BaseModel):
    one_click: ApplyTypeCounts
    company_site: ApplyTypeCounts


class AnalyticsPeriod(BaseModel):
    linkedin: LinkedInAnalytics
    naukri: NaukriAnalytics


class Analytics(BaseModel):
    today: AnalyticsPeriod  # jobs found today (local day), notes sent today
    all: AnalyticsPeriod


# ---------------------------------------------------------------------------
# Connections and scans


class ConnectionRead(BaseModel):
    platform: Platform
    status: ConnectionStatus
    account_name: str | None
    checked_at: UTCDatetime | None
    message: str | None
    # The platform's login page; the frontend opens it in the user's own browser.
    login_url: str


class ScanRunRead(_ORMModel):
    id: int
    kind: ScanKind
    started_at: UTCDatetime
    finished_at: UTCDatetime | None
    status: ScanStatus
    found: int
    new: int
    message: str | None


class ScanGroupStatusRead(BaseModel):
    """One scan group (2026-10-15, "Separate LinkedIn and Naukri scans")."""

    running: bool
    interval_minutes: int
    # Null while this group runs or when scanning is disabled.
    next_run_at: UTCDatetime | None
    last_finished_at: UTCDatetime | None


class ScanGroupsRead(BaseModel):
    linkedin: ScanGroupStatusRead
    naukri: ScanGroupStatusRead


class ScanStatusRead(BaseModel):
    enabled: bool
    running: bool  # any group is running
    # The earliest next run of the groups; null when all are running or scanning is disabled.
    next_run_at: UTCDatetime | None
    interval_minutes: int  # LinkedIn's (kept for older UIs)
    platforms: ScanGroupsRead
    # The most recent run of each kind that has run at least once, in ScanKind order.
    latest: list[ScanRunRead]


class ScanStarted(BaseModel):
    started: bool
    platforms: list[Platform]  # the groups this request started


# ---------------------------------------------------------------------------
# Profile

PROFILE_KEY_PATTERN = r"^[a-z][a-z0-9_]*$"


class ProfileFieldRead(_ORMModel):
    key: str
    label: str
    section: str
    value: str
    position: int


class ProfileFieldWrite(BaseModel):
    key: Annotated[str, StringConstraints(pattern=PROFILE_KEY_PATTERN)]
    label: NonBlankStr
    section: NonBlankStr
    value: TextStr
    # Left out: an existing field keeps its position; a new field goes last (max + 1).
    position: int | None = None


class ProfileFieldsWrite(BaseModel):
    fields: list[ProfileFieldWrite]


class ProfileSectionRead(BaseModel):
    title: str
    fields: list[ProfileFieldRead]


class ResumeRead(BaseModel):
    filename: str
    url: str


class ProfileSummaryRead(BaseModel):
    name: str | None
    email: str | None
    phone: str | None
    location: str | None
    current_company: str | None
    designation: str | None
    total_experience: str | None
    current_ctc: str | None
    graduation: str | None
    linkedin_url: str | None
    naukri_url: str | None
    skills: list[str]
    resume: ResumeRead | None


class ProfileRead(BaseModel):
    summary: ProfileSummaryRead
    sections: list[ProfileSectionRead]


# ---------------------------------------------------------------------------
# Deployment: app login, pushed logins and uploads (docs/API_CONTRACT.md, "Deployment")

# Pushed logins: at most this many cookies per request.
MAX_PUSHED_COOKIES = 300


class AuthStatusRead(BaseModel):
    required: bool
    authenticated: bool


class LoginWrite(BaseModel):
    password: str = Field(max_length=1024)


class LoginRead(BaseModel):
    token: str
    expires_at: UTCDatetime


class PushedCookie(BaseModel):
    """One cookie in Playwright's shape (``context.cookies()`` / storage state)."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=256, pattern=r"^[^\s;=,]+$")
    value: str = Field(max_length=8192)
    # ".linkedin.com" (a domain cookie) or "www.linkedin.com" (host-only).
    domain: str = Field(min_length=1, max_length=255)
    path: str = Field(default="/", max_length=1024, pattern=r"^/[^\s;]*$")
    # Unix seconds; -1 (or missing) for a session cookie.
    expires: float | None = -1
    http_only: bool = Field(default=False, alias="httpOnly")
    secure: bool = False
    same_site: Literal["Strict", "Lax", "None"] = Field(default="Lax", alias="sameSite")


class CookiesWrite(BaseModel):
    cookies: list[PushedCookie] = Field(max_length=MAX_PUSHED_COOKIES)


class ResumeUploadRead(BaseModel):
    bytes: int


class DatabaseUploadRead(BaseModel):
    jobs: int
    templates: int
    profile_fields: int
