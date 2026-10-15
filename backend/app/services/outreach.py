"""What automatic emails and LinkedIn connection notes share (docs/API_CONTRACT.md, "Automatic
email and LinkedIn outreach"): filling a template's placeholders, choosing the template, and
counting what was already sent.

Placeholders (case-insensitive):

- ``[Job title]``: the job title without " (hiring post)".
- ``[Company]``: the company, when known; otherwise "your company". The hiring-post scanner falls
  back to the author's name or "LinkedIn post" when a post doesn't say the company, so those count
  as unknown.
- ``[Name]``: the person's first name, when known; otherwise "Hi [Name]" becomes "Hi there" (and
  "Dear [Name]" "Dear Hiring Team").

Any other bracketed word left after filling (``[Role]``, ``[Link]``...) means the text isn't ready
to send automatically: ``DraftProblem`` is raised and nothing is sent.
"""

import re
from datetime import datetime

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models import (
    ApplyType,
    Job,
    JobStatus,
    OutreachKind,
    OutreachMessage,
    OutreachStatus,
    Template,
    TemplateKind,
)
from app.timeutils import local_day_bounds, local_month_bounds

HIRING_POST_SUFFIX = " (hiring post)"
UNKNOWN_COMPANY = "your company"
_UNKNOWN_COMPANIES = {"", "linkedin post", "unknown company", "confidential"}
_PLACEHOLDER = re.compile(r"\[[A-Za-z][A-Za-z .'/-]{0,40}\]")
_JOB_TITLE = re.compile(r"\[\s*job\s*title\s*\]", re.I)
_COMPANY = re.compile(r"\[\s*company\s*\]", re.I)
_NAME = re.compile(r"\[\s*name\s*\]", re.I)
_GREETING_NAME = re.compile(r"\b(hi|hello|hey|dear)(\s+)\[\s*name\s*\]", re.I)
_EASY_APPLY = re.compile(r"\beasy[\s-]*apply\b", re.I)
# Templates written for a particular group (e.g. "Alumni ask (DA-IICT senior)": "Hi bhaiya, ...")
# are never sent automatically: the tool can't tell who belongs to that group.
_GROUP_TEMPLATE = re.compile(r"\balumni\b|\bbhaiya\b|\bdidi\b", re.I)
_HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "er", "ca", "adv", "prof", "shri", "smt", "sir"}


class DraftProblem(ValueError):
    """The template can't be turned into a message that is ready to send."""


def clean_job_title(title: str | None) -> str:
    """"Backend Developer (hiring post)" -> "Backend Developer"."""
    title = " ".join((title or "").split())
    if title.lower().endswith(HIRING_POST_SUFFIX):
        title = title[: -len(HIRING_POST_SUFFIX)].rstrip()
    return title


def first_name(name: str | None) -> str | None:
    """"Dr. Priya Sharma, PHR" -> "Priya"; "PRIYA SHARMA" -> "Priya". None when there is no name."""
    text = re.sub(r"\(.*?\)", " ", name or "")  # "(She/Her)"
    text = text.split(",")[0]  # ", PHR"
    for token in text.split():
        word = re.sub(r"[^\w'-]", "", token).strip("'-_")
        if not word or word.lower().rstrip(".") in _HONORIFICS or any(ch.isdigit() for ch in word):
            continue
        if len(word) == 1:  # an initial ("A. Kumar")
            continue
        return word.capitalize() if word.isupper() or word.islower() else word
    return None


def known_company(company: str | None, contact_name: str | None = None) -> str | None:
    """The company, or None when the job doesn't really know it."""
    text = " ".join((company or "").split())
    if text.lower() in _UNKNOWN_COMPANIES:
        return None
    if contact_name and text.lower() == " ".join(contact_name.split()).lower():
        return None  # the post scanner used the author's name
    return text


def leftover_placeholders(text: str) -> list[str]:
    found: list[str] = []
    for match in _PLACEHOLDER.finditer(text or ""):
        if match.group() not in found:
            found.append(match.group())
    return found


