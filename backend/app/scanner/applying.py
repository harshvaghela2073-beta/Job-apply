"""What every auto-apply source shares: the result of one attempt and how it is stored on the job.

Used by Naukri's one-click apply (``app/sources/naukri/apply.py``) and LinkedIn Easy Apply
(``app/sources/linkedin/easy_apply.py``). The result values are part of the API contract
(``POST /api/jobs/{id}/apply``).
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from sqlmodel import Session, col, func, select

from app.models import ApplyType, Job, JobStatus, Platform, UnansweredQuestion
from app.timeutils import local_day_bounds, utcnow


class ApplyResult(StrEnum):
    applied = "applied"
    already_applied = "already_applied"
    # The form asks questions the tool can't read or fill. A question without a saved answer makes
    # the job ``skipped`` instead (2026-10-21), with the question in ``ApplyAttempt.questions``.
    questionnaire = "questionnaire"
    company_site = "company_site"
    quota_reached = "quota_reached"
    no_button = "no_button"
    # No confirmation after applying, or (LinkedIn) the form couldn't be completed.
    no_confirmation = "no_confirmation"
    would_apply = "would_apply"  # dry run: everything up to the final click worked; nothing was sent
    skipped = "skipped"  # one of the candidate's skip rules matched, or a question has no saved answer


def no_saved_answer(questions: list[str]) -> str:
    """The skip reason when a form asks questions nobody saved an answer for (the candidate's choice on
    2026-10-21: skip the job rather than leave it waiting)."""
    quoted = [f'"{q}"' for q in questions]
    asks = quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " and " + quoted[-1]
    return f"The form asks {asks}, which {'has' if len(quoted) == 1 else 'have'} no saved answer."


@dataclass
class ApplyAttempt:
    result: ApplyResult
    detail: str
    questions: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)  # what the job page (and form) showed
    # Whether the platform's apply button was clicked (pauses between applications follow clicks).
    clicked: bool = False


@dataclass(frozen=True)
class StoredJob:
    """What the search stored about a job, used when the page doesn't show something."""

    url: str
    company: str = ""
    text: str = ""  # short_description
    experience: str = ""
    title: str = ""


def add_unanswered_questions(session: Session, questions: list[str], platform: Platform, job_id: int | None) -> None:
    """Add each question to the unanswered questions, unless it is already waiting there."""
    for question in questions:
        exists = session.exec(
            select(UnansweredQuestion).where(
                UnansweredQuestion.question == question, col(UnansweredQuestion.answered_at).is_(None)
            )
        ).first()
        if exists is None:
            session.add(UnansweredQuestion(question=question, platform=platform, job_id=job_id))


def record_attempt(session: Session, job: Job, attempt: ApplyAttempt) -> None:
    """Store an attempt's outcome on the job (and its unanswered questions, if any)."""
    now = utcnow()
    if attempt.result in (ApplyResult.applied, ApplyResult.already_applied):
        job.status = JobStatus.applied
        job.applied_at = now
        job.status_reason = attempt.detail
    elif attempt.result == ApplyResult.skipped:
        job.status = JobStatus.skipped
        job.status_reason = f"Not auto-applied: {attempt.detail}"[:1000]
        # Skipped for a question without a saved answer: answering it lets later jobs through.
        add_unanswered_questions(session, attempt.questions, Platform(job.platform), job.id)
    elif attempt.result == ApplyResult.company_site:
        job.apply_type = ApplyType.company_site
        job.status_reason = attempt.detail
        if attempt.facts and attempt.facts.get("apply_url"):
            job.apply_url = attempt.facts["apply_url"]  # the company's own apply page (2026-10-15)
    elif attempt.result in (ApplyResult.questionnaire, ApplyResult.no_button, ApplyResult.no_confirmation):
        job.status = JobStatus.needs_manual
        detail = attempt.detail
        if attempt.questions:
            detail += " Questions: " + "; ".join(attempt.questions)
        job.status_reason = detail[:1000]
        add_unanswered_questions(session, attempt.questions, Platform(job.platform), job.id)
    else:  # quota_reached / would_apply: leave the job as it is
        return
    job.updated_at = now
    session.add(job)


def applied_today(session: Session, now: datetime, platform: Platform, apply_type: ApplyType) -> int:
    start, end = local_day_bounds(now)
    return session.exec(
        select(func.count())
        .select_from(Job)
        .where(
            Job.platform == platform,
            Job.apply_type == apply_type,
            Job.status == JobStatus.applied,
            col(Job.applied_at) >= start,
            col(Job.applied_at) < end,
        )
    ).one()


def pending_jobs(session: Session, now: datetime, limit: int, platform: Platform, apply_type: ApplyType) -> list[Job]:
    """Today's jobs of this apply type with status ``found``, most important first."""
    start, _ = local_day_bounds(now)
    return list(
        session.exec(
            select(Job)
            .where(
                Job.platform == platform,
                Job.apply_type == apply_type,
                Job.status == JobStatus.found,
                col(Job.found_at) >= start,
            )
            .order_by(col(Job.priority).asc().nulls_last(), col(Job.found_at).desc())
            .limit(limit)
        ).all()
    )
