"""Scan intervals set in the app (2026-10-15): stored in app_setting, validated, and used by each scan
group's schedule and by the LinkedIn searches ("Separate LinkedIn and Naukri scans")."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.config import Settings
from app.models import Platform, ScanKind, ScanRun, ScanStatus
from app.sources.linkedin.jobs import LinkedInJobsScanner
from app.timeutils import iso_utc, utcnow
from tests.conftest import make_app
from tests.fakes import page
from tests.scan_helpers import connect_linkedin, make_ctx
from tests.test_linkedin_search import NOW, add_run, results_html

KEYS = ("linkedin_scan_interval_minutes", "naukri_scan_interval_minutes", "linkedin_search_interval_minutes")


def intervals(body: dict) -> tuple[int, int, int]:
    return tuple(body[key] for key in KEYS)


def test_intervals_are_read_and_saved(client):
    body = client.get("/api/settings").json()
    assert intervals(body) == (30, 30, 60)
    assert "scan_interval_minutes" not in body  # replaced by the two run intervals
    body = client.put("/api/settings", json=dict(zip(KEYS, (45, 20, 90)))).json()
    assert intervals(body) == (45, 20, 90)
    assert intervals(client.get("/api/settings").json()) == (45, 20, 90)
    scheduler = client.app.state.scheduler
    assert (scheduler.interval(Platform.linkedin), scheduler.interval(Platform.naukri)) == (
        timedelta(minutes=45), timedelta(minutes=20),
    )
    body = client.put("/api/settings", json={"naukri_scan_interval_minutes": 240}).json()
    assert intervals(body) == (45, 240, 90)  # the others are unchanged
    status = client.get("/api/scans/status").json()
    assert status["interval_minutes"] == 45  # LinkedIn's, for older UIs
    assert (status["platforms"]["linkedin"]["interval_minutes"], status["platforms"]["naukri"]["interval_minutes"]) == (45, 240)


@pytest.mark.parametrize(
    "key, low, high",
    [("linkedin_scan_interval_minutes", 10, 240), ("naukri_scan_interval_minutes", 10, 240),
     ("linkedin_search_interval_minutes", 30, 720)],
)
def test_intervals_out_of_range_are_refused(client, key, low, high):
    for value in (low - 1, high + 1):
        assert client.put("/api/settings", json={key: value}).status_code == 422
    assert intervals(client.get("/api/settings").json()) == (30, 30, 60)
    for value in (low, high):
        assert client.put("/api/settings", json={key: value}).json()[key] == value


def env_settings(settings: Settings) -> Settings:
    """The test settings, read again with the environment."""
    return Settings(
        _env_file=None,
        database_url=settings.database_url,
        profile_path=settings.profile_path,
        resume_path=settings.resume_path,
        browser_profiles_dir=settings.browser_profiles_dir,
        chrome_user_data_dir=settings.chrome_user_data_dir,
        scan_enabled=False,
    )


def test_defaults_come_from_the_environment(settings, fake_browser, monkeypatch):
    monkeypatch.delenv("LINKEDIN_SEARCH_INTERVAL_MINUTES", raising=False)
    monkeypatch.setenv("SCAN_INTERVAL_MINUTES", "25")
    monkeypatch.setenv("LINKEDIN_SCAN_INTERVAL_MINUTES", "90")  # the old name of the search interval
    old_name = env_settings(settings)
    assert (old_name.scan_interval_minutes, old_name.linkedin_search_interval_minutes) == (25, 90)
    with TestClient(make_app(old_name, fake_browser)) as client:
        assert intervals(client.get("/api/settings").json()) == (25, 25, 90)
        status = client.get("/api/scans/status").json()
        assert [status["platforms"][p]["interval_minutes"] for p in ("linkedin", "naukri")] == [25, 25]

    monkeypatch.setenv("LINKEDIN_SEARCH_INTERVAL_MINUTES", "120")  # the new name wins
    assert env_settings(settings).linkedin_search_interval_minutes == 120
    monkeypatch.delenv("LINKEDIN_SCAN_INTERVAL_MINUTES")
    assert env_settings(settings).linkedin_search_interval_minutes == 120
    monkeypatch.delenv("LINKEDIN_SEARCH_INTERVAL_MINUTES")
    assert env_settings(settings).linkedin_search_interval_minutes == 60


def test_the_linkedin_search_uses_the_search_interval_set_in_the_app(client, fake_browser):
    engine = client.app.state.engine
    connect_linkedin(client.app, fake_browser)
    fake_browser.routes = [("linkedin.com/jobs/search", lambda url: page(url, results_html(no_results=True)))]
    add_run(engine, ScanStatus.ok, 40)  # searched 40 minutes ago
    assert LinkedInJobsScanner().run(make_ctx(client.app, NOW)).status == ScanStatus.skipped  # hourly
    # The LinkedIn run interval is a different setting: the search still waits for its hour.
    client.put("/api/settings", json={"linkedin_scan_interval_minutes": 10})
    assert LinkedInJobsScanner().run(make_ctx(client.app, NOW)).status == ScanStatus.skipped
    client.put("/api/settings", json={"linkedin_search_interval_minutes": 30})
    assert LinkedInJobsScanner().run(make_ctx(client.app, NOW)).status == ScanStatus.ok


def add_finished(engine, kind: ScanKind, minutes_ago: int):
    finished = utcnow() - timedelta(minutes=minutes_ago)
    with Session(engine) as session:
        session.add(ScanRun(kind=kind, status=ScanStatus.ok, started_at=finished - timedelta(minutes=1), finished_at=finished))
        session.commit()
    return finished


def test_saving_a_run_interval_plans_that_group_again(settings, fake_browser):
    app = make_app(settings.model_copy(update={"scan_enabled": True}), fake_browser, scanners=[])
    scheduler = app.state.scheduler
    with TestClient(app) as client:
        engine = app.state.engine
        linkedin_done = add_finished(engine, ScanKind.linkedin_outreach, 20)
        naukri_done = add_finished(engine, ScanKind.naukri_apply, 5)
        assert app.state.scans.last_finished_at(Platform.linkedin) == linkedin_done
        assert app.state.scans.last_finished_at(Platform.naukri) == naukri_done
        naukri_planned = scheduler.next_run_at(Platform.naukri)  # 1 minute after startup

        client.put("/api/settings", json={"linkedin_scan_interval_minutes": 45})
        assert scheduler.next_run_at(Platform.linkedin) == linkedin_done + timedelta(minutes=45)
        assert scheduler.next_run_at(Platform.naukri) == naukri_planned  # the other group is untouched

        client.put("/api/settings", json={"naukri_scan_interval_minutes": 10})
        assert scheduler.next_run_at(Platform.naukri) == naukri_done + timedelta(minutes=10)
        assert scheduler.next_run_at(Platform.linkedin) == linkedin_done + timedelta(minutes=45)

        status = client.get("/api/scans/status").json()
        assert status["next_run_at"] == iso_utc(naukri_done + timedelta(minutes=10))  # the earliest
        assert status["platforms"]["linkedin"] == {
            "running": False, "interval_minutes": 45,
            "next_run_at": iso_utc(linkedin_done + timedelta(minutes=45)), "last_finished_at": iso_utc(linkedin_done),
        }
        assert status["platforms"]["naukri"]["last_finished_at"] == iso_utc(naukri_done)

    # After a restart both intervals come from the database.
    app = make_app(settings.model_copy(update={"scan_enabled": True}), fake_browser, scanners=[])
    with TestClient(app):
        scheduler = app.state.scheduler
        assert (scheduler.interval_minutes(Platform.linkedin), scheduler.interval_minutes(Platform.naukri)) == (45, 10)
        assert scheduler.next_run_at(Platform.linkedin) == linkedin_done + timedelta(minutes=45)
        assert scheduler.next_run_at(Platform.naukri) == naukri_done + timedelta(minutes=10)
