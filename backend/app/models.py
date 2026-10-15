"""Enums and database tables (see docs/API_CONTRACT.md)."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, UniqueConstraint, event
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel

from app.timeutils import to_utc, utcnow

CONNECTION_NOTE_MAX_CHARS = 300


class Platform(StrEnum):
    linkedin = "linkedin"
    naukri = "naukri"


class ApplyType(StrEnum):
    easy_apply = "easy_apply"  # LinkedIn
    one_click = "one_click"  # Naukri
    company_site = "company_site"
    google_form = "google_form"
    email = "email"


class JobStatus(StrEnum):
    found = "found"
    queued = "queued"
    applied = "applied"
    needs_manual = "needs_manual"
    failed = "failed"
    skipped = "skipped"


class OutreachKind(StrEnum):
    email = "email"
    connection_note = "connection_note"
    inmail = "inmail"


class OutreachStatus(StrEnum):
    draft = "draft"
    approved = "approved"
    sent = "sent"
    rejected = "rejected"
    failed = "failed"


class TemplateKind(StrEnum):
    answer = "answer"  # application question
    linkedin_message = "linkedin_message"  # connection note / InMail
    email = "email"


class ConnectionStatus(StrEnum):
    connected = "connected"
    disconnected = "disconnected"
    connecting = "connecting"  # a login window is open
    error = "error"  # the check itself failed


class ScanKind(StrEnum):
    # Contract order: also the order the scanners run in and the order of /scans/status "latest".
    # LinkedIn first (decided 2026-10-14), then the senders, then Naukri.
    linkedin_jobs = "linkedin_jobs"
    linkedin_apply = "linkedin_apply"
    linkedin_posts = "linkedin_posts"
    email_send = "email_send"
    linkedin_outreach = "linkedin_outreach"
    naukri_jobs = "naukri_jobs"
    naukri_apply = "naukri_apply"


class ScanStatus(StrEnum):
    running = "running"
    ok = "ok"
    skipped = "skipped"
    failed = "failed"


# Answers that mean "not answered yet".
UNANSWERED_VALUES = ("", "TBD")


class UTCDateTime(TypeDecorator):
    """Stores datetimes as naive UTC and always loads them back as aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return to_utc(value).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return to_utc(value)


class Job(SQLModel, table=True):
    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_job_platform_external_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    platform: Platform = Field(index=True)
    external_id: str
    title: str
    company: str
    location: str = ""
    url: str
    apply_url: str | None = None
    form_url: str | None = None
    contact_email: str | None = None
    post_url: str | None = None
    short_description: str = ""
    apply_type: ApplyType = Field(index=True)
    status: JobStatus = Field(default=JobStatus.found, index=True)
    status_reason: str | None = None
    found_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime, index=True)
    applied_at: datetime | None = Field(default=None, sa_type=UTCDateTime, index=True)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    # Filled by the scanners; null for older rows. These columns are added to existing databases
    # by app.db.add_missing_columns, so new Job columns must stay nullable.
    posted_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    experience: str | None = None
    matched_role: str | None = None
    priority: int | None = None
    # The person to send a LinkedIn connection note to: the author of the hiring post, or someone
    # from "Meet the hiring team" on the LinkedIn job page. Not part of the API's Job shape.
    contact_name: str | None = None
    contact_profile_url: str | None = None
    # When the job page was checked for "Meet the hiring team" (found or not), so it isn't reopened.
    contact_checked_at: datetime | None = Field(default=None, sa_type=UTCDateTime)


class OutreachMessage(SQLModel, table=True):
    __tablename__ = "outreach_message"

    id: int | None = Field(default=None, primary_key=True)
    kind: OutreachKind = Field(index=True)
    job_id: int | None = Field(default=None, foreign_key="job.id", ondelete="SET NULL", index=True)
    recipient_name: str
    recipient_profile_url: str | None = None
    recipient_email: str | None = None  # used only when kind is email
    subject: str | None = None  # used only when kind is email
    body: str
    status: OutreachStatus = Field(default=OutreachStatus.draft, index=True)
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime, index=True)
    approved_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    sent_at: datetime | None = Field(default=None, sa_type=UTCDateTime, index=True)


class AppSetting(SQLModel, table=True):
    """A switch the user can flip in the app (see app/services/app_settings.py)."""

    __tablename__ = "app_setting"

    key: str = Field(primary_key=True)
    value: str


class UnansweredQuestion(SQLModel, table=True):
    __tablename__ = "unanswered_question"

    id: int | None = Field(default=None, primary_key=True)
    question: str
    platform: Platform
    job_id: int | None = Field(default=None, foreign_key="job.id", ondelete="SET NULL", index=True)
    seen_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    answered_at: datetime | None = Field(default=None, sa_type=UTCDateTime, index=True)


class Template(SQLModel, table=True):
    """Template Library entry: an application answer, a LinkedIn message or an email."""

    __tablename__ = "template"

    id: int | None = Field(default=None, primary_key=True)
    kind: TemplateKind = Field(default=TemplateKind.answer, index=True)
    # The application question, or a short name for messages and emails.
    question: str
    # The answer text or message body; line breaks are kept.
    answer: str = ""
    # Used by emails and InMail-style LinkedIn messages; None otherwise.
    subject: str | None = None
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)

    @property
    def is_answered(self) -> bool:
        return self.answer.strip().upper() not in UNANSWERED_VALUES


class ProfileField(SQLModel, table=True):
    """One detail about the candidate. ``key`` is the stable name code uses to look it up."""

    __tablename__ = "profile_field"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True)
    label: str
    section: str
    value: str = ""
    position: int = Field(default=0, index=True)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class Connection(SQLModel, table=True):
    """The login state of one platform. There is always exactly one row per platform."""

    __tablename__ = "connection"

    platform: Platform = Field(primary_key=True)
    status: ConnectionStatus = ConnectionStatus.disconnected
    account_name: str | None = None
    checked_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    message: str | None = None


class ScanRun(SQLModel, table=True):
    """One run of one scanner."""

    __tablename__ = "scan_run"

    id: int | None = Field(default=None, primary_key=True)
    kind: ScanKind = Field(index=True)
    started_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime, index=True)
    finished_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    status: ScanStatus = ScanStatus.running
    found: int = 0
    new: int = 0
    message: str | None = None


class ConnectionNoteTooLong(ValueError):
    def __init__(self, length: int):
        super().__init__(
            f"Connection notes are limited to {CONNECTION_NOTE_MAX_CHARS} characters "
            f"(got {length})."
        )
        self.length = length


def check_connection_note(kind: OutreachKind | str, body: str | None) -> None:
    """Raise ConnectionNoteTooLong if a connection-note body exceeds the LinkedIn limit."""
    if kind == OutreachKind.connection_note and body is not None:
        if len(body) > CONNECTION_NOTE_MAX_CHARS:
            raise ConnectionNoteTooLong(len(body))


@event.listens_for(OutreachMessage, "before_insert")
@event.listens_for(OutreachMessage, "before_update")
def _guard_connection_note(mapper, connection, target: OutreachMessage) -> None:
    # Last line of defence for code paths that create messages without going through the API
    # (e.g. the LinkedIn outreach source in later phases).
    check_connection_note(target.kind, target.body)


@event.listens_for(Job, "before_update")
@event.listens_for(Template, "before_update")
@event.listens_for(ProfileField, "before_update")
def _touch_updated_at(mapper, connection, target: Job | Template | ProfileField) -> None:
    target.updated_at = utcnow()
