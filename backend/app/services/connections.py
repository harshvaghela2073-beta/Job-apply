"""Platform logins (docs/API_CONTRACT.md, "Connections").

Each platform has its own browser profile (``BROWSER_PROFILES_DIR/<platform>``) that keeps the
login. Passwords never reach the app. Two ways to fill it:

- ``import_from_chrome`` (the main way): copy the platform's cookies from the user's own Google
  Chrome into the profile, then confirm the login. See ``app.browser.chrome_cookies`` for the
  safety rules (values never leave memory / the tool's profile). On a server without the user's
  Chrome this raises ``ChromeUnavailable``; there ``install_cookies`` takes the cookies pushed from
  the Mac (``python -m app.tools.push login``) and installs them the same way.
- ``connect`` (fallback): a visible login window on the profile. It is polled until the auth
  cookie (or a logged-in URL) appears, then the login is confirmed. Window closed first, or
  ``LOGIN_TIMEOUT_MINUTES`` passed -> ``disconnected`` with a message.

``connected`` always means both checks in ``app.browser.login.confirm_login`` passed: the auth
cookie (where its name is known) and a member-only page whose HTML shows a logged-in member.

Other transitions: ``disconnect`` deletes the profile; the startup check (background) re-runs the
checks; scans report login walls (``disconnected``) and working pages (``connected``); before a
logged-in scan the login is re-imported from Chrome silently (``sync_from_chrome``).

All browser work for a platform runs on that platform's worker thread, so the profile directory is
never opened twice at once.
"""

import hashlib
import logging
import shutil
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session

from app.browser import BrowserPool, BrowserSession, BrowserUnavailable
from app.browser.chrome_cookies import ChromeCookie, ChromeCookieReader, ChromeCookiesError, host_matches
from app.browser.login import LoginSpec, confirm_login
from app.config import Settings
from app.models import Connection, ConnectionStatus, Platform
from app.sources.linkedin.login import LINKEDIN_LOGIN
from app.sources.naukri.login import NAUKRI_LOGIN
from app.timeutils import utcnow

logger = logging.getLogger(__name__)

LOGIN_SPECS: dict[Platform, LoginSpec] = {
    Platform.linkedin: LINKEDIN_LOGIN,
    Platform.naukri: NAUKRI_LOGIN,
}
# Contract order: LinkedIn first.
PLATFORM_ORDER = (Platform.linkedin, Platform.naukri)
# A startup check is skipped when the last one is this recent (``--reload`` restarts often).
RECHECK_AFTER = timedelta(minutes=15)
# Identical Chrome cookies aren't re-verified more often than this (the frontend polls import every
# few seconds; each check costs a browser page load). Keyed by a hash of the cookie values.
RECHECK_IMPORT_SECONDS = 60
_KEEP = object()
CHROME_UNAVAILABLE = "Chrome isn't on this server: push the login from your Mac."


class AlreadyConnecting(RuntimeError):
    pass


class LoginWindowOpen(RuntimeError):
    pass


class ChromeUnavailable(RuntimeError):
    """The user's Chrome isn't on this machine (a server): logins are pushed from the Mac instead."""


class LoginBusy(RuntimeError):
    """Another import or push of the same platform's login is still running."""


class NoLoginCookies(ValueError):
    """Pushed cookies hold no login for the platform; the saved login is left as it is."""


