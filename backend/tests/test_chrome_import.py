"""Login import from the user's Chrome, and the strict login check. Uses synthetic Chrome cookie
databases in temporary folders and an injected password; never the real Chrome or Keychain."""

import json
import logging
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.browser.chrome_cookies import ChromeCookie, ChromeCookieReader, ChromeCookiesError
from app.browser.driver import ApiResponse
from app.models import Connection, ConnectionStatus, Platform
from app.sources.linkedin.parse import page_logged_in as linkedin_logged_in
from app.sources.naukri.parse import page_logged_in as naukri_logged_in
from app.timeutils import utcnow
from tests.chrome_fixture import TEST_PASSWORD, cookie, make_profile
from tests.conftest import make_app
from tests.fakes import page

ME_JSON = {"included": [{"$type": "com.linkedin.voyager.identity.shared.MiniProfile", "firstName": "Harsh", "lastName": "Vaghela"}]}


def me_route(*, logged_in=True):
    return [("voyager/api/me", ApiResponse(200, "https://www.linkedin.com/voyager/api/me", ME_JSON) if logged_in
             else ApiResponse(401, "https://www.linkedin.com/uas/login", None))]

SECRET = "secret-li-at-value-7f3a"
FEED_IN = page(
    "https://www.linkedin.com/feed/",
    '<nav id="global-nav" class="global-nav"><img class="global-nav__me-photo" alt="Harsh Vaghela"></nav>',
)
FEED_OUT = page(
    "https://www.linkedin.com/feed/",
    '<header><a class="nav__button-secondary" href="https://www.linkedin.com/login">Sign in</a>'
    '<a href="/signup">Join now</a></header>',
)
NAUKRI_IN = page(
    "https://www.naukri.com/mnjuser/homepage",
    '<div class="nI-gNb-header"><div class="nI-gNb-drawer"><div class="nI-gNb-drawer__icon"></div></div></div>'
    '<div class="info__heading">Harsh Vaghela</div>',
)
NAUKRI_OUT = page(  # no redirect, but a logged-out page
    "https://www.naukri.com/mnjuser/homepage",
    '<div class="nI-gNb-header"><a id="login_Layer" class="nI-gNb-lg-rg__login" title="Jobseeker Login">Login</a>'
    '<a id="register_Layer" class="nI-gNb-lg-rg__register">Register</a></div><div class="nI-gNb-drawer"></div>',
)


class CountingPassword:
    def __init__(self, password=TEST_PASSWORD, error=None):
        self.password = password
        self.error = error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.password


def linkedin_chrome(root, *, value=SECRET):
    make_profile(root, "Default", [
        cookie(".linkedin.com", "li_at", "older-profile-token", age_hours=48),
        cookie(".linkedin.com", "bcookie", "b-old", age_hours=48, http_only=False),
    ], network_dir=False)
    make_profile(root, "Profile 1", [
        cookie(".linkedin.com", "li_at", value, age_hours=0.5),
        cookie("www.linkedin.com", "JSESSIONID", "ajax:123", age_hours=0.5, same_site=0, http_only=False),
        cookie(".linkedin.com", "lang", "v=2&lang=en-us", encrypted=False, expires_in_days=None),
        cookie(".linkedin.com", "expired_one", "x", expires_in_days=-1),
        cookie(".notlinkedin.com", "li_at", "evil-1"),
        cookie("linkedin.com.evil.com", "li_at", "evil-2"),
        cookie(".google.com", "SID", "google-secret"),
    ])
    make_profile(root, "Guest Profile", [cookie(".linkedin.com", "li_at", "guest")])


# ---------------------------------------------------------------------------
# Reader


def test_profiles_reads_names_only(tmp_path):
    root = tmp_path / "Application Support" / "Google" / "Chrome"  # spaces, like the real path
    linkedin_chrome(root)
    password = CountingPassword()
    reader = ChromeCookieReader(root, password)
    profiles = reader.profiles("linkedin.com")
    assert [p.profile for p in profiles] == ["Default", "Profile 1"]  # not "Guest Profile"
    assert sorted(profiles[1].names) == ["JSESSIONID", "lang", "li_at"]  # other hosts and expired skipped
    assert profiles[1].last_access > profiles[0].last_access
    assert password.calls == 0 and not reader.has_key


