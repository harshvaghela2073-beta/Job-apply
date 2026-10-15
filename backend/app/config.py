"""Application settings, read from environment variables (and ``backend/.env`` if present).

Relative paths (``PROFILE_PATH``, ``RESUME_PATH``, ``BROWSER_PROFILES_DIR`` and SQLite file paths in
``DATABASE_URL``) resolve against the ``backend/`` directory, never against the process's working
directory.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _resolve(path: Path) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        path = BACKEND_DIR / path
    return path.resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    database_url: str = "sqlite:///data/jobapply.db"
    # Comma-separated list of allowed browser origins for the frontend.
    cors_origins: str = "http://localhost:5173"
    # Generated, read-only mirror of the profile and Template Library (rewritten on every change).
    profile_path: Path = Path("../profile/CANDIDATE_PROFILE.md")
    resume_path: Path = Path("../profile/resume/Harsh_Vaghela.pdf")

    easy_apply_daily_cap: int = Field(default=25, ge=0)
    naukri_daily_cap: int = Field(default=50, ge=0)
    connection_notes_monthly_cap: int = Field(default=250, ge=0)
    inmail_monthly_cap: int = Field(default=5, ge=0)
    # Automatic emails to hiring posts (docs/API_CONTRACT.md, "Automatic email and LinkedIn outreach").
    email_daily_cap: int = Field(default=20, ge=0)

    # Gmail SMTP for those emails. The app password is a secret: it is never logged or returned.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_from_name: str = ""
    smtp_app_password: SecretStr = SecretStr("")

    # Saved LinkedIn / Naukri logins: one Chrome profile directory per platform (gitignored).
    browser_profiles_dir: Path = Path("../.browser-profiles")
    # Close a browser that shows no activity for this long (0 = never); see app/browser/worker.py.
    browser_stall_minutes: float = Field(default=5, ge=0)
    # The app's own log, next to the console output ("" = console only).
    log_file: str = "data/logs/backend.log"
    # "chrome" uses the installed Google Chrome (no download); "chromium" needs
    # `python -m playwright install chromium`.
    browser_channel: Literal["chrome", "chromium"] = "chrome"
    # Background scanning (docs/API_CONTRACT.md, "Scans"). LinkedIn and Naukri run as separate groups
    # (2026-10-15); this is the default interval of both until one is set in the app.
    scan_enabled: bool = True
    scan_interval_minutes: int = Field(default=30, ge=1)
    scan_headless: bool = True
    # Page loads per scanner per run.
    scan_max_page_loads: int = Field(default=20, ge=1)
    # The logged-in LinkedIn job and post searches run at most this often (runs in between record
    # "skipped"). LINKEDIN_SCAN_INTERVAL_MINUTES is its name before 2026-10-15 and still works.
    linkedin_search_interval_minutes: int = Field(
        default=60,
        ge=1,
        validation_alias=AliasChoices("linkedin_search_interval_minutes", "linkedin_scan_interval_minutes"),
    )
    # How long a login window waits for the user before giving up.
    login_timeout_minutes: float = Field(default=10, gt=0)
    # The user's own Google Chrome data folder; "Connect" copies the platform's login from here.
    chrome_user_data_dir: Path = Path("~/Library/Application Support/Google/Chrome")
    # Screenshots and HTML of pages where a scan or login check failed (on a server: the persistent disk).
    diagnostics_dir: Path = Path("data/diagnostics")

    # App login (docs/API_CONTRACT.md, "Deployment"). Empty = no login, the local default. Secrets:
    # never logged or returned. AUTH_SECRET signs the login tokens (derived from APP_PASSWORD when
    # empty); changing either one logs every device out.
    app_password: SecretStr = SecretStr("")
    auth_secret: SecretStr = SecretStr("")

    @field_validator("profile_path", "resume_path", "browser_profiles_dir", "chrome_user_data_dir", "diagnostics_dir")
    @classmethod
    def _resolve_paths(cls, value: Path) -> Path:
        return _resolve(Path(value))

    @field_validator("smtp_app_password", mode="before")
    @classmethod
    def _strip_password_spaces(cls, value):
        # Google shows app passwords in groups of four ("abcd efgh ijkl mnop"); the spaces don't count.
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        return SecretStr("".join(str(value or "").split()))

    @field_validator("smtp_user", "smtp_host", "smtp_from_name")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("database_url")
    @classmethod
    def _resolve_sqlite_path(cls, value: str) -> str:
        url = make_url(value)
        if url.get_backend_name() != "sqlite":
            return value
        database = url.database
        if not database or database == ":memory:" or database.startswith("file:"):
            return value
        resolved = _resolve(Path(database))
        return url.set(database=str(resolved)).render_as_string(hide_password=False)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
