import os

os.environ["LOG_FILE"] = ""  # set before app.main is imported: never the real data/logs/backend.log

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.config import Settings
from app.main import create_app
from tests.fakes import FakeBrowser

FAKE_PDF = b"%PDF-1.4\n% test resume\n%%EOF\n"


@pytest.fixture(autouse=True)
def _no_real_browser(tmp_path: Path, monkeypatch) -> None:
    """Every test, including ones that build ``Settings`` themselves, gets a temporary browser
    profile directory, scanning off, and no real browser: the real profiles hold live logins."""
    monkeypatch.setenv("BROWSER_PROFILES_DIR", str(tmp_path / "env-browser-profiles"))
    monkeypatch.setenv("SCAN_ENABLED", "false")
    monkeypatch.setenv("LOG_FILE", "")  # never the real data/logs/backend.log
    # Never the user's real Chrome profile or Keychain.
    monkeypatch.setenv("CHROME_USER_DATA_DIR", str(tmp_path / "no-chrome"))
    # Behave as on the Mac on any OS: the Chrome tests use synthetic cookie databases.
    monkeypatch.setattr("app.browser.chrome_cookies.on_macos", lambda: True)
    # No app login unless a test sets one (whatever the shell or backend/.env says).
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    monkeypatch.delenv("JOB_APPLY_PASSWORD", raising=False)
    monkeypatch.setenv("DIAGNOSTICS_DIR", str(tmp_path / "diagnostics"))

    def no_keychain():
        raise AssertionError("A test tried to read the macOS Keychain; inject a password provider.")

    monkeypatch.setattr("app.browser.chrome_cookies.keychain_password", no_keychain)

    def no_playwright(settings):
        def factory():
            raise AssertionError("A test tried to start a real browser; pass a fake driver_factory.")

        return factory

    monkeypatch.setattr("app.main.playwright_factory", no_playwright)

    def no_smtp(host, port, timeout):
        raise AssertionError("A test tried to send real email; pass a fake transport (tests.fakes.FakeSMTP).")

    # Never a real mail server.
    monkeypatch.setattr("app.services.mailer.default_transport", no_smtp)
    # Failure screenshots go to a temporary folder, never the real backend/data/diagnostics (also
    # for scanners run without an app; create_app sets it from the settings).
    monkeypatch.setattr("app.scanner.base.DIAGNOSTICS_DIR", tmp_path / "diagnostics")


@pytest.fixture
def profile_path(tmp_path: Path) -> Path:
    """Where the generated mirror file goes. It doesn't exist until the first profile or template
    write; the real profile file is never touched."""
    return tmp_path / "profile" / "CANDIDATE_PROFILE.md"


@pytest.fixture
def resume_path(tmp_path: Path) -> Path:
    """A small fake PDF; tests delete it to simulate a missing resume."""
    path = tmp_path / "resume" / "Test_Resume.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(FAKE_PDF)
    return path


@pytest.fixture
def settings(tmp_path: Path, profile_path: Path, resume_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        profile_path=profile_path,
        resume_path=resume_path,
        cors_origins="http://localhost:5173",
        browser_profiles_dir=tmp_path / "browser-profiles",
        chrome_user_data_dir=tmp_path / "chrome",
        diagnostics_dir=tmp_path / "diagnostics",
        scan_enabled=False,
    )


@pytest.fixture
def fake_browser() -> FakeBrowser:
    """The fake browser behind every app the tests create; no real browser is ever started."""
    return FakeBrowser()


def make_app(settings: Settings, fake_browser: FakeBrowser, **kwargs):
    app = create_app(settings, driver_factory=fake_browser.factory(), **kwargs)
    app.state.scans.wait_range = (0.0, 0.0)
    app.state.connections.poll_seconds = 0.01
    return app


@pytest.fixture
def client(settings: Settings, fake_browser: FakeBrowser) -> Iterator[TestClient]:
    with TestClient(make_app(settings, fake_browser)) as test_client:
        yield test_client


@pytest.fixture
def session(client: TestClient) -> Iterator[Session]:
    with Session(client.app.state.engine) as db_session:
        yield db_session