def test_read_decrypts_and_caches_the_key(tmp_path):
    linkedin_chrome(tmp_path)
    password = CountingPassword()
    reader = ChromeCookieReader(tmp_path, password)
    newest = reader.profiles("linkedin.com")[1]
    cookies = {c.name: c for c in reader.read(newest, "linkedin.com")}
    assert cookies["li_at"].value == SECRET  # SHA-256 host prefix (version 24) removed
    assert cookies["JSESSIONID"].value == "ajax:123" and cookies["JSESSIONID"].host == "www.linkedin.com"
    assert cookies["lang"].value == "v=2&lang=en-us" and cookies["lang"].expires is None
    assert cookies["li_at"].expires > time.time() and cookies["li_at"].http_only
    reader.read(newest, "linkedin.com")
    assert password.calls == 1 and reader.has_key
    assert SECRET not in repr(cookies["li_at"])


def test_read_older_format_without_host_prefix(tmp_path):
    make_profile(tmp_path, "Default", [cookie(".naukri.com", "nauk_at", "jwt-token")], version=23)
    reader = ChromeCookieReader(tmp_path, CountingPassword())
    (only,) = reader.read(reader.profiles("naukri.com")[0], "naukri.com")
    assert only.value == "jwt-token"


def test_wrong_password(tmp_path):
    linkedin_chrome(tmp_path)
    reader = ChromeCookieReader(tmp_path, CountingPassword(b"wrong-password"))
    with pytest.raises(ChromeCookiesError, match="couldn't be decrypted"):
        reader.read(reader.profiles("linkedin.com")[1], "linkedin.com")
    assert not reader.has_key  # a wrong key isn't kept


def test_missing_chrome_folder(tmp_path):
    with pytest.raises(ChromeCookiesError, match="data folder wasn't found"):
        ChromeCookieReader(tmp_path / "nope", CountingPassword()).profiles("linkedin.com")


def test_playwright_cookie_format():
    domain = ChromeCookie(".linkedin.com", "li_at", "/", 2_000_000_000.0, True, True, "None", "v")
    assert domain.to_playwright() == {
        "name": "li_at", "value": "v", "httpOnly": True, "secure": True, "sameSite": "None",
        "domain": ".linkedin.com", "path": "/", "expires": 2_000_000_000.0,
    }
    host_only = ChromeCookie("www.linkedin.com", "__Host-x", "/feed", None, False, False, "None", "v")
    assert host_only.to_playwright() == {
        "name": "__Host-x", "value": "v", "httpOnly": False, "secure": False, "sameSite": "Lax",
        "url": "https://www.linkedin.com/feed",
    }


# ---------------------------------------------------------------------------
# POST /api/connections/{platform}/import


@pytest.fixture
def chrome_dir(settings):
    return settings.chrome_user_data_dir


def test_import_fast_path_not_logged_in(client, chrome_dir, fake_browser):
    make_profile(chrome_dir, "Default", [
        cookie(".naukri.com", name, "x") for name in ("HOWTORT", "J", "_t_ds", "ak_bmsc", "bm_sv")
    ])
    response = client.post("/api/connections/naukri/import")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "disconnected"
    assert data["message"] == "Not logged in to Naukri in your Chrome yet."
    assert data["login_url"] == "https://www.naukri.com/nlogin/login"
    assert fake_browser.opened == []  # nothing decrypted (the test Keychain would fail), no browser


