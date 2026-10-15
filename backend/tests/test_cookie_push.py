"""PUT /api/connections/{platform}/cookies: a login pushed from the Mac (docs/API_CONTRACT.md,
"Deployment: login, pushing logins and data"). Synthetic cookies and the fake browser only."""

import json
import logging
import time

import pytest

from app.browser.chrome_cookies import ChromeCookie
from app.models import Platform
from app.schemas import MAX_PUSHED_COOKIES
from tests.test_chrome_import import FEED_OUT, NAUKRI_IN, NAUKRI_OUT, me_route

SECRET = "pushed-li-at-9c2e"
FUTURE = time.time() + 30 * 86400


def li(name="li_at", value=SECRET, domain=".linkedin.com", **extra):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": FUTURE,
            "httpOnly": True, "secure": True, "sameSite": "None", **extra}


def push(client, platform, cookies):
    return client.put(f"/api/connections/{platform}/cookies", json={"cookies": cookies})


def test_push_installs_only_the_platforms_cookies(client, fake_browser, settings, caplog):
    fake_browser.cookies = [{"name": "li_at", "value": "stale", "domain": ".linkedin.com"},
                            {"name": "keep", "value": "other-site", "domain": ".example.org"}]
    fake_browser.api_routes = me_route()
    cookies = [
        li(),
        li("JSESSIONID", '"ajax:42"', "www.linkedin.com", httpOnly=False, sameSite="Lax"),  # host-only
        li("lang", "v=2&lang=en-us", expires=-1),  # a session cookie
        li("old", "expired-value", expires=time.time() - 60),
        li("SID", "google-secret", ".google.com"),
        li("li_at", "evil-1", ".notlinkedin.com"),
        li("li_at", "evil-2", "linkedin.com.evil.com"),
        li("li_at", "evil-3", "evil.com/x?.linkedin.com"),
    ]
    with caplog.at_level(logging.DEBUG):
        response = push(client, "linkedin", cookies)
    assert response.status_code == 200
    data = response.json()
    assert (data["platform"], data["status"], data["account_name"], data["message"]) == (
        "linkedin", "connected", "Harsh Vaghela", None,
    )
    assert data["login_url"] == "https://www.linkedin.com/login"
    installed = {(c["name"], c["domain"]): c["value"] for c in fake_browser.cookies}
    assert installed == {
        ("keep", ".example.org"): "other-site",  # other sites' cookies in the profile are left alone
        ("li_at", ".linkedin.com"): SECRET,
        ("JSESSIONID", "www.linkedin.com"): '"ajax:42"',
        ("lang", ".linkedin.com"): "v=2&lang=en-us",
    }
    # Checked headless on the platform's own profile, like an import.
    assert fake_browser.opened == [(settings.browser_profiles_dir / "linkedin", True)]
    for secret in (SECRET, "ajax:42", "google-secret", "evil-1", "expired-value"):
        assert secret not in caplog.text and secret not in json.dumps(data)
    assert "li_at" in caplog.text  # names may be logged


def test_push_works_without_chrome_on_the_server(client, fake_browser, monkeypatch):
    monkeypatch.setattr("app.browser.chrome_cookies.on_macos", lambda: False)
    fake_browser.routes = [("/mnjuser/homepage", NAUKRI_IN)]
    response = push(client, "naukri", [li("nauk_at", "jwt-token", ".naukri.com"), li("_t_ds", "x", ".naukri.com")])
    assert response.status_code == 200
    assert response.json()["status"] == "connected" and response.json()["account_name"] == "Harsh Vaghela"
    assert client.post("/api/connections/naukri/import").status_code == 409  # Chrome isn't here


