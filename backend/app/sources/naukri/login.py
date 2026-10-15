"""Recognising a Naukri login."""

from app.browser.login import LoginSpec
from app.models import Platform
from app.sources.naukri import selectors as S
from app.sources.naukri.parse import account_name, chrome_login_cookie, page_logged_in

NAUKRI_LOGIN = LoginSpec(
    platform=Platform.naukri,
    display_name="Naukri",
    login_url=S.LOGIN_URL,
    confirm_url=S.CONFIRM_URL,
    confirm_wait_for=", ".join(S.LOGGED_IN_MARKERS + S.LOGGED_OUT_MARKERS),
    cookie_domain=S.COOKIE_DOMAIN,
    auth_cookie_names=S.AUTH_COOKIES,
    # Naukri's cookie names aren't verified, so the page check decides.
    cookie_required=False,
    logged_in_url_markers=S.LOGGED_IN_URL_MARKERS,
    login_wall_markers=S.LOGIN_WALL_MARKERS,
    security_check_markers=(),  # Naukri's check relies on the member page below
    page_logged_in=page_logged_in,
    account_name=account_name,
    chrome_login_cookie=chrome_login_cookie,
)