def test_import_connects_and_never_leaks_values(client, chrome_dir, fake_browser, settings, caplog):
    linkedin_chrome(chrome_dir)
    password = CountingPassword()
    client.app.state.connections.chrome.password_provider = password
    fake_browser.cookies = [{"name": "li_at", "value": "stale", "domain": ".linkedin.com"}]
    fake_browser.api_routes = me_route()
    with caplog.at_level(logging.DEBUG):
        response = client.post("/api/connections/linkedin/import")
    data = response.json()
    assert response.status_code == 200
    assert (data["status"], data["account_name"], data["message"]) == ("connected", "Harsh Vaghela", None)
    # The newest Chrome profile's cookies replaced the old ones in the tool's own profile.
    installed = {c["name"]: c["value"] for c in fake_browser.cookies}
    assert installed["li_at"] == SECRET and "SID" not in installed and "expired_one" not in installed
    assert fake_browser.opened == [(settings.browser_profiles_dir / "linkedin", True)]
    # Values are never logged or returned; names may be logged.
    for secret in (SECRET, "ajax:123", "older-profile-token", "google-secret"):
        assert secret not in caplog.text and secret not in json.dumps(data)
    assert "li_at" in caplog.text
    # The key is cached: a second import doesn't ask the Keychain again.
    assert client.post("/api/connections/linkedin/import").json()["status"] == "connected"
    assert password.calls == 1


def test_import_unconfirmed_is_error_not_disconnected(client, chrome_dir, fake_browser):
    # The /me API says logged out although li_at was copied: `error`, so the frontend stops polling.
    linkedin_chrome(chrome_dir)
    client.app.state.connections.chrome.password_provider = CountingPassword()
    fake_browser.api_routes = me_route(logged_in=False)
    fake_browser.routes = [("/feed/", FEED_OUT)]
    data = client.post("/api/connections/linkedin/import").json()
    assert data["status"] == "error"
    assert data["message"].startswith(
        "Copied the LinkedIn cookies from Chrome, but LinkedIn didn't show a logged-in session"
    )
    assert fake_browser.screenshots  # a diagnostics screenshot was saved
    # Identical cookies aren't re-verified within 60 s...
    loads = len(fake_browser.visited)
    assert client.post("/api/connections/linkedin/import").json()["status"] == "error"
    assert len(fake_browser.visited) == loads
    # ...but a new login in Chrome is checked right away.
    linkedin_chrome(chrome_dir / "new", value="new-session")
    client.app.state.connections.chrome.user_data_dir = chrome_dir / "new"
    fake_browser.api_routes = me_route()
    assert client.post("/api/connections/linkedin/import").json()["status"] == "connected"


def test_import_security_check_is_error(client, chrome_dir, fake_browser):
    linkedin_chrome(chrome_dir)
    client.app.state.connections.chrome.password_provider = CountingPassword()
    fake_browser.api_routes = [("voyager/api/me", ApiResponse(403, "https://www.linkedin.com/checkpoint/challenge/x", None))]
    data = client.post("/api/connections/linkedin/import").json()
    assert data["status"] == "error"
    assert data["message"] == "LinkedIn wants a security check. Open linkedin.com in your Chrome, complete it, then click Connect again."


def test_import_keychain_denied(client, chrome_dir):
    linkedin_chrome(chrome_dir)
    denied = ChromeCookiesError('Access to "Chrome Safe Storage" in the macOS Keychain was denied.')
    client.app.state.connections.chrome.password_provider = CountingPassword(error=denied)
    data = client.post("/api/connections/linkedin/import").json()
    assert data["status"] == "error" and "Keychain was denied" in data["message"]


def test_import_without_chrome(client):
    """A server without the user's Chrome (no data folder): 409, push the login from the Mac."""
    response = client.post("/api/connections/linkedin/import")
    assert response.status_code == 409
    assert response.json() == {"detail": "Chrome isn't on this server: push the login from your Mac."}
    assert client.get("/api/connections").json()["items"][0]["status"] == "disconnected"  # unchanged


def test_import_off_macos(client, chrome_dir, monkeypatch):
    """Linux (Render): even with a folder at that path, Chrome's cookies can't be decrypted there."""
    linkedin_chrome(chrome_dir)
    monkeypatch.setattr("app.browser.chrome_cookies.on_macos", lambda: False)
    response = client.post("/api/connections/linkedin/import")
    assert response.status_code == 409 and "push the login from your Mac" in response.json()["detail"]


