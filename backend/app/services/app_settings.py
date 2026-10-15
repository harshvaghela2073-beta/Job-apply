"""Switches and scan intervals the user can change in the app, stored in the ``app_setting`` table."""

from sqlmodel import Session

from app.models import AppSetting, Platform

# Apply automatically to Naukri one-click jobs after each scan (off until the user turns it on).
AUTO_APPLY_NAUKRI = "auto_apply_naukri"
# LinkedIn Easy Apply after each LinkedIn search (off until the user turns it on, after a one-job test).
AUTO_APPLY_LINKEDIN = "auto_apply_linkedin"

# Pause everything on Naukri (search and auto-apply), e.g. while LinkedIn is being fixed (2026-10-14).
PAUSE_NAUKRI = "pause_naukri"
# Per-feature pause switches (2026-10-15); app.scheduler.runner.PAUSES maps them to the scanners.
PAUSE_LINKEDIN = "pause_linkedin"  # every LinkedIn scanner, the email sender included
PAUSE_LINKEDIN_JOBS = "pause_linkedin_jobs"  # the LinkedIn job search
PAUSE_LINKEDIN_POSTS = "pause_linkedin_posts"  # hiring posts with a Google Form or email
PAUSE_NAUKRI_JOBS = "pause_naukri_jobs"  # the Naukri search

# Email the hiring posts that ask for one, with the resume (off until a checked test send).
AUTO_SEND_EMAIL = "auto_send_email"
# LinkedIn connection notes to the hiring team and post authors (off until a checked test send).
AUTO_SEND_LINKEDIN_NOTES = "auto_send_linkedin_notes"

DEFAULTS = {
    AUTO_APPLY_NAUKRI: False,
    AUTO_APPLY_LINKEDIN: False,
    AUTO_SEND_EMAIL: False,
    AUTO_SEND_LINKEDIN_NOTES: False,
    PAUSE_NAUKRI: False,
    PAUSE_LINKEDIN: False,
    PAUSE_LINKEDIN_JOBS: False,
    PAUSE_LINKEDIN_POSTS: False,
    PAUSE_NAUKRI_JOBS: False,
}


# Minutes between scans, editable in the app. Since 2026-10-15 LinkedIn and Naukri are scanned
# separately, each group on its own interval (docs/API_CONTRACT.md, "Separate LinkedIn and Naukri
# scans"). The .env values are the defaults until the user sets one: SCAN_INTERVAL_MINUTES for both
# run intervals, LINKEDIN_SEARCH_INTERVAL_MINUTES for the LinkedIn searches.
# Note: before 2026-10-15 the key "linkedin_scan_interval_minutes" meant the LinkedIn search interval
# (now LINKEDIN_SEARCH_INTERVAL) and "scan_interval_minutes" every run; no rows had been saved under
# either, so the reused key carries only its new meaning and the old run key is no longer read.
LINKEDIN_SCAN_INTERVAL = "linkedin_scan_interval_minutes"  # LinkedIn scan runs
NAUKRI_SCAN_INTERVAL = "naukri_scan_interval_minutes"  # Naukri scan runs
LINKEDIN_SEARCH_INTERVAL = "linkedin_search_interval_minutes"  # LinkedIn's job and post searches
# The run interval of each scan group.
RUN_INTERVALS: dict[Platform, str] = {
    Platform.linkedin: LINKEDIN_SCAN_INTERVAL,
    Platform.naukri: NAUKRI_SCAN_INTERVAL,
}
# What the app accepts. LinkedIn searches no more often than every 30 minutes (account safety).
MINUTES_RANGE = {
    LINKEDIN_SCAN_INTERVAL: (10, 240),
    NAUKRI_SCAN_INTERVAL: (10, 240),
    LINKEDIN_SEARCH_INTERVAL: (30, 720),
}


def get_minutes(session: Session, key: str, default: int) -> int:
    row = session.get(AppSetting, key)
    if row is None:
        return default
    try:
        return int(row.value)
    except ValueError:
        return default


def run_interval(session: Session, platform: Platform, default: int) -> int:
    """Minutes between two runs of this platform's scan group (``default``: SCAN_INTERVAL_MINUTES)."""
    return get_minutes(session, RUN_INTERVALS[Platform(platform)], default)


def set_minutes(session: Session, key: str, value: int) -> None:
    row = session.get(AppSetting, key) or AppSetting(key=key, value="")
    row.value = str(int(value))
    session.add(row)
    session.commit()


def get_flag(session: Session, key: str) -> bool:
    row = session.get(AppSetting, key)
    return DEFAULTS.get(key, False) if row is None else row.value == "true"


def set_flag(session: Session, key: str, value: bool) -> None:
    row = session.get(AppSetting, key) or AppSetting(key=key, value="")
    row.value = "true" if value else "false"
    session.add(row)
    session.commit()
