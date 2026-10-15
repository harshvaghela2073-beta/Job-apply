import time
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.browser import BrowserUnavailable
from app.browser.driver import ApiResponse
from app.models import Connection, ConnectionStatus, Platform
from app.timeutils import utcnow
from tests.conftest import make_app
from tests.fakes import page

LI_AT = {"name": "li_at", "value": "token-1", "domain": ".www.linkedin.com"}
JSESSION = {"name": "JSESSIONID", "value": '"ajax:99"', "domain": ".www.linkedin.com"}
FEED = page("https://www.linkedin.com/feed/", "<title>Feed | LinkedIn</title>")
ME_JSON = {"included": [{"$type": "com.linkedin.voyager.identity.shared.MiniProfile", "firstName": "Harsh", "lastName": "Vaghela"}]}


def me_when_li_at(fake_browser):
    """voyager /me: 200 while an li_at cookie is present, else 401 (redirect to login)."""
    def route(url, headers):
        if any(c["name"] == "li_at" for c in fake_browser.cookies):
            return ApiResponse(200, url, ME_JSON)
        return ApiResponse(401, "https://www.linkedin.com/uas/login", None)
    return [("voyager/api/me", route)]


def wait_for(client, platform, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        rows = {row["platform"]: row for row in client.get("/api/connections").json()["items"]}
        if predicate(rows[platform]) or time.monotonic() > deadline:
            return rows[platform]
        time.sleep(0.02)


def test_list_connections(client):
    data = client.get("/api/connections").json()
    assert data["total"] == 2
    assert [row["platform"] for row in data["items"]] == ["linkedin", "naukri"]
    assert data["items"][0] == {
        "platform": "linkedin", "status": "disconnected", "account_name": None, "checked_at": None, "message": None,
        "login_url": "https://www.linkedin.com/login",
    }
    assert data["items"][1]["login_url"] == "https://www.naukri.com/nlogin/login"


def test_connect_detects_login(client, fake_browser, settings):
    fake_browser.cookies = [JSESSION]
    fake_browser.login_cookie = LI_AT
    fake_browser.login_after_polls = 3
    fake_browser.api_routes = me_when_li_at(fake_browser)
    fake_browser.routes = [("/feed/", FEED)]
    response = client.post("/api/connections/linkedin/connect")
    assert response.status_code == 202
    assert response.json()["status"] == "connecting"
    assert "Chrome window" in response.json()["message"]

    row = wait_for(client, "linkedin", lambda r: r["status"] != "connecting")
    assert row["status"] == "connected" and row["account_name"] == "Harsh Vaghela"
    assert row["message"] is None and row["checked_at"] is not None
    # A visible window on the platform's own profile, the login page first.
    assert fake_browser.opened[0] == (settings.browser_profiles_dir / "linkedin", False)
    assert fake_browser.visited[0] == "https://www.linkedin.com/login"
    assert any(v.startswith("API https://www.linkedin.com/voyager/api/me") for v in fake_browser.visited)


def test_connect_ignores_stale_cookie_until_it_changes(client, fake_browser):
    fake_browser.cookies = [JSESSION, dict(LI_AT, value="expired")]
    state = {"logged_in": False}

    def me(url, headers):
        return ApiResponse(200, url, ME_JSON) if state["logged_in"] else ApiResponse(401, "https://www.linkedin.com/uas/login", None)

    fake_browser.api_routes = [("voyager/api/me", me)]
    fake_browser.routes = [("/feed/", page("https://www.linkedin.com/uas/login?redirect=feed"))]
    client.post("/api/connections/linkedin/connect")
    time.sleep(0.2)
    assert wait_for(client, "linkedin", lambda r: False, timeout=0)["status"] == "connecting"

    state["logged_in"] = True
    fake_browser.cookies = [JSESSION, LI_AT]  # a fresh login cookie after the user signs in
    assert wait_for(client, "linkedin", lambda r: r["status"] == "connected")["status"] == "connected"


def test_connect_naukri_with_its_own_spec(client, fake_browser):
    fake_browser.login_cookie = {"name": "nauk_at", "value": "abc", "domain": ".naukri.com"}
    fake_browser.login_after_polls = 2
    fake_browser.routes = [
        ("/mnjuser/homepage", page("https://www.naukri.com/mnjuser/homepage", '<div class="info__heading">Harsh Vaghela</div>')),
    ]
    client.post("/api/connections/naukri/connect")
    row = wait_for(client, "naukri", lambda r: r["status"] != "connecting")
    assert (row["status"], row["account_name"]) == ("connected", "Harsh Vaghela")
    assert fake_browser.visited[0] == "https://www.naukri.com/nlogin/login"


def test_connect_409_while_connecting_and_disconnect_cancels(client, fake_browser, settings):
    assert client.post("/api/connections/linkedin/connect").status_code == 202
    second = client.post("/api/connections/linkedin/connect")
    assert second.status_code == 409 and "already open" in second.json()["detail"]
    # The other platform is independent.
    assert client.post("/api/connections/naukri/connect").status_code == 202

    response = client.post("/api/connections/linkedin/disconnect")
    assert response.status_code == 200 and response.json()["status"] == "disconnected"
    assert not (settings.browser_profiles_dir / "linkedin").exists()
    time.sleep(0.1)
    assert wait_for(client, "linkedin", lambda r: False, timeout=0)["status"] == "disconnected"
    # A new login can start again.
    assert client.post("/api/connections/linkedin/connect").status_code == 202
    client.post("/api/connections/linkedin/disconnect")
    client.post("/api/connections/naukri/disconnect")


def test_connect_timeout(settings, fake_browser):
    app = make_app(settings.model_copy(update={"login_timeout_minutes": 0.001}), fake_browser)
    with TestClient(app) as client:
        client.post("/api/connections/linkedin/connect")
        row = wait_for(client, "linkedin", lambda r: r["status"] != "connecting")
    assert row["status"] == "disconnected"
    assert row["message"] == "No login was detected within 0.001 minutes, so the window was closed."


def test_window_closed_before_login(client, fake_browser):
    fake_browser.close_after_polls = 2
    client.post("/api/connections/linkedin/connect")
    row = wait_for(client, "linkedin", lambda r: r["status"] != "connecting")
    assert row["status"] == "disconnected"
    assert row["message"] == "The login window was closed before the login finished."


def test_window_closed_after_login_is_confirmed_headless(client, fake_browser):
    fake_browser.cookies = [JSESSION, LI_AT]
    fake_browser.close_after_polls = 1
    fake_browser.api_routes = me_when_li_at(fake_browser)
    fake_browser.routes = [("/feed/", FEED)]
    client.post("/api/connections/linkedin/connect")
    row = wait_for(client, "linkedin", lambda r: r["status"] != "connecting")
    assert row["status"] == "connected"
    assert [headless for _, headless in fake_browser.opened] == [False, True]


def test_browser_unavailable_sets_error(client, fake_browser):
    fake_browser.open_error = BrowserUnavailable("Google Chrome was not found. Install Google Chrome.")
    client.post("/api/connections/linkedin/connect")
    row = wait_for(client, "linkedin", lambda r: r["status"] != "connecting")
    assert row["status"] == "error" and row["message"] == "Google Chrome was not found. Install Google Chrome."


def test_disconnect_deletes_profile(client, settings):
    profile = settings.browser_profiles_dir / "naukri"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_text("x")
    other = settings.browser_profiles_dir / "linkedin"
    other.mkdir(parents=True)
    data = client.post("/api/connections/naukri/disconnect").json()
    assert data["status"] == "disconnected" and data["account_name"] is None
    assert not profile.exists() and other.exists()


def test_unknown_platform(client):
    assert client.post("/api/connections/indeed/connect").status_code == 422


# ---------------------------------------------------------------------------
# Status checks


def test_startup_check(settings, fake_browser):
    (settings.browser_profiles_dir / "linkedin").mkdir(parents=True)
    fake_browser.cookies = [JSESSION, LI_AT]
    fake_browser.api_routes = me_when_li_at(fake_browser)
    fake_browser.routes = [("/feed/", FEED)]
    with TestClient(make_app(settings, fake_browser)) as client:
        row = wait_for(client, "linkedin", lambda r: r["status"] == "connected")
        assert row["account_name"] == "Harsh Vaghela"
        # Naukri has no profile: no browser is opened for it.
        assert wait_for(client, "naukri", lambda r: True)["status"] == "disconnected"
    assert fake_browser.opened == [(settings.browser_profiles_dir / "linkedin", True)]


def test_startup_check_expired_and_recent(settings, fake_browser):
    (settings.browser_profiles_dir / "linkedin").mkdir(parents=True)
    with TestClient(make_app(settings, fake_browser)) as client:  # no li_at cookie
        row = wait_for(client, "linkedin", lambda r: r["status"] == "disconnected" and r["message"])
        assert row["message"] == "The saved LinkedIn login has expired or isn't there (no li_at cookie). Connect again."
    assert fake_browser.visited == []  # no page load needed without li_at
    # Checked moments ago: a restart doesn't check again.
    opened = len(fake_browser.opened)
    with TestClient(make_app(settings, fake_browser)):
        time.sleep(0.1)
    assert len(fake_browser.opened) == opened


def test_startup_resets_stale_connecting(settings, fake_browser):
    with TestClient(make_app(settings, fake_browser)) as client:
        with Session(client.app.state.engine) as session:
            row = session.get(Connection, Platform.naukri)
            row.status = ConnectionStatus.connecting
            session.add(row)
            session.commit()
    with TestClient(make_app(settings, fake_browser)) as client:
        row = wait_for(client, "naukri", lambda r: True)
    assert row["status"] == "disconnected"
    assert row["message"] == "The backend restarted before the login finished."


def test_scan_reports_update_status(client, settings):
    connections = client.app.state.connections
    (settings.browser_profiles_dir / "linkedin").mkdir(parents=True)
    assert not connections.login_usable(Platform.linkedin)  # disconnected
    with Session(client.app.state.engine) as session:
        row = session.get(Connection, Platform.linkedin)
        row.status = ConnectionStatus.connected
        row.checked_at = utcnow() - timedelta(hours=1)
        session.add(row)
        session.commit()
    assert connections.login_usable(Platform.linkedin)
    connections.record_login_ok(Platform.linkedin)
    assert connections.get(Platform.linkedin).checked_at > utcnow() - timedelta(minutes=1)
    connections.record_login_lost(Platform.linkedin, "The saved LinkedIn login has expired. Connect again.")
    row = connections.get(Platform.linkedin)
    assert row.status == ConnectionStatus.disconnected and "expired" in row.message
    assert not connections.login_usable(Platform.linkedin)
    connections.record_login_ok(Platform.linkedin)  # doesn't resurrect a disconnected login
    assert connections.get(Platform.linkedin).status == ConnectionStatus.disconnected
