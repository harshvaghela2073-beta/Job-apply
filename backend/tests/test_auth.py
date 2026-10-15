"""App login (docs/API_CONTRACT.md, "Deployment: login, pushing logins and data")."""

import base64
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.services.auth import BLOCK_SECONDS, FAILURE_WINDOW_SECONDS, AuthService, LoginLimiter
from tests.conftest import make_app

PASSWORD = "correct horse battery staple"
ORIGIN = "http://localhost:5173"


def with_login(settings: Settings, password: str = PASSWORD, secret: str = "") -> Settings:
    return settings.model_copy(update={"app_password": SecretStr(password), "auth_secret": SecretStr(secret)})


@pytest.fixture
def auth_client(settings, fake_browser):
    with TestClient(make_app(with_login(settings), fake_browser)) as client:
        yield client


def login(client, password=PASSWORD, **kwargs):
    return client.post("/api/auth/login", json={"password": password}, **kwargs)


def token_for(client) -> str:
    response = login(client)
    assert response.status_code == 200
    return response.json()["token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# No APP_PASSWORD: nothing changes


def test_no_login_by_default(client):
    assert client.get("/api/auth/status").json() == {"required": False, "authenticated": True}
    assert client.get("/api/jobs").status_code == 200
    response = login(client, "anything")
    assert response.status_code == 400 and response.json()["detail"] == "This server doesn't need a login."


def test_settings_read_the_password_from_the_environment(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("AUTH_SECRET", "s3cret")
    settings = Settings(_env_file=None)
    assert settings.app_password.get_secret_value() == PASSWORD
    assert settings.auth_secret.get_secret_value() == "s3cret"
    assert PASSWORD not in repr(settings) and "s3cret" not in repr(settings)


# ---------------------------------------------------------------------------
# Login and the guard


def test_login_flow(auth_client, caplog):
    assert auth_client.get("/api/auth/status").json() == {"required": True, "authenticated": False}
    response = auth_client.get("/api/jobs")
    assert response.status_code == 401
    assert response.json() == {"detail": "Login required."}
    assert response.headers["www-authenticate"] == "Bearer"

    wrong = login(auth_client, "nope")
    assert wrong.status_code == 401 and wrong.json() == {"detail": "Wrong password."}

    with caplog.at_level(logging.DEBUG):
        response = login(auth_client)
    assert response.status_code == 200
    data = response.json()
    expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    assert data["expires_at"].endswith("Z")
    assert abs(expires_at - (datetime.now(UTC) + timedelta(days=30))) < timedelta(minutes=1)
    token = data["token"]
    assert PASSWORD not in caplog.text and token not in caplog.text

    assert auth_client.get("/api/jobs", headers=bearer(token)).status_code == 200
    assert auth_client.get("/api/jobs", headers={"Authorization": f"bearer {token}"}).status_code == 200
    assert auth_client.get("/api/auth/status", headers=bearer(token)).json() == {"required": True, "authenticated": True}
    # Writes are guarded too.
    assert auth_client.put("/api/settings", json={}).status_code == 401
    assert auth_client.put("/api/settings", json={}, headers=bearer(token)).status_code == 200


@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Bearer ", "Basic dXNlcjpwYXNz", "Bearer not-a-token", "Bearer a.b", "Bearer ..", "Bearer " + "x" * 600],
)
def test_bad_authorization_headers(auth_client, header):
    headers = {"Authorization": header} if header else {}
    assert auth_client.get("/api/jobs", headers=headers).status_code == 401


def test_public_paths(auth_client):
    assert auth_client.get("/api/health").json() == {"status": "ok"}
    assert auth_client.get("/api/auth/status").status_code == 200
    # A CORS preflight needs no token (browsers never send one with it).
    preflight = auth_client.options(
        "/api/jobs",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"},
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == ORIGIN
    assert "authorization" in preflight.headers["access-control-allow-headers"].lower()
    # Everything else under /api is guarded, unknown paths included.
    for path in ("/api/dashboard", "/api/connections", "/api/profile/resume", "/api/nope", "/api/healthz", "/api/authx"):
        assert auth_client.get(path).status_code == 401, path


def test_401_carries_cors_headers(auth_client):
    response = auth_client.get("/api/jobs", headers={"Origin": ORIGIN})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == ORIGIN
    other = auth_client.get("/api/jobs", headers={"Origin": "https://evil.example"})
    assert other.status_code == 401 and "access-control-allow-origin" not in other.headers


# ---------------------------------------------------------------------------
# Tokens


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_token_tampering_and_expiry(auth_client):
    token = token_for(auth_client)
    payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    longer = _b64(json.dumps({"exp": claims["exp"] + 10**6}).encode())
    flipped = signature[:-2] + ("A" if signature[-2] != "A" else "B") + signature[-1]
    for forged in (f"{longer}.{signature}", f"{payload}.{flipped}", f"{payload}.", f".{signature}", f"{payload}.{signature}.x"):
        assert auth_client.get("/api/jobs", headers=bearer(forged)).status_code == 401, forged

    auth = auth_client.app.state.auth
    auth.clock = lambda: time.time() + timedelta(days=30, minutes=1).total_seconds()
    assert auth_client.get("/api/jobs", headers=bearer(token)).status_code == 401
    auth.clock = lambda: time.time() + timedelta(days=29).total_seconds()
    assert auth_client.get("/api/jobs", headers=bearer(token)).status_code == 200


def test_changing_the_password_or_secret_logs_everyone_out(settings):
    auth = AuthService(with_login(settings))
    token, _ = auth.issue()
    assert auth.verify(token)
    assert AuthService(with_login(settings)).verify(token)  # same settings (a restart): still valid
    assert not AuthService(with_login(settings, password="new password")).verify(token)
    assert not AuthService(with_login(settings, secret="a-new-secret")).verify(token)
    with_secret = AuthService(with_login(settings, secret="secret-1"))
    token, _ = with_secret.issue()
    assert not AuthService(with_login(settings, secret="secret-2")).verify(token)
    assert not AuthService(settings).verify(token)  # login switched off: no token is valid (none is needed)
    assert AuthService(settings).authorized(None)


# ---------------------------------------------------------------------------
# Wrong-password limit


def test_five_wrong_passwords_block_that_address(auth_client):
    for _ in range(5):
        assert login(auth_client, "wrong").status_code == 401
    blocked = login(auth_client)  # even the right password
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "Too many wrong passwords. Try again in 15 minutes."
    assert 0 < int(blocked.headers["retry-after"]) <= BLOCK_SECONDS
    # Another address isn't affected; requests with a valid token still work.
    token = login(auth_client, headers={"X-Forwarded-For": "203.0.113.9"}).json()["token"]
    assert auth_client.get("/api/jobs", headers=bearer(token)).status_code == 200
    # After 15 minutes the address may try again.
    limiter = auth_client.app.state.auth.limiter
    limiter.clock = lambda: time.monotonic() + BLOCK_SECONDS + 1
    assert login(auth_client).status_code == 200


def test_forwarded_for_last_entry_is_the_client(auth_client):
    """The proxy appends the address it saw; entries before it come from the client and can be made up."""
    faked = [{"X-Forwarded-For": f"10.9.9.{i}, 198.51.100.7"} for i in range(5)]
    for headers in faked:
        assert login(auth_client, "wrong", headers=headers).status_code == 401
    assert login(auth_client, headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 429
    # Other clients and the TCP peer aren't blocked.
    assert login(auth_client).status_code == 200
    assert login(auth_client, headers={"X-Forwarded-For": "198.51.100.8"}).status_code == 200


def test_many_addresses_together_block_every_login(auth_client):
    from app.services.auth import MAX_FAILURES_ALL

    for i in range(MAX_FAILURES_ALL):
        assert login(auth_client, "wrong", headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code == 401
    assert login(auth_client, headers={"X-Forwarded-For": "198.51.100.99"}).status_code == 429


def test_a_right_password_resets_the_count(auth_client):
    for _ in range(4):
        assert login(auth_client, "wrong").status_code == 401
    assert login(auth_client).status_code == 200
    for _ in range(4):
        assert login(auth_client, "wrong").status_code == 401
    assert login(auth_client).status_code == 200


def test_limiter_window():
    now = [1000.0]
    limiter = LoginLimiter(clock=lambda: now[0])
    for _ in range(4):
        limiter.failed("a")
    now[0] += FAILURE_WINDOW_SECONDS  # those four are older than 15 minutes now
    limiter.failed("a")
    assert limiter.retry_after("a") == 0
    for _ in range(4):
        limiter.failed("a")
    assert limiter.retry_after("a") == BLOCK_SECONDS
    now[0] += BLOCK_SECONDS - 10
    assert limiter.retry_after("a") == 10
    now[0] += 10
    assert limiter.retry_after("a") == 0 and limiter.retry_after("b") == 0
