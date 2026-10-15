"""Scan runs and their schedule (docs/API_CONTRACT.md, "Scans" and "Separate LinkedIn and Naukri
scans": a LinkedIn and a Naukri group, each with its own schedule, "Run now" and status)."""

from fastapi import APIRouter, HTTPException, Request
from sqlmodel import col, select

from app.api.deps import PaginationDep, SessionDep, SettingsDep, paginate
from app.models import Platform, ScanRun
from app.schemas import Page, ScanGroupsRead, ScanGroupStatusRead, ScanRunRead, ScanStarted, ScanStatusRead

router = APIRouter(prefix="/scans", tags=["scans"])

NAMES = {Platform.linkedin: "LinkedIn", Platform.naukri: "Naukri"}


@router.get("/status", response_model=ScanStatusRead)
def scan_status(request: Request, settings: SettingsDep) -> ScanStatusRead:
    scans = request.app.state.scans
    scheduler = request.app.state.scheduler
    groups = {
        platform: ScanGroupStatusRead(
            running=scans.running_for(platform),
            interval_minutes=scheduler.interval_minutes(platform),  # as set in the app
            next_run_at=scheduler.next_run_at(platform),
            last_finished_at=scans.last_finished_at(platform),
        )
        for platform in Platform
    }
    return ScanStatusRead(
        enabled=settings.scan_enabled,
        running=scans.running,
        next_run_at=scheduler.next_run_at(),
        interval_minutes=groups[Platform.linkedin].interval_minutes,
        platforms=ScanGroupsRead(**{platform.value: group for platform, group in groups.items()}),
        latest=[ScanRunRead.model_validate(run) for run in scans.latest()],
    )


@router.post("/run", response_model=ScanStarted, status_code=202)
def run_now(request: Request, platform: Platform | None = None) -> ScanStarted:
    """Start that group now, or without ``platform`` every group that isn't running (also when the
    schedule is disabled). LinkedIn's hourly searches run too when their last search was at least 15
    minutes ago. 409 when the requested group (without ``platform``: every group) is already running."""
    started = request.app.state.scans.start(platform, manual=True)
    if not started:
        detail = "Both scans are already running." if platform is None else f"The {NAMES[platform]} scan is already running."
        raise HTTPException(status_code=409, detail=detail)
    return ScanStarted(started=True, platforms=started)


@router.get("", response_model=Page[ScanRunRead])
def list_scans(session: SessionDep, page: PaginationDep) -> Page[ScanRunRead]:
    items, total = paginate(
        session, select(ScanRun), page, col(ScanRun.started_at).desc(), col(ScanRun.id).desc()
    )
    return Page(items=[ScanRunRead.model_validate(run) for run in items], total=total)
