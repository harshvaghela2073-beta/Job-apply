"""Types shared by the scanners and the scan runner."""

import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import Engine

from app.browser import BrowserPool
from app.config import BACKEND_DIR, Settings
from app.models import ApplyType, Platform, ScanKind, ScanStatus
from app.scanner.matching import SearchPrefs

if TYPE_CHECKING:
    from app.services.connections import ConnectionService


logger = logging.getLogger(__name__)

# Screenshots of pages where a scan failed (gitignored, under data/). ``create_app`` sets it to the
# DIAGNOSTICS_DIR setting (``set_diagnostics_dir``); the notes keep calling it "data/diagnostics/".
DIAGNOSTICS_DIR = BACKEND_DIR / "data" / "diagnostics"


def set_diagnostics_dir(path: Path) -> None:
    global DIAGNOSTICS_DIR
    DIAGNOSTICS_DIR = path


def capture_failure(browser, kind: str) -> str:
    """Log the page a scan failed on and save a screenshot of it. Returns a note for the run message.

    Never raises: diagnostics must not hide the real failure.
    """
    url = title = ""
    try:
        url, title = browser.current_url(), browser.title()
    except Exception:  # noqa: BLE001
        pass
    note = ""
    try:
        DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
        path = DIAGNOSTICS_DIR / f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        if browser.screenshot(path):
            note = f" Screenshot: data/diagnostics/{path.name}."
        # The page's HTML too: it shows why a parser found nothing even when the screenshot fails.
        html_path = path.with_suffix(".html")
        html_path.write_text(browser.html() or "", encoding="utf-8")
        note += f" Page: data/diagnostics/{html_path.name}."
    except Exception:  # noqa: BLE001
        logger.debug("Saving the failure screenshot failed", exc_info=True)
    logger.warning("Scan %s failed on %s (title %r).%s", kind, url, title, note)
    return note


class ScanBlocked(RuntimeError):
    """The platform showed a block page, a CAPTCHA or an error; the run stops here."""


class LoginWall(ScanBlocked):
    """A logged-in page redirected to a login wall: the saved login no longer works."""


@dataclass
class Candidate:
    """A matching job or post, ready to be stored as a ``Job`` row with status ``found``."""

    platform: Platform
    external_id: str
    title: str
    company: str
    url: str
    apply_type: ApplyType
    priority: int
    matched_role: str
    location: str = ""
    apply_url: str | None = None
    form_url: str | None = None
    contact_email: str | None = None
    post_url: str | None = None
    short_description: str = ""
    posted_at: datetime | None = None
    experience: str | None = None
    # Who to send a LinkedIn connection note to (a hiring post's author), when known.
    contact_name: str | None = None
    contact_profile_url: str | None = None


@dataclass
class ScanOutcome:
    status: ScanStatus
    found: int = 0
    new: int = 0
    message: str | None = None
    page_loads: int = 0


@dataclass
class ScanReport:
    """What a scanner's browser task found, before storing."""

    candidates: list[Candidate] = field(default_factory=list)
    failure: str | None = None  # set when the run stopped at a block, CAPTCHA or error
    notes: list[str] = field(default_factory=list)
    page_loads: int = 0
    # Logged-in scans: whether a page proved the login works, or why it no longer does.
    login_ok: bool = False
    login_lost: str | None = None
    # Candidates whose apply type is a guess (stored as company_site unless confirmed).
    unconfirmed: list[Candidate] = field(default_factory=list)


@dataclass
class ScanContext:
    engine: Engine
    settings: Settings
    prefs: SearchPrefs
    now: datetime
    browsers: BrowserPool
    connections: "ConnectionService"
    stop: threading.Event
    # Start of the previous successful run of this scanner, if any.
    last_ok_started_at: datetime | None = None
    wait_range: tuple[float, float] = (2.0, 6.0)
    # Started from the app ("Run now", POST /api/scans/run) rather than by the schedule.
    manual: bool = False

    def pacer(self) -> "Pacer":
        return Pacer(self.settings.scan_max_page_loads, self.wait_range, self.stop)


class Scanner(Protocol):
    kind: ScanKind

    def run(self, ctx: ScanContext) -> ScanOutcome: ...


@dataclass
class Pacer:
    """Page-load budget and human-like pauses: a random wait before every load but the first.

    ``next_load()`` returns False when the budget is used up or the run is being stopped.
    """

    max_loads: int
    wait_range: tuple[float, float] = (2.0, 6.0)
    stop: threading.Event = field(default_factory=threading.Event)
    rng: Callable[[float, float], float] = random.uniform
    loads: int = 0

    @property
    def exhausted(self) -> bool:
        return self.loads >= self.max_loads

    def next_load(self) -> bool:
        if self.exhausted or self.stop.is_set():
            return False
        if self.loads:
            low, high = self.wait_range
            if high > 0 and self.stop.wait(self.rng(low, high)):
                return False
        self.loads += 1
        return True


def truncate(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
