from datetime import timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models import ScanKind, ScanRun, ScanStatus
from app.timeutils import utcnow
from tests.conftest import make_app

KIND = ScanKind.linkedin_jobs


def add_runs(engine, *runs: tuple[ScanStatus, str | None, int], kind: ScanKind = KIND) -> None:
    """Runs as (status, message, finished N minutes ago), oldest first."""
    with Session(engine) as session:
        for status, message, minutes_ago in runs:
            finished = utcnow() - timedelta(minutes=minutes_ago)
            session.add(
                ScanRun(kind=kind, status=status, message=message, started_at=finished - timedelta(minutes=2), finished_at=finished)
            )
        session.commit()


def backoff(app, kind: ScanKind = KIND):
    return app.state.scans._backoff(kind)


def test_no_backoff_without_failures(settings, fake_browser):
    app = make_app(settings, fake_browser, scanners=[])
    with TestClient(app):
        assert backoff(app) is None
        add_runs(app.state.engine, (ScanStatus.ok, None, 40))
        assert backoff(app) is None


def test_single_failure_retries_at_the_normal_interval(settings, fake_browser):
    app = make_app(settings, fake_browser, scanners=[])
    with TestClient(app):
        add_runs(app.state.engine, (ScanStatus.failed, "The scan stopped: Page.goto: Timeout", 29))
        assert backoff(app) is None


def test_rate_limit_waits_at_least_two_hours(settings, fake_browser):
    app = make_app(settings, fake_browser, scanners=[])
    with TestClient(app):
        add_runs(app.state.engine, (ScanStatus.failed, "LinkedIn returned HTTP 429 to the public job search; stopped.", 31))
        message = backoff(app)
        assert message is not None and "429" in message and "next try is after" in message


def test_rate_limit_retries_after_two_hours(settings, fake_browser):
    app = make_app(settings, fake_browser, scanners=[])
    with TestClient(app):
        add_runs(app.state.engine, (ScanStatus.failed, "LinkedIn returned HTTP 429; stopped.", 125))
        assert backoff(app) is None


def test_repeated_failures_double_the_wait(settings, fake_browser):
    app = make_app(settings, fake_browser, scanners=[])
    with TestClient(app):
        # Two failures in a row: wait 2 x 30 = 60 minutes after the last one.
        add_runs(app.state.engine, (ScanStatus.failed, "timeout", 70), (ScanStatus.failed, "timeout", 40))
        assert "2 failed runs in a row" in backoff(app)


def test_repeated_failures_retry_after_the_doubled_wait(settings, fake_browser):
    app = make_app(settings, fake_browser, scanners=[])
    with TestClient(app):
        add_runs(app.state.engine, (ScanStatus.failed, "timeout", 100), (ScanStatus.failed, "timeout", 61))
        assert backoff(app) is None


def test_offline_failures_dont_count(settings, fake_browser):
    app = make_app(settings, fake_browser, scanners=[])
    with TestClient(app):
        offline = "The scan stopped: Page.goto: net::ERR_INTERNET_DISCONNECTED at https://www.linkedin.com/x"
        add_runs(
            app.state.engine,
            (ScanStatus.failed, offline, 50),
            (ScanStatus.failed, "The scan failed: The browser couldn't start: BrowserType.launch: Timeout", 40),
            (ScanStatus.failed, offline, 10),
            (ScanStatus.failed, "Interrupted: the backend stopped during this run.", 5),
        )
        assert backoff(app) is None


def test_skipped_runs_dont_reset_and_ok_runs_do(settings, fake_browser):
    app = make_app(settings, fake_browser, scanners=[])
    with TestClient(app):
        add_runs(
            app.state.engine,
            (ScanStatus.failed, "timeout", 90),
            (ScanStatus.skipped, "Paused", 60),
            (ScanStatus.failed, "timeout", 30),
        )
        assert backoff(app) is not None  # the skipped run in between doesn't break the streak
        add_runs(app.state.engine, (ScanStatus.ok, None, 1))
        assert backoff(app) is None


def test_a_failed_run_that_found_results_ends_the_streak(settings, fake_browser):
    app = make_app(settings, fake_browser, scanners=[])
    with TestClient(app):
        add_runs(app.state.engine, (ScanStatus.failed, "timeout", 70), (ScanStatus.failed, "timeout", 50))
        with Session(app.state.engine) as session:
            from datetime import timedelta as _td
            finished = utcnow() - _td(minutes=10)
            session.add(ScanRun(kind=KIND, status=ScanStatus.failed, found=20, message="stopped on the last page",
                                started_at=finished - _td(minutes=2), finished_at=finished))
            session.commit()
        assert backoff(app) is None


def test_backoff_uses_the_scanner_group_interval(settings, fake_browser):
    """The doubled wait is based on the scanner's own group interval (2026-10-15)."""
    app = make_app(settings, fake_browser, scanners=[])
    with TestClient(app) as client:
        failures = ((ScanStatus.failed, "timeout", 70), (ScanStatus.failed, "timeout", 40))
        add_runs(app.state.engine, *failures)
        add_runs(app.state.engine, *failures, kind=ScanKind.naukri_jobs)
        # 2 x 30 = 60 minutes after the last failure: both still wait.
        assert backoff(app) is not None and backoff(app, ScanKind.naukri_jobs) is not None
        client.put("/api/settings", json={"naukri_scan_interval_minutes": 15})
        assert backoff(app, ScanKind.naukri_jobs) is None  # 2 x 15 = 30 minutes have passed
        assert backoff(app) is not None  # LinkedIn keeps its own interval
        client.put("/api/settings", json={"linkedin_scan_interval_minutes": 15})
        assert backoff(app) is None
        client.put("/api/settings", json={"naukri_scan_interval_minutes": 60})
        assert "2 failed runs in a row" in backoff(app, ScanKind.naukri_jobs)  # 2 x 60 minutes
