"""The Analysis tab: jobs found, applied and pending per platform and apply type, today and all time."""

from fastapi import APIRouter
from sqlalchemy import and_, case, func
from sqlmodel import Session, col, select

from app.api.deps import SessionDep
from app.models import ApplyType, Job, JobStatus, OutreachKind, OutreachMessage, OutreachStatus, Platform
from app.schemas import Analytics, AnalyticsPeriod, ApplyTypeCounts, LinkedInAnalytics, NaukriAnalytics
from app.timeutils import local_day_bounds

router = APIRouter(tags=["analytics"])

# (found, applied) per (platform, apply type).
Counts = dict[tuple[Platform, ApplyType], tuple[int, int]]


def _job_counts(session: Session, day_start, day_end) -> tuple[Counts, Counts]:
    """One GROUP BY over the jobs: (today, all time). "Today" is the jobs found today."""
    is_applied = Job.status == JobStatus.applied
    found_today = and_(col(Job.found_at) >= day_start, col(Job.found_at) < day_end)
    rows = session.exec(
        select(
            Job.platform,
            Job.apply_type,
            func.count(),
            func.sum(case((is_applied, 1), else_=0)),
            func.sum(case((found_today, 1), else_=0)),
            func.sum(case((and_(found_today, is_applied), 1), else_=0)),
        )
        # Removed by the candidate or skipped by the rules: not "found" (2026-10-21).
        .where(col(Job.status).not_in([JobStatus.skipped, JobStatus.failed]))
        .group_by(Job.platform, Job.apply_type)
    ).all()
    today: Counts = {}
    all_time: Counts = {}
    for platform, apply_type, found, applied, found_now, applied_now in rows:
        key = (Platform(platform), ApplyType(apply_type))
        all_time[key] = (found, applied or 0)
        today[key] = (found_now or 0, applied_now or 0)
    return today, all_time


def _notes_sent(session: Session, day_start, day_end) -> tuple[int, int]:
    """Connection notes sent: (today, all time)."""
    sent_today = and_(col(OutreachMessage.sent_at) >= day_start, col(OutreachMessage.sent_at) < day_end)
    total, today = session.exec(
        select(func.count(), func.coalesce(func.sum(case((sent_today, 1), else_=0)), 0)).where(
            OutreachMessage.kind == OutreachKind.connection_note,
            OutreachMessage.status == OutreachStatus.sent,
        )
    ).one()
    return today, total


def _period(counts: Counts, notes_sent: int) -> AnalyticsPeriod:
    def bucket(platform: Platform, apply_type: ApplyType) -> ApplyTypeCounts:
        found, applied = counts.get((platform, apply_type), (0, 0))
        return ApplyTypeCounts(found=found, applied=applied, pending=found - applied)

    li, nk = Platform.linkedin, Platform.naukri
    return AnalyticsPeriod(
        linkedin=LinkedInAnalytics(
            easy_apply=bucket(li, ApplyType.easy_apply),
            company_site=bucket(li, ApplyType.company_site),
            google_form=bucket(li, ApplyType.google_form),
            email=bucket(li, ApplyType.email),
            connection_notes_sent=notes_sent,
        ),
        naukri=NaukriAnalytics(
            one_click=bucket(nk, ApplyType.one_click),
            company_site=bucket(nk, ApplyType.company_site),
        ),
    )


@router.get("/analytics", response_model=Analytics)
def analytics(session: SessionDep) -> Analytics:
    day_start, day_end = local_day_bounds()
    jobs_today, jobs_all = _job_counts(session, day_start, day_end)
    notes_today, notes_all = _notes_sent(session, day_start, day_end)
    return Analytics(today=_period(jobs_today, notes_today), all=_period(jobs_all, notes_all))
