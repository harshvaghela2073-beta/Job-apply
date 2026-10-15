"""Dashboard summary and the applications list."""

from fastapi import APIRouter
from sqlalchemy import func
from sqlmodel import Session, col, select

from app.api.deps import PaginationDep, SessionDep, SettingsDep, paginate
from app.models import (
    ApplyType,
    Job,
    JobStatus,
    OutreachKind,
    OutreachMessage,
    OutreachStatus,
    Platform,
    UnansweredQuestion,
)
from app.schemas import CapUsage, Caps, DashboardSummary, JobRead, Page
from app.timeutils import local_day_bounds, local_month_bounds

router = APIRouter(tags=["dashboard"])


def _count(session: Session, model, *where) -> int:
    return session.exec(select(func.count()).select_from(model).where(*where)).one()


def _applied_between(session: Session, start, end, *where) -> int:
    return _count(
        session,
        Job,
        Job.status == JobStatus.applied,
        col(Job.applied_at) >= start,
        col(Job.applied_at) < end,
        *where,
    )


def _sent_between(session: Session, kind: OutreachKind, start, end) -> int:
    return _count(
        session,
        OutreachMessage,
        OutreachMessage.kind == kind,
        OutreachMessage.status == OutreachStatus.sent,
        col(OutreachMessage.sent_at) >= start,
        col(OutreachMessage.sent_at) < end,
    )


@router.get("/dashboard/summary", response_model=DashboardSummary)
def dashboard_summary(session: SessionDep, settings: SettingsDep) -> DashboardSummary:
    day_start, day_end = local_day_bounds()
    month_start, month_end = local_month_bounds()

    by_platform = {p.value: 0 for p in Platform}
    for platform, count in session.exec(
        select(Job.platform, func.count()).where(Job.status == JobStatus.applied).group_by(Job.platform)
    ).all():
        by_platform[Platform(platform).value] = count

    needs_action = {t.value: 0 for t in ApplyType}
    for apply_type, count in session.exec(
        select(Job.apply_type, func.count())
        .where(col(Job.status).in_([JobStatus.needs_manual, JobStatus.found]))
        .group_by(Job.apply_type)
    ).all():
        needs_action[ApplyType(apply_type).value] = count

    caps = Caps(
        easy_apply_today=CapUsage(
            used=_applied_between(
                session,
                day_start,
                day_end,
                Job.platform == Platform.linkedin,
                Job.apply_type == ApplyType.easy_apply,
            ),
            limit=settings.easy_apply_daily_cap,
        ),
        naukri_apply_today=CapUsage(
            used=_applied_between(
                session,
                day_start,
                day_end,
                Job.platform == Platform.naukri,
                Job.apply_type == ApplyType.one_click,
            ),
            limit=settings.naukri_daily_cap,
        ),
        connection_notes_month=CapUsage(
            used=_sent_between(session, OutreachKind.connection_note, month_start, month_end),
            limit=settings.connection_notes_monthly_cap,
        ),
        inmail_month=CapUsage(
            used=_sent_between(session, OutreachKind.inmail, month_start, month_end),
            limit=settings.inmail_monthly_cap,
        ),
    )

    return DashboardSummary(
        applied_today=_applied_between(session, day_start, day_end),
        applied_total=_count(session, Job, Job.status == JobStatus.applied),
        applied_by_platform=by_platform,
        needs_action=needs_action,
        outreach_pending=_count(
            session, OutreachMessage, OutreachMessage.status == OutreachStatus.draft
        ),
        unanswered_questions=_count(
            session, UnansweredQuestion, col(UnansweredQuestion.answered_at).is_(None)
        ),
        caps=caps,
    )


@router.get("/applications", response_model=Page[JobRead])
def list_applications(
    session: SessionDep,
    page: PaginationDep,
    platform: Platform | None = None,
) -> Page[JobRead]:
    stmt = select(Job).where(Job.status == JobStatus.applied)
    if platform is not None:
        stmt = stmt.where(Job.platform == platform)
    items, total = paginate(
        session,
        stmt,
        page,
        col(Job.applied_at).desc().nulls_last(),
        col(Job.id).desc(),
    )
    return Page(items=[JobRead.model_validate(job) for job in items], total=total)
