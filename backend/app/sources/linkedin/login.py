"""Recognising a LinkedIn login.

The confirm step calls LinkedIn's own ``/voyager/api/me`` with the browser context's cookies
instead of reading page markup (LinkedIn renames its CSS classes often). A 200 JSON response means
logged in; a 401 / 403 / redirect means logged out. ``/feed/`` is still loaded, but only to spot a
``/checkpoint`` security challenge and to capture diagnostics when the API says "logged out".
"""

import logging
from urllib.parse import urlparse

from app.browser.driver import BrowserSession
from app.browser.login import LoginCheck, LoginSpec
from app.models import Platform
from app.sources.linkedin import selectors as S
from app.sources.linkedin.parse import account_name, account_name_from_me, chrome_login_cookie, page_logged_in

logger = logging.getLogger(__name__)


def csrf_token(browser: BrowserSession) -> str | None:
    """The csrf-token header for LinkedIn's API: the JSESSIONID cookie without its quotes."""
    value = browser.cookie_value(S.CSRF_COOKIE, S.COOKIE_DOMAIN)
    return value.strip('"') if value else None


def confirm_linkedin(browser: BrowserSession, spec: LoginSpec) -> LoginCheck:
    token = csrf_token(browser)
    if token is not None:
        headers = {**S.ME_API_HEADERS, "csrf-token": token}
        try:
            response = browser.api_get(S.ME_API_URL, headers)
        except Exception as error:  # noqa: BLE001 - fall through to the page check
            logger.info("LinkedIn /me call failed: %s", type(error).__name__)
        else:
            if response.status == 200 and response.json is not None:
                return LoginCheck(True, account_name=account_name_from_me(response.json))
            if spec.is_security_check(response.url):
                return LoginCheck(False, reason="a security check (/checkpoint)", security_check=True)
            logger.info("LinkedIn /me said logged out: HTTP %s %s", response.status, urlparse(response.url).path)
    # Logged out (or no csrf token yet): load /feed/ to tell a security check from a plain logout,
    # and to leave the page on screen for the diagnostics screenshot.
    result = browser.goto(spec.confirm_url, wait_for=spec.confirm_wait_for, timeout=45)
    path = urlparse(result.url).path or "/"
    if spec.is_security_check(result.url):
        return LoginCheck(False, reason=f"a security check at {path}", security_check=True)
    reason = "the /me API did not confirm a logged-in session"
    if spec.is_login_wall(result.url):
        reason = f"it redirected to {path}"
    return LoginCheck(False, reason=reason)


LINKEDIN_LOGIN = LoginSpec(
    platform=Platform.linkedin,
    display_name="LinkedIn",
    login_url=S.LOGIN_URL,
    confirm_url=S.CONFIRM_URL,
    confirm_wait_for=", ".join(S.LOGGED_IN_MARKERS + S.LOGGED_OUT_MARKERS),
    cookie_domain=S.COOKIE_DOMAIN,
    auth_cookie_names=S.AUTH_COOKIES,
    # LinkedIn sets li_at only for a logged-in session, so no li_at means logged out.
    cookie_required=True,
    logged_in_url_markers=S.LOGGED_IN_URL_MARKERS,
    login_wall_markers=S.LOGIN_WALL_MARKERS,
    security_check_markers=S.SECURITY_CHECK_MARKERS,
    page_logged_in=page_logged_in,
    account_name=account_name,
    chrome_login_cookie=chrome_login_cookie,
    confirm=confirm_linkedin,
)
