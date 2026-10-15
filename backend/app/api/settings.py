"""App switches (auto-apply, automatic outreach, pausing a platform or one feature) and scan intervals."""

from fastapi import APIRouter, Request

from app.api.deps import SessionDep, SettingsDep
from app.schemas import AppSettingsRead, AppSettingsUpdate
from app.services.app_settings import (
    AUTO_APPLY_LINKEDIN,
    AUTO_APPLY_NAUKRI,
    AUTO_SEND_EMAIL,
    AUTO_SEND_LINKEDIN_NOTES,
    PAUSE_LINKEDIN,
    PAUSE_LINKEDIN_JOBS,
    PAUSE_LINKEDIN_POSTS,
    PAUSE_NAUKRI,
    PAUSE_NAUKRI_JOBS,
    LINKEDIN_SEARCH_INTERVAL,
    RUN_INTERVALS,
    get_flag,
    get_minutes,
    run_interval,
    set_flag,
    set_minutes,
)

router = APIRouter(prefix="/settings", tags=["settings"])

FLAGS = (
    AUTO_APPLY_NAUKRI,
    AUTO_APPLY_LINKEDIN,
    AUTO_SEND_EMAIL,
    AUTO_SEND_LINKEDIN_NOTES,
    PAUSE_NAUKRI,
    PAUSE_LINKEDIN,
    PAUSE_LINKEDIN_JOBS,
    PAUSE_LINKEDIN_POSTS,
    PAUSE_NAUKRI_JOBS,
)


def _read(session, settings) -> AppSettingsRead:
    return AppSettingsRead(
        **{key: get_flag(session, key) for key in FLAGS},
        **{key: run_interval(session, platform, settings.scan_interval_minutes) for platform, key in RUN_INTERVALS.items()},
        linkedin_search_interval_minutes=get_minutes(
            session, LINKEDIN_SEARCH_INTERVAL, settings.linkedin_search_interval_minutes
        ),
    )


@router.get("", response_model=AppSettingsRead)
def get_settings(session: SessionDep, settings: SettingsDep) -> AppSettingsRead:
    return _read(session, settings)


@router.put("", response_model=AppSettingsRead)
def update_settings(body: AppSettingsUpdate, session: SessionDep, settings: SettingsDep, request: Request) -> AppSettingsRead:
    for key in FLAGS:
        value = getattr(body, key)
        if value is not None:
            set_flag(session, key, value)
    if body.linkedin_search_interval_minutes is not None:
        set_minutes(session, LINKEDIN_SEARCH_INTERVAL, body.linkedin_search_interval_minutes)
    for platform, key in RUN_INTERVALS.items():
        minutes = getattr(body, key)
        if minutes is not None:
            set_minutes(session, key, minutes)
            request.app.state.scheduler.set_interval(platform, minutes)  # that group's next run moves now
    return _read(session, settings)
