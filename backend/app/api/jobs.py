"""Jobs found on LinkedIn and Naukri."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func
from sqlmodel import col, select

from app.api.deps import PaginationDep, SessionDep, paginate
from app.models import ApplyType, Job, JobStatus, Platform
from app.schemas import JobPatch, JobRead, Page
from app.timeutils import to_utc, utcnow

router = APIRouter(prefix="/jobs", tags=["jobs"])

SORT_HELP = (
    "posted: newest posting first (a job without posted_at counts by its found_at); "
    "found: newest found first. Ties by id, newest first."
)


@router.get("", response_model=Page[JobRead])
def list_jobs(
    session: SessionDep,
    page: PaginationDep,
    platform: Platform | None = None,
    apply_type: ApplyType | None = None,
    status: Annotated[list[JobStatus] | None, Query()] = None,
    found_after: Annotated[
        datetime | None, Query(description="Only jobs found at or after this time (ISO 8601).")
    ] = None,
    sort: Annotated[Literal["posted", "found"], Query(description=SORT_HELP)] = "posted",
) -> Page[JobRead]:
    stmt = select(Job)
    if found_after is not None:
        stmt = stmt.where(col(Job.found_at) >= to_utc(found_after))
    if platform is not None:
        stmt = stmt.where(Job.platform == platform)
    if apply_type is not None:
        stmt = stmt.where(Job.apply_type == apply_type)
    if status:
        stmt = stmt.where(col(Job.status).in_(status))
    newest = col(Job.found_at) if sort == "found" else func.coalesce(col(Job.posted_at), col(Job.found_at))
    items, total = paginate(session, stmt, page, newest.desc(), col(Job.id).desc())
    return Page(items=[JobRead.model_validate(job) for job in items], total=total)


def _get_job(session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: int, session: SessionDep) -> JobRead:
    return JobRead.model_validate(_get_job(session, job_id))


@router.patch("/{job_id}", response_model=JobRead)
def update_job(job_id: int, body: JobPatch, session: SessionDep) -> JobRead:
    job = _get_job(session, job_id)
    new_status = JobStatus(body.status)
    now = utcnow()
    if new_status == JobStatus.applied:
        # Keep the original timestamp if the job was already applied (e.g. a double click).
        if job.status != JobStatus.applied or job.applied_at is None:
            job.applied_at = now
    else:
        job.applied_at = None
    job.status = new_status
    # The reason describes the new status; an omitted reason clears the old one. Back to Pending
    # ("found") starts over without one, so the next scan tries an Easy Apply or one-click job again.
    job.status_reason = None if new_status == JobStatus.found else body.status_reason
    job.updated_at = now
    session.add(job)
    session.commit()
    session.refresh(job)
    return JobRead.model_validate(job)