def test_import_409_while_login_window_open(client):
    assert client.post("/api/connections/naukri/connect").status_code == 202
    response = client.post("/api/connections/naukri/import")
    assert response.status_code == 409 and "login window is open" in response.json()["detail"]
    client.post("/api/connections/naukri/disconnect")


def test_silent_sync_only_with_unlocked_key(client, chrome_dir, fake_browser):
    linkedin_chrome(chrome_dir)
    connections = client.app.state.connections
    password = CountingPassword()
    connections.chrome.password_provider = password
    fake_browser.api_routes = me_route()
    connections.sync_from_chrome(Platform.linkedin)  # no key yet: nothing happens, no prompt
    assert password.calls == 0 and fake_browser.opened == []

    client.post("/api/connections/linkedin/import")
    assert password.calls == 1
    # A later Chrome login (new cookie values) is synced into the tool profile without a prompt.
    linkedin_chrome(chrome_dir / "later", value="later-token")
    connections.chrome.user_data_dir = chrome_dir / "later"
    fake_browser.cookies.clear()
    connections.sync_from_chrome(Platform.linkedin)
    assert {c["name"]: c["value"] for c in fake_browser.cookies}["li_at"] == "later-token"
    assert password.calls == 1  # key still cached


# ---------------------------------------------------------------------------
# Strict login check


def test_linkedin_page_check():
    assert linkedin_logged_in(FEED_IN.html)
    assert linkedin_logged_in('<header><a href="https://www.linkedin.com/messaging/?">Messaging</a></header>')
    assert not linkedin_logged_in(FEED_OUT.html)
    assert not linkedin_logged_in("<html><body>Loading</body></html>")
    both = FEED_IN.html + '<button>Sign in</button>'
    assert not linkedin_logged_in(both)


def test_naukri_page_check():
    assert naukri_logged_in(NAUKRI_IN.html)
    assert not naukri_logged_in(NAUKRI_OUT.html)  # a drawer, but Login/Register are shown
    assert not naukri_logged_in('<div class="nI-gNb-shimmer__lg-rg-loader"></div><div class="nI-gNb-drawer"></div>')
    assert not naukri_logged_in('<header><a href="/x">Login</a></header><div class="info__heading">Harsh</div>')


def test_startup_check_corrects_a_wrong_connected_row(settings, fake_browser):
    """The old bug: a /mnjuser/ URL that didn't redirect counted as a Naukri login."""
    (settings.browser_profiles_dir / "naukri").mkdir(parents=True)
    fake_browser.cookies = [{"name": "HOWTORT", "value": "x", "domain": ".naukri.com"}]
    fake_browser.routes = [("/mnjuser/homepage", NAUKRI_OUT)]
    with TestClient(make_app(settings, fake_browser)) as client:
        with Session(client.app.state.engine) as session:
            row = session.get(Connection, Platform.naukri)
            row.status, row.checked_at = ConnectionStatus.connected, utcnow()
            session.add(row)
            session.commit()
    with TestClient(make_app(settings, fake_browser)) as client:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            naukri = client.get("/api/connections").json()["items"][1]
            if naukri["status"] != "connected":
                break
            time.sleep(0.02)
    assert naukri["status"] == "disconnected"
    assert naukri["message"] == (
        "The saved Naukri login has expired or isn't there "
        "(/mnjuser/homepage (title '') shows no logged-in member). Connect again."
    )


def test_login_window_doesnt_trust_a_member_url(settings, fake_browser):
    """In the login window, a /mnjuser/ URL only triggers the checks; it isn't a login by itself."""
    fake_browser.routes = [("naukri.com", NAUKRI_OUT)]  # every page: /mnjuser/homepage, logged out
    app = make_app(settings.model_copy(update={"login_timeout_minutes": 0.003}), fake_browser)
    with TestClient(app) as client:
        client.post("/api/connections/naukri/connect")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            naukri = client.get("/api/connections").json()["items"][1]
            if naukri["status"] != "connecting":
                break
            time.sleep(0.02)
    assert naukri["status"] == "disconnected" and "No login was detected" in naukri["message"]
    # The checks ran once for that URL, not on every poll.
    assert fake_browser.visited.count("https://www.naukri.com/mnjuser/homepage") == 1