def fill_placeholders(text: str, *, job_title: str | None = None, company: str | None = None, name: str | None = None) -> str:
    """Fill ``[Job title]``, ``[Company]`` and ``[Name]`` (see the module docstring). ``name`` is the
    first name."""
    text = text or ""
    if job_title:
        text = _JOB_TITLE.sub(lambda _: job_title, text)
    text = _COMPANY.sub(lambda _: company or UNKNOWN_COMPANY, text)
    if name:
        text = _NAME.sub(lambda _: name, text)
    else:
        text = _GREETING_NAME.sub(
            lambda m: f"{m.group(1)}{m.group(2)}{'Hiring Team' if m.group(1).lower() == 'dear' else 'there'}", text
        )
    return text


def ready_text(text: str, what: str) -> str:
    """Raise ``DraftProblem`` when placeholders are left in ``text``."""
    leftover = leftover_placeholders(text)
    if leftover:
        raise DraftProblem(f"The {what} still has {', '.join(leftover)} after filling it in")
    return text


# ---------------------------------------------------------------------------
# Templates


def email_template(session: Session) -> Template | None:
    """The first non-empty ``email`` template (lowest id)."""
    rows = session.exec(select(Template).where(Template.kind == TemplateKind.email).order_by(col(Template.id))).all()
    return next((t for t in rows if t.is_answered), None)


def note_templates(session: Session) -> list[Template]:
    """``linkedin_message`` templates usable as connection notes, lowest id first: not empty, no
    subject (those are InMails, never sent automatically), and not written for a particular group."""
    rows = session.exec(
        select(Template).where(Template.kind == TemplateKind.linkedin_message).order_by(col(Template.id))
    ).all()
    return [
        t for t in rows
        if t.is_answered and not (t.subject or "").strip() and not _GROUP_TEMPLATE.search(f"{t.question} {t.answer}")
    ]


def mentions_easy_apply(template: Template) -> bool:
    return _EASY_APPLY.search(f"{template.question} {template.answer}") is not None


def applied_with_easy_apply(job: Job | None) -> bool:
    return job is not None and job.apply_type == ApplyType.easy_apply and job.status == JobStatus.applied


def choose_note_template(templates: list[Template], job: Job | None) -> Template | None:
    """The note for one person. Templates mentioning Easy Apply are used only (and first) for jobs
    applied to with Easy Apply; otherwise the first template that greets the person by ``[Name]``,
    else the first one."""
    easy = [t for t in templates if mentions_easy_apply(t)]
    if applied_with_easy_apply(job) and easy:
        return easy[0]
    others = [t for t in templates if t not in easy]
    personal = [t for t in others if _NAME.search(t.answer)]
    return (personal or others or [None])[0]


# ---------------------------------------------------------------------------
# What was sent


def sent_between(session: Session, kind: OutreachKind, start: datetime, end: datetime) -> int:
    return session.exec(
        select(func.count())
        .select_from(OutreachMessage)
        .where(
            OutreachMessage.kind == kind,
            OutreachMessage.status == OutreachStatus.sent,
            col(OutreachMessage.sent_at) >= start,
            col(OutreachMessage.sent_at) < end,
        )
    ).one()


def sent_today(session: Session, kind: OutreachKind, now: datetime | None = None) -> int:
    return sent_between(session, kind, *local_day_bounds(now))


def sent_this_month(session: Session, kind: OutreachKind, now: datetime | None = None) -> int:
    return sent_between(session, kind, *local_month_bounds(now))


def last_emailed(session: Session, address: str, since: datetime) -> datetime | None:
    """When ``address`` was last sent an email, if that was after ``since``."""
    return session.exec(
        select(func.max(OutreachMessage.sent_at)).where(
            OutreachMessage.kind == OutreachKind.email,
            OutreachMessage.status == OutreachStatus.sent,
            func.lower(OutreachMessage.recipient_email) == address.strip().lower(),
            col(OutreachMessage.sent_at) >= since,
        )
    ).one()
