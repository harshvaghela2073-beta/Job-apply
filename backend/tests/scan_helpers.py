"""Helpers for scanner tests."""

import threading
from datetime import UTC, datetime, timedelta

from app.scanner.base import ScanContext
from app.scanner.matching import SearchPrefs
from app.timeutils import local_day_bounds

ROLES = (
    "Software Development Engineer, SDE1, SDE2, Java Developer, Backend Developer, Golang Developer, "
    "Full Stack Developer, Forward Deployed Engineer"
)
PREFS = SearchPrefs.from_values("1+ years, 2+ years, 3+ years", ROLES, "Bengaluru, Remote")
# The candidate's skills and the skip list approved on 2026-10-15, for the job-fit rules.
SKILLS = "Java, Spring Boot, Golang, Python, SQL, Redis, Apache Kafka, Cassandra, Jenkins, Docker, Grafana, GKE, Kubernetes"
SKIP_COMPANIES = "Jobgether, Crossing Hurdles, Scoutit, Applicantz, CodeRound AI"
FIT_PREFS = SearchPrefs.from_values(
    "1+ years, 2+ years, 3+ years", ROLES, "Bengaluru, Remote", skills=SKILLS, skip_companies=SKIP_COMPANIES
)
SEARCH_FIELDS = [
    {"key": "search_experience_order", "label": "Experience", "section": "Job search",
     "value": "1+ years, 2+ years, 3+ years"},
    {"key": "search_roles", "label": "Roles", "section": "Job search", "value": ROLES},
    {"key": "search_locations", "label": "Locations", "section": "Job search", "value": "Bengaluru, Remote"},
]


def ten_am_today() -> datetime:
    midnight, _ = local_day_bounds(datetime.now(UTC))
    return midnight + timedelta(hours=10)


def make_ctx(
    app, now: datetime | None = None, *, max_loads: int = 20, last_ok=None, prefs: SearchPrefs = PREFS
) -> ScanContext:
    settings = app.state.settings.model_copy(update={"scan_max_page_loads": max_loads})
    return ScanContext(
        engine=app.state.engine,
        settings=settings,
        prefs=prefs,
        now=now or ten_am_today(),
        browsers=app.state.browsers,
        connections=app.state.connections,
        stop=threading.Event(),
        last_ok_started_at=last_ok,
        wait_range=(0.0, 0.0),
    )


def epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def connect_linkedin(app, fake_browser) -> None:
    """Mark LinkedIn connected, with a saved profile and an li_at cookie in the fake browser."""
    from sqlmodel import Session

    from app.models import Connection, ConnectionStatus, Platform

    app.state.browsers.profile_dir(Platform.linkedin).mkdir(parents=True, exist_ok=True)
    fake_browser.cookies = [
        {"name": "li_at", "value": "t", "domain": ".linkedin.com"},
        {"name": "JSESSIONID", "value": '"ajax:1"', "domain": ".www.linkedin.com"},
    ]
    with Session(app.state.engine) as session:
        row = session.get(Connection, Platform.linkedin)
        row.status = ConnectionStatus.connected
        session.add(row)
        session.commit()