class ConnectionService:
    def __init__(self, engine: Engine, settings: Settings, browsers: BrowserPool):
        self.engine = engine
        self.settings = settings
        self.browsers = browsers
        self.chrome = ChromeCookieReader(settings.chrome_user_data_dir)
        self.poll_seconds = 2.0
        # How long an import request waits for the browser check before returning the current row.
        self.import_wait_seconds = 120.0
        self._lock = threading.Lock()
        self._logins: dict[Platform, threading.Event] = {}  # platform -> cancel flag
        self._imports: dict[Platform, threading.Lock] = {p: threading.Lock() for p in Platform}
        # platform -> (hash of the Chrome login cookie values, time) of the last verified import
        self._import_cache: dict[Platform, tuple[str, float]] = {}

    # ------------------------------------------------------------------ rows

    def _save(
        self,
        platform: Platform,
        status: ConnectionStatus,
        *,
        message: str | None = None,
        account_name=_KEEP,
        checked: bool = True,
    ) -> Connection:
        with Session(self.engine) as session:
            row = session.get(Connection, platform) or Connection(platform=platform)
            row.status = status
            row.message = message
            if account_name is not _KEEP:
                row.account_name = account_name
            if checked:
                row.checked_at = utcnow()
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get(self, platform: Platform) -> Connection:
        with Session(self.engine) as session:
            row = session.get(Connection, platform)
            if row is None:
                row = Connection(platform=platform)
                session.add(row)
                session.commit()
                session.refresh(row)
            return row

    def all(self) -> list[Connection]:
        return [self.get(platform) for platform in PLATFORM_ORDER]

    def startup(self) -> None:
        """Make sure both rows exist; a login window can't survive a restart."""
        for platform in PLATFORM_ORDER:
            row = self.get(platform)
            if row.status == ConnectionStatus.connecting and platform not in self._logins:
                self._save(platform, ConnectionStatus.disconnected, message="The backend restarted before the login finished.")

    def profile_dir(self, platform: Platform) -> Path:
        return self.browsers.profile_dir(platform)

    def is_connecting(self, platform: Platform) -> bool:
        with self._lock:
            return platform in self._logins

    def _save_check(self, spec: LoginSpec, check, *, failed_message: str) -> Connection:
        if check.ok:
            return self._save(spec.platform, ConnectionStatus.connected, account_name=check.account_name or _KEEP)
        logger.info("%s login not confirmed: %s", spec.display_name, check.reason)
        if check.security_check:
            return self._save(spec.platform, ConnectionStatus.error, message=_security_message(spec))
        return self._save(spec.platform, ConnectionStatus.disconnected, message=failed_message)

    # ------------------------------------------------------------------ used by scans

    def login_usable(self, platform: Platform) -> bool:
        """Whether a scan should use the saved login: a profile exists, no login window is open,
        and the last check didn't say it's logged out."""
        if self.is_connecting(platform) or not self.profile_dir(platform).is_dir():
            return False
        return self.get(platform).status in (ConnectionStatus.connected, ConnectionStatus.error)

    def record_login_ok(self, platform: Platform) -> None:
        if self.is_connecting(platform):
            return
        if self.get(platform).status in (ConnectionStatus.connected, ConnectionStatus.error):
            self._save(platform, ConnectionStatus.connected)

    def record_login_lost(self, platform: Platform, message: str) -> None:
        if not self.is_connecting(platform):
            self._save(platform, ConnectionStatus.disconnected, message=message)

    def sync_from_chrome(self, platform: Platform) -> None:
        """Before a logged-in scan: silently copy the login from Chrome again, so the tool follows
        the user's browser. Never prompts: it only runs when the Keychain key is already unlocked
        (from a Connect in this backend session). If Chrome shows no login, the tool's own saved
        login is left as it is (the scan itself notices an expired one)."""
        if self.is_connecting(platform) or not self.chrome.available or not self.chrome.has_key:
            return
        try:
            self.import_from_chrome(platform, interactive=False)
        except Exception:  # noqa: BLE001 - a failed sync must not stop the scan
            logger.warning("Silent %s import from Chrome failed", platform, exc_info=True)

    # ------------------------------------------------------------------ import from Chrome

    def import_from_chrome(self, platform: Platform, *, interactive: bool = True) -> Connection:
        """Copy the platform's login from the user's Chrome into the tool's profile and confirm it.

        Returns the row: ``connected``, or ``disconnected`` / ``error`` with a message. While
        another import of the same platform is running, returns the current row unchanged.
        Raises ``ChromeUnavailable`` on a machine without the user's Chrome.
        """
        spec = LOGIN_SPECS[platform]
        self._refuse_while_login_window(spec)
        if not self.chrome.available:
            raise ChromeUnavailable(CHROME_UNAVAILABLE)
        lock = self._imports[platform]
        if not lock.acquire(blocking=False):
            return self.get(platform)
        release = True
        try:
            try:
                newest = self.chrome.newest_logged_in(spec.cookie_domain, spec.chrome_login_cookie)
            except ChromeCookiesError as error:
                return self._save(platform, ConnectionStatus.error, message=str(error)) if interactive else self.get(platform)
            if newest is None:
                if not interactive:
                    return self.get(platform)
                return self._save(
                    platform, ConnectionStatus.disconnected, message=f"Not logged in to {spec.display_name} in your Chrome yet."
                )
            try:
                cookies = self.chrome.read(newest, spec.cookie_domain)
            except ChromeCookiesError as error:
                if not interactive:
                    return self.get(platform)
                return self._save(platform, ConnectionStatus.error, message=str(error))
            fingerprint = _fingerprint(spec, cookies)
            last = self._import_cache.get(platform)
            if last and last[0] == fingerprint and time.monotonic() - last[1] < RECHECK_IMPORT_SECONDS:
                return self.get(platform)  # identical cookies verified seconds ago: don't load pages again
            logger.info("Importing %d %s cookies from Chrome profile %r", len(cookies), spec.display_name, newest.profile)
            release = False  # _install releases the lock when its browser task ends
            return self._install(spec, cookies, fingerprint, lock, pushed=False)
        finally:
            if release:
                lock.release()

    def install_cookies(self, platform: Platform, cookies: list[ChromeCookie]) -> Connection:
        """Install a login pushed from the Mac (``PUT /api/connections/{platform}/cookies``) into the
        tool's profile and confirm it, the same way as ``import_from_chrome``. Only cookies of the
        platform's domain are used. Raises ``NoLoginCookies`` (nothing changed) when none of them is
        a login, ``LoginBusy`` while another import or push of this platform runs and
        ``LoginWindowOpen`` while a login window is open."""
        spec = LOGIN_SPECS[platform]
        now = time.time()
        cookies = [
            c for c in cookies
            if host_matches(c.host, spec.cookie_domain) and (c.expires is None or c.expires > now)
        ]
        if not any(spec.chrome_login_cookie(c.name) for c in cookies):
            raise NoLoginCookies(
                f"No {spec.display_name} login among the cookies. Log in to {spec.display_name} in Chrome "
                "on your Mac, then push again."
            )
        self._refuse_while_login_window(spec)
        lock = self._imports[platform]
        if not lock.acquire(blocking=False):
            raise LoginBusy(f"A {spec.display_name} login check is already running. Try again in a minute.")
        logger.info(
            "Installing %d pushed %s cookies: %s",
            len(cookies), spec.display_name, ", ".join(sorted({c.name for c in cookies})),
        )
        return self._install(spec, cookies, _fingerprint(spec, cookies), lock, pushed=True)

    def _refuse_while_login_window(self, spec: LoginSpec) -> None:
        if self.is_connecting(spec.platform):
            raise LoginWindowOpen(
                f"A {spec.display_name} login window is open. Finish or close it first, or disconnect."
            )

    def _install(
        self, spec: LoginSpec, cookies: list[ChromeCookie], fingerprint: str, lock: threading.Lock, *, pushed: bool
    ) -> Connection:
        """Install ``cookies`` on the platform's worker thread and confirm the login, waiting up to
        ``import_wait_seconds`` (then the current row; the task saves its result later). Takes over
        ``lock`` (held by the caller) and releases it when the task ends."""
        platform = spec.platform
        try:
            future = self.browsers.worker(platform.value).submit(
                lambda driver: self._install_and_check(driver, spec, cookies, fingerprint, pushed=pushed)
            )
        except BaseException:
            lock.release()
            raise
        future.add_done_callback(lambda _: lock.release())
        try:
            return future.result(timeout=self.import_wait_seconds)
        except FutureTimeout:
            return self.get(platform)  # still busy (e.g. behind a scan); the task saves the result
        except BrowserUnavailable as error:
            return self._save(platform, ConnectionStatus.error, message=str(error))
        except Exception as error:  # noqa: BLE001
            logger.exception("%s import failed", spec.display_name)
            return self._save(platform, ConnectionStatus.error, message=f"The login check failed: {error}")

    def _install_and_check(
        self, driver, spec: LoginSpec, cookies: list[ChromeCookie], fingerprint: str, *, pushed: bool = False
    ) -> Connection:
        if self.is_connecting(spec.platform):
            return self.get(spec.platform)
        browser: BrowserSession = driver.open(profile_dir=self.profile_dir(spec.platform), headless=self.settings.scan_headless)
        try:
            browser.clear_cookies(spec.cookie_domain)
            rejected = browser.add_cookies([cookie.to_playwright() for cookie in cookies])
            if rejected:
                logger.info("Chrome rejected %s cookies: %s", spec.display_name, ", ".join(rejected))
            check = confirm_login(browser, spec)
            if not check.ok:
                self._capture_diagnostics(browser, spec, check)
        finally:
            browser.close()
        self._import_cache[spec.platform] = (fingerprint, time.monotonic())
        if check.ok:
            return self._save(spec.platform, ConnectionStatus.connected, account_name=check.account_name or _KEEP)
        if check.security_check:
            return self._save(spec.platform, ConnectionStatus.error, message=_security_message(spec))
        # Cookies were found in Chrome but the login can't be confirmed: `error`, not `disconnected`,
        # so the frontend stops polling.
        if pushed:
            return self._save(
                spec.platform,
                ConnectionStatus.error,
                message=(
                    f"Installed the {spec.display_name} login pushed from your Mac, but {spec.display_name} "
                    f"didn't show a logged-in session ({check.reason}). Open {spec.login_url} in Chrome on "
                    "your Mac, log in, and push the login again."
                ),
            )
        return self._save(
            spec.platform,
            ConnectionStatus.error,
            message=(
                f"Copied the {spec.display_name} cookies from Chrome, but {spec.display_name} didn't show a "
                f"logged-in session ({check.reason}). Open {spec.login_url} in Chrome, log in, and try again."
            ),
        )

    def _capture_diagnostics(self, browser: BrowserSession, spec: LoginSpec, check) -> None:
        """Log the final URL, page title and marker hits, and save a screenshot. No cookie values."""
        url = title = ""
        markers = ""
        try:
            url, title = browser.current_url(), browser.title()
            html = (browser.html() or "").lower()
            tokens = [t for t in ("sign in", "join now", "login", "register", "/checkpoint", "security") if t in html]
            markers = f"logged_in_markup={spec.page_logged_in(browser.html())}, tokens={tokens}"
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            logger.debug("Reading diagnostics failed", exc_info=True)
        shot = ""
        try:
            folder = self.settings.diagnostics_dir
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{spec.platform.value}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
            if browser.screenshot(path):
                shot = f"; screenshot {path}"
        except Exception:  # noqa: BLE001
            logger.debug("Diagnostics screenshot failed", exc_info=True)
        logger.info(
            "%s confirm failed: %s | url=%s title=%r %s%s",
            spec.display_name, check.reason, url, title, markers, shot,
        )

    # ------------------------------------------------------------------ connect (login window)

    def connect(self, platform: Platform) -> Connection:
        spec = LOGIN_SPECS[platform]
        with self._lock:
            if platform in self._logins:
                raise AlreadyConnecting(f"A {spec.display_name} login window is already open.")
            cancel = threading.Event()
            self._logins[platform] = cancel
        row = self._save(
            platform,
            ConnectionStatus.connecting,
            message=f"Log in to {spec.display_name} in the Chrome window that opened.",
            checked=False,
        )
        future = self.browsers.worker(platform.value).submit(lambda driver: self._login(driver, spec, cancel))
        future.add_done_callback(lambda f: self._login_finished(platform, cancel, f))
        return row

    def _login_finished(self, platform: Platform, cancel: threading.Event, future: Future) -> None:
        with self._lock:
            if self._logins.get(platform) is cancel:
                del self._logins[platform]
        if future.cancelled() or cancel.is_set():
            return
        error = future.exception()
        if error is not None:
            logger.error("%s login failed: %s", platform, error)
            message = str(error) if isinstance(error, BrowserUnavailable) else f"The login failed: {error}"
            self._save(platform, ConnectionStatus.error, message=message)

    def _login(self, driver, spec: LoginSpec, cancel: threading.Event) -> None:
        platform = spec.platform
        timeout = self.settings.login_timeout_minutes * 60
        deadline = time.monotonic() + timeout
        browser = driver.open(profile_dir=self.profile_dir(platform), headless=False)
        try:
            browser.goto(spec.login_url, timeout=60)
            tried: set[tuple] = set()  # (auth cookie, URL) combinations already checked
            while not cancel.is_set():
                if browser.is_closed():
                    browser.close()  # release the profile before checking it headless
                    self._login_window_closed(driver, spec)
                    return
                cookie = spec.auth_cookie_value(browser.cookies())
                url = browser.current_url()
                hint = cookie is not None or spec.looks_logged_in(url)
                if hint and (cookie, url) not in tried:
                    tried.add((cookie, url))
                    check = confirm_login(browser, spec)
                    if check.ok:
                        self._save(platform, ConnectionStatus.connected, account_name=check.account_name)
                        return
                    logger.info("%s login not confirmed yet: %s", spec.display_name, check.reason)
                    tried.add((cookie, browser.current_url()))
                    browser.goto(spec.login_url, timeout=60)  # back to the login form
                if time.monotonic() >= deadline:
                    minutes = self.settings.login_timeout_minutes
                    self._save(
                        platform,
                        ConnectionStatus.disconnected,
                        message=f"No login was detected within {minutes:g} minutes, so the window was closed.",
                    )
                    return
                browser.wait(self.poll_seconds)
        finally:
            browser.close()

    def _login_window_closed(self, driver, spec: LoginSpec) -> None:
        """The user closed the window. If they had logged in already, a headless check confirms it."""
        try:
            check = self._run_check(driver, spec)
        except Exception:
            check = None
        if check is not None and check.ok:
            self._save(spec.platform, ConnectionStatus.connected, account_name=check.account_name or _KEEP)
            return
        self._save(
            spec.platform,
            ConnectionStatus.disconnected,
            message="The login window was closed before the login finished.",
        )

    # ------------------------------------------------------------------ disconnect

    def disconnect(self, platform: Platform, wait_seconds: float = 5.0) -> Connection:
        """Cancel a login in progress and delete the saved profile. The deletion runs on the
        platform's worker, after any task using the profile; this waits briefly for it."""
        with self._lock:
            cancel = self._logins.pop(platform, None)
        if cancel is not None:
            cancel.set()
        profile_dir = self.profile_dir(platform)
        future = self.browsers.worker(platform.value).submit(lambda driver: _delete_profile(profile_dir))
        row = self._save(platform, ConnectionStatus.disconnected, account_name=None)
        try:
            future.result(timeout=wait_seconds)
        except FutureTimeout:
            logger.info("The %s profile will be deleted when the current browser task ends", platform)
        except Exception:
            logger.exception("Deleting the %s browser profile failed", platform)
        return row

    # ------------------------------------------------------------------ check

    def check_in_background(self, platform: Platform, *, force: bool = False) -> Future | None:
        """Queue a login check unless one isn't needed (no profile, recent check, login open)."""
        row = self.get(platform)
        if self.is_connecting(platform):
            return None
        if not self.profile_dir(platform).is_dir():
            if row.status != ConnectionStatus.disconnected:
                self._save(platform, ConnectionStatus.disconnected)
            return None
        recent = row.checked_at is not None and utcnow() - row.checked_at < RECHECK_AFTER
        if not force and recent and row.status == ConnectionStatus.disconnected:
            return None
        spec = LOGIN_SPECS[platform]
        return self.browsers.worker(platform.value).submit(lambda driver: self._check_task(driver, spec))

    def check_all_in_background(self) -> list[Future]:
        futures = [self.check_in_background(platform) for platform in PLATFORM_ORDER]
        return [future for future in futures if future is not None]

    def _check_task(self, driver, spec: LoginSpec) -> Connection:
        if self.is_connecting(spec.platform):
            return self.get(spec.platform)
        try:
            return self._check_with(driver, spec)
        except BrowserUnavailable as error:
            return self._save(spec.platform, ConnectionStatus.error, message=str(error))
        except Exception as error:  # noqa: BLE001
            logger.exception("%s login check failed", spec.platform)
            return self._save(spec.platform, ConnectionStatus.error, message=f"The login check failed: {error}")

    def _run_check(self, driver, spec: LoginSpec):
        """Both login checks on the saved profile, headless. None when there is no profile."""
        if not self.profile_dir(spec.platform).is_dir():
            return None
        browser: BrowserSession = driver.open(profile_dir=self.profile_dir(spec.platform), headless=self.settings.scan_headless)
        try:
            check = confirm_login(browser, spec)
            if not check.ok:
                self._capture_diagnostics(browser, spec, check)
            return check
        finally:
            browser.close()

    def _check_with(self, driver, spec: LoginSpec) -> Connection:
        check = self._run_check(driver, spec)
        if check is None:
            return self._save(spec.platform, ConnectionStatus.disconnected)
        return self._save_check(spec, check, failed_message=_expired(spec, check.reason))

    def shutdown(self) -> None:
        with self._lock:
            cancels = list(self._logins.values())
        for cancel in cancels:
            cancel.set()


def _security_message(spec: LoginSpec) -> str:
    domain = spec.cookie_domain
    return (
        f"{spec.display_name} wants a security check. Open {domain} in your Chrome, complete it, "
        "then click Connect again."
    )


def _expired(spec: LoginSpec, reason: str | None = None) -> str:
    detail = f" ({reason})" if reason else ""
    return f"The saved {spec.display_name} login has expired or isn't there{detail}. Connect again."


def _fingerprint(spec: LoginSpec, cookies: list[ChromeCookie]) -> str:
    """A hash of the login cookies' values (never stored or logged in clear)."""
    return hashlib.sha256(
        "\n".join(sorted(f"{c.host}|{c.name}|{c.value}" for c in cookies if spec.chrome_login_cookie(c.name))).encode()
    ).hexdigest()


def _delete_profile(profile_dir: Path) -> None:
    if profile_dir.is_dir():
        shutil.rmtree(profile_dir)