def test_push_that_doesnt_log_in_is_an_error(client, fake_browser):
    fake_browser.api_routes = me_route(logged_in=False)
    fake_browser.routes = [("/feed/", FEED_OUT)]
    data = push(client, "linkedin", [li()]).json()
    assert data["status"] == "error"
    assert data["message"].startswith(
        "Installed the LinkedIn login pushed from your Mac, but LinkedIn didn't show a logged-in session"
    )
    assert data["message"].endswith("log in, and push the login again.")
    assert fake_browser.screenshots  # diagnostics saved, under DIAGNOSTICS_DIR

    fake_browser.routes = [("/mnjuser/homepage", NAUKRI_OUT)]
    assert push(client, "naukri", [li("nauk_at", "jwt", ".naukri.com")]).json()["status"] == "error"


def test_push_without_a_login_cookie_changes_nothing(client, fake_browser):
    fake_browser.cookies = [{"name": "li_at", "value": "working", "domain": ".linkedin.com"}]
    for cookies in ([li("bcookie", "b")], [li("SID", "x", ".google.com")], []):
        response = push(client, "linkedin", cookies)
        assert response.status_code == 422
        assert response.json()["detail"] == (
            "No LinkedIn login among the cookies. Log in to LinkedIn in Chrome on your Mac, then push again."
        )
    assert fake_browser.opened == [] and fake_browser.cookies[0]["value"] == "working"
    # A LinkedIn login doesn't count for Naukri.
    assert push(client, "naukri", [li()]).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"cookies": [li()] * (MAX_PUSHED_COOKIES + 1)},
        {"cookies": [{"value": "x", "domain": ".linkedin.com"}]},  # no name
        {"cookies": [li(sameSite="Sometimes")]},
        {"cookies": [li(path="feed")]},
        {"cookies": [li(name="li at")]},
        {"cookies": [li(value="x" * 9000)]},
        {"cookies": "li_at=x"},
        {},
    ],
)
def test_push_validation(client, fake_browser, body):
    assert client.put("/api/connections/linkedin/cookies", json=body).status_code == 422
    assert fake_browser.opened == []


def test_push_unknown_platform(client):
    assert push(client, "indeed", [li()]).status_code == 422


def test_push_409_while_login_window_open_or_busy(client, fake_browser):
    assert client.post("/api/connections/naukri/connect").status_code == 202
    response = push(client, "naukri", [li("nauk_at", "jwt", ".naukri.com")])
    assert response.status_code == 409 and "login window is open" in response.json()["detail"]
    client.post("/api/connections/naukri/disconnect")

    lock = client.app.state.connections._imports[Platform.linkedin]
    assert lock.acquire(blocking=False)  # an import or push of LinkedIn is running
    try:
        response = push(client, "linkedin", [li()])
        assert response.status_code == 409
        assert response.json()["detail"] == "A LinkedIn login check is already running. Try again in a minute."
    finally:
        lock.release()
    fake_browser.api_routes = me_route()
    assert push(client, "linkedin", [li(), li("JSESSIONID", '"ajax:1"')]).json()["status"] == "connected"
    assert lock.acquire(blocking=False)  # released after the check
    lock.release()


def test_storage_cookie_round_trip():
    domain = ChromeCookie(".linkedin.com", "li_at", "/", 2_000_000_000.0, True, True, "None", "v")
    host_only = ChromeCookie("www.linkedin.com", "JSESSIONID", "/feed", None, False, False, "Lax", "w")
    assert domain.to_storage_cookie() == {
        "name": "li_at", "value": "v", "domain": ".linkedin.com", "path": "/", "expires": 2_000_000_000.0,
        "httpOnly": True, "secure": True, "sameSite": "None",
    }
    assert host_only.to_storage_cookie()["expires"] == -1
    for cookie in (domain, host_only):
        shape = cookie.to_storage_cookie()
        back = ChromeCookie.from_storage_cookie(
            name=shape["name"], value=shape["value"], domain=shape["domain"], path=shape["path"],
            expires=shape["expires"], http_only=shape["httpOnly"], secure=shape["secure"], same_site=shape["sameSite"],
        )
        assert back == cookie and back.value == cookie.value
