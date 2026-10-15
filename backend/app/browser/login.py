"""How to recognise a platform's login. Each source defines one ``LoginSpec``
(``app/sources/<platform>/login.py``).

A login counts only when **both** checks pass (docs/API_CONTRACT.md, "Detecting a real login"):
1. the auth cookie is there (for platforms whose cookie name is known, ``cookie_required``), and
2. the platform confirms the session. LinkedIn uses an authenticated API call (``confirm`` in its
   ``LoginSpec``), so the check doesn't depend on page markup that LinkedIn changes often; Naukri
   loads a member-only page and checks that it shows a logged-in member and not the Login/Register
   buttons. A URL that merely didn't redirect is never enough.

A ``/checkpoint`` (security check) is reported separately so the app can ask the user to finish it.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

from app.browser.driver import BrowserSession, has_cookie
from app.models import Platform


@dataclass(frozen=True)
class LoginCheck:
    ok: bool
    account_name: str | None = None
    reason: str | None = None  # why it isn't a login (path / title / markers; never cookie values)
    security_check: bool = False  # a /checkpoint challenge the user must finish


@dataclass(frozen=True)
class LoginSpec:
    platform: Platform
    display_name: str
    login_url: str
    # A member-only page; loading it confirms the login (and spots /checkpoint).
    confirm_url: str
    # CSS selectors (comma-separated) to wait for on the confirm page: logged-in *or* logged-out
    # markers, so the check runs once the page has rendered either way.
    confirm_wait_for: str
    cookie_domain: str
    # Cookies that appear after a login.
    auth_cookie_names: tuple[str, ...]
    # True when check 1 applies: no auth cookie means logged out.
    cookie_required: bool
    # URL fragments of logged-in pages. Only a hint to *try* the checks in the login window.
    logged_in_url_markers: tuple[str, ...]
    # URL fragments of login / auth-wall / security-check pages.
    login_wall_markers: tuple[str, ...]
    # URL fragments of a security-challenge page (a subset that needs the user, not a re-login).
    security_check_markers: tuple[str, ...]
    # Whether a confirm page's HTML shows a logged-in member (Naukri; also diagnostics).
    page_logged_in: Callable[[str], bool]
    # Best-effort account name from the confirm page's HTML.
    account_name: Callable[[str], str | None]
    # Whether a cookie name in the user's Chrome suggests a login (fast path, no decryption).
    chrome_login_cookie: Callable[[str], bool]
    # The whole confirm step; defaults to the page-markup check below.
    confirm: "Callable[[BrowserSession, LoginSpec], LoginCheck] | None" = None

    def has_auth_cookie(self, cookies: Sequence[dict]) -> bool:
        return has_cookie(cookies, self.auth_cookie_names, self.cookie_domain)

    def auth_cookie_value(self, cookies: Sequence[dict]) -> str | None:
        for cookie in cookies:
            if cookie.get("name") in self.auth_cookie_names and cookie.get("value"):
                if cookie.get("domain", "").lstrip(".").endswith(self.cookie_domain):
                    return cookie["value"]
        return None

    def is_login_wall(self, url: str) -> bool:
        lowered = url.lower()
        return any(marker in lowered for marker in self.login_wall_markers)

    def is_security_check(self, url: str) -> bool:
        lowered = url.lower()
        return any(marker in lowered for marker in self.security_check_markers)

    def looks_logged_in(self, url: str) -> bool:
        lowered = url.lower()
        return not self.is_login_wall(lowered) and any(m in lowered for m in self.logged_in_url_markers)


def title_of(html: str) -> str:
    from app.sources.html import soup, text_of

    return text_of(soup(html).title)[:60]


def confirm_via_page(browser: BrowserSession, spec: LoginSpec) -> LoginCheck:
    """The default confirm: load the member page and read its HTML (Naukri)."""
    result = browser.goto(spec.confirm_url, wait_for=spec.confirm_wait_for, timeout=45)
    path = urlparse(result.url).path or "/"
    if spec.is_security_check(result.url):
        return LoginCheck(False, reason=f"a security check at {path}", security_check=True)
    if spec.is_login_wall(result.url):
        return LoginCheck(False, reason=f"it redirected to {path}")
    if not spec.page_logged_in(result.html):
        return LoginCheck(False, reason=f"{path} (title {title_of(result.html)!r}) shows no logged-in member")
    return LoginCheck(True, account_name=spec.account_name(result.html))


def confirm_login(browser: BrowserSession, spec: LoginSpec) -> LoginCheck:
    """Run both login checks in ``browser``. ``reason`` is safe to log / show (no cookie values)."""
    if spec.cookie_required and not spec.has_auth_cookie(browser.cookies()):
        return LoginCheck(False, reason=f"no {' / '.join(spec.auth_cookie_names)} cookie")
    confirm = spec.confirm or confirm_via_page
    return confirm(browser, spec)
