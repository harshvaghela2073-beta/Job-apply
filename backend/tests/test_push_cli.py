"""``python -m app.tools.push`` against the in-process test app (an injected transport; never a
network). Synthetic Chrome cookie databases and an injected Keychain password only."""

import io
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlmodel import Session

from app.browser.chrome_cookies import ChromeCookieReader
from app.db import create_db_engine, init_db
from app.tools import push
from app.tools.push import PushError, server_base
from tests.chrome_fixture import cookie, make_profile
from tests.conftest import make_app
from tests.factories import make_field, make_job, make_template
from tests.test_admin_uploads import make_db
from tests.test_chrome_import import NAUKRI_IN, SECRET, CountingPassword, linkedin_chrome, me_route

SERVER = "https://jobapply.example"
PASSWORD = "app-password-for-cli-tests"
NEW_PDF = b"%PDF-1.7\n% pushed resume\n%%EOF\n"


class AppTransport:
    """Sends the CLI's requests to the TestClient instead of the network."""

    def __init__(self, client: TestClient, *, drop_auth: bool = False):
        self.client = client
        self.drop_auth = drop_auth
        self.calls: list[tuple[str, str, bool]] = []

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == SERVER
        if self.drop_auth:
            headers = {k: v for k, v in headers.items() if k != "Authorization"}
        self.calls.append((method, parts.path, "Authorization" in headers))
        response = self.client.request(method, parts.path, headers=headers, content=body)
        return response.status_code, response.content


def no_prompt(prompt):
    raise AssertionError("the password was asked for although JOB_APPLY_PASSWORD is set")


@pytest.fixture
def server(settings, fake_browser):
    """The deployed backend: login required, no Chrome."""
    app = make_app(settings.model_copy(update={"app_password": SecretStr(PASSWORD)}), fake_browser)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def mac_settings(settings, tmp_path):
    """The Mac's own settings (its resume, database and Chrome)."""
    return settings.model_copy(update={
        "resume_path": tmp_path / "mac" / "Resume.pdf",
        "database_url": f"sqlite:///{tmp_path / 'mac' / 'jobapply.db'}",
        "chrome_user_data_dir": tmp_path / "mac-chrome",
    })


def run(argv, client, mac_settings, *, reader=None, ask=no_prompt, transport=None):
    out = io.StringIO()
    transport = transport or AppTransport(client)
    code = push.main(
        ["--server", SERVER, *argv], settings=mac_settings, transport=transport, reader=reader, ask_password=ask, out=out,
    )
    return code, out.getvalue(), transport


def mac_chrome(root):
    linkedin_chrome(root)  # the newest logged-in profile ("Profile 1") holds SECRET
    make_profile(root, "Profile 2", [cookie(".naukri.com", "nauk_at", "naukri-jwt-secret"),
                                     cookie(".naukri.com", "_t_ds", "x")])


def test_login_all(server, mac_settings, fake_browser, monkeypatch):
    monkeypatch.setenv("JOB_APPLY_PASSWORD", PASSWORD)
    mac_chrome(mac_settings.chrome_user_data_dir)
    keychain = CountingPassword()
    reader = ChromeCookieReader(mac_settings.chrome_user_data_dir, keychain)
    fake_browser.api_routes = me_route()
    fake_browser.routes = [("/mnjuser/homepage", NAUKRI_IN)]
    code, out, transport = run(["login", "all"], server, mac_settings, reader=reader)
    assert code == 0, out
    assert out.splitlines() == [
        "Logged in to the server.",
        "LinkedIn: sending 3 cookies from Chrome profile 'Profile 1'...",
        "LinkedIn: connected as Harsh Vaghela.",
        "Naukri: sending 2 cookies from Chrome profile 'Profile 2'...",
        "Naukri: connected as Harsh Vaghela.",
    ]
    assert transport.calls == [
        ("GET", "/api/auth/status", False),
        ("POST", "/api/auth/login", False),
        ("PUT", "/api/connections/linkedin/cookies", True),
        ("PUT", "/api/connections/naukri/cookies", True),
    ]
    # The same profile Connect picks: the newest logged-in one, not "Default" (older token).
    installed = {c["name"]: c["value"] for c in fake_browser.cookies}
    assert installed["li_at"] == SECRET and installed["nauk_at"] == "naukri-jwt-secret"
    assert keychain.calls == 1
    for secret in (SECRET, "naukri-jwt-secret", "ajax:123", PASSWORD):
        assert secret not in out
    token = server.post("/api/auth/login", json={"password": PASSWORD}).json()["token"]
    rows = server.get("/api/connections", headers={"Authorization": f"Bearer {token}"}).json()["items"]
    assert [(row["platform"], row["status"]) for row in rows] == [("linkedin", "connected"), ("naukri", "connected")]


def test_login_one_platform_asks_for_the_password(server, mac_settings, fake_browser):
    linkedin_chrome(mac_settings.chrome_user_data_dir)
    fake_browser.api_routes = me_route()
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return PASSWORD

    reader = ChromeCookieReader(mac_settings.chrome_user_data_dir, CountingPassword())
    code, out, transport = run(["login", "linkedin"], server, mac_settings, reader=reader, ask=ask)
    assert code == 0 and out.splitlines()[-1] == "LinkedIn: connected as Harsh Vaghela."
    assert prompts == [f"App password for {SERVER}: "]
    assert [path for _, path, _ in transport.calls][-1] == "/api/connections/linkedin/cookies"


def test_login_not_logged_in_or_failing(server, mac_settings, fake_browser, monkeypatch):
    monkeypatch.setenv("JOB_APPLY_PASSWORD", PASSWORD)
    make_profile(mac_settings.chrome_user_data_dir, "Default", [cookie(".linkedin.com", "bcookie", "b")])
    reader = ChromeCookieReader(mac_settings.chrome_user_data_dir, CountingPassword())
    code, out, transport = run(["login", "linkedin"], server, mac_settings, reader=reader)
    assert code == 1
    assert "LinkedIn: not logged in to LinkedIn in Chrome on this Mac." in out
    assert not any(method == "PUT" for method, _, _ in transport.calls)

    linkedin_chrome(mac_settings.chrome_user_data_dir / "other")
    reader = ChromeCookieReader(mac_settings.chrome_user_data_dir / "other", CountingPassword())
    fake_browser.api_routes = me_route(logged_in=False)
    code, out, _ = run(["login", "linkedin"], server, mac_settings, reader=reader)
    assert code == 1
    assert "LinkedIn: error - Installed the LinkedIn login pushed from your Mac, but" in out
    assert SECRET not in out


def test_login_needs_chrome(server, mac_settings, monkeypatch):
    monkeypatch.setenv("JOB_APPLY_PASSWORD", PASSWORD)
    code, out, _ = run(["login", "all"], server, mac_settings)  # no Chrome folder on this "Mac"
    assert code == 1 and out.startswith("Logged in to the server.\nError: Google Chrome's data wasn't found at ")


def test_wrong_password_and_lost_token(server, mac_settings, monkeypatch):
    mac_settings.resume_path.parent.mkdir(parents=True)
    mac_settings.resume_path.write_bytes(NEW_PDF)
    code, out, transport = run(["resume"], server, mac_settings, ask=lambda prompt: "wrong")
    assert code == 1 and out == "Error: Wrong app password.\n"
    assert "wrong" not in out.replace("Wrong", "")

    monkeypatch.setenv("JOB_APPLY_PASSWORD", PASSWORD)
    code, out, _ = run(["resume"], server, mac_settings, transport=AppTransport(server, drop_auth=True))
    assert code == 1 and "The server refused the login (Login required.)" in out


def test_resume(server, mac_settings, resume_path, monkeypatch):
    monkeypatch.setenv("JOB_APPLY_PASSWORD", PASSWORD)
    mac_settings.resume_path.parent.mkdir(parents=True)
    mac_settings.resume_path.write_bytes(NEW_PDF)
    code, out, _ = run(["resume"], server, mac_settings)  # default: the Mac's RESUME_PATH
    assert code == 0 and out.splitlines()[-1] == f"Resume uploaded ({len(NEW_PDF)} bytes)."
    assert resume_path.read_bytes() == NEW_PDF  # the server's RESUME_PATH

    other = mac_settings.resume_path.with_name("Other.pdf")
    other.write_bytes(b"%PDF-1.4 other")
    assert run(["resume", str(other)], server, mac_settings)[0] == 0
    assert resume_path.read_bytes() == b"%PDF-1.4 other"

    not_pdf = mac_settings.resume_path.with_name("notes.txt")
    not_pdf.write_text("hello")
    code, out, transport = run(["resume", str(not_pdf)], server, mac_settings)
    assert code == 1 and out.endswith("Error: notes.txt isn't a PDF file.\n")
    assert not any(path == "/api/admin/resume" for _, path, _ in transport.calls)
    code, out, _ = run(["resume", str(not_pdf.with_name("missing.pdf"))], server, mac_settings)
    assert code == 1 and "No resume at" in out


def test_database_snapshot_while_the_local_backend_writes(server, mac_settings, monkeypatch):
    """The local database is open and in WAL mode (the Mac's backend running): the snapshot still
    has every committed row."""
    monkeypatch.setenv("JOB_APPLY_PASSWORD", PASSWORD)
    engine = create_db_engine(mac_settings.database_url)
    init_db(engine)
    with Session(engine) as session:  # pooled connections stay open, so these rows sit in the WAL
        for n in range(4):
            make_job(session, title=f"Mac job {n}", external_id=f"mac-{n}")
        make_template(session)
        make_field(session)
    try:
        code, out, _ = run(["database"], server, mac_settings)
    finally:
        engine.dispose()
    assert code == 0, out
    assert out.splitlines()[-1] == "Database uploaded: 4 jobs, 1 templates, 1 profile fields."
    token = server.post("/api/auth/login", json={"password": PASSWORD}).json()["token"]
    jobs = server.get("/api/jobs", headers={"Authorization": f"Bearer {token}"}).json()
    assert jobs["total"] == 4


def test_database_from_a_path(server, mac_settings, tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_APPLY_PASSWORD", PASSWORD)
    path = tmp_path / "copy.db"
    make_db(path, jobs=2, templates=1, fields=0)
    code, out, _ = run(["database", str(path)], server, mac_settings)
    assert code == 0 and out.splitlines()[-1] == "Database uploaded: 2 jobs, 1 templates, 0 profile fields."
    code, out, transport = run(["database", str(tmp_path / "missing.db")], server, mac_settings)
    assert code == 1 and "No database at" in out
    assert not (tmp_path / "missing.db").exists()  # never created by the snapshot


def test_no_login_needed(client, mac_settings, resume_path):
    """A server without APP_PASSWORD (e.g. the local one): no password, no login call."""
    mac_settings.resume_path.parent.mkdir(parents=True)
    mac_settings.resume_path.write_bytes(NEW_PDF)
    code, out, transport = run(["resume"], client, mac_settings)
    assert code == 0 and out == f"Resume uploaded ({len(NEW_PDF)} bytes).\n"
    assert [path for _, path, _ in transport.calls] == ["/api/auth/status", "/api/admin/resume"]


def test_server_url_rules(client, mac_settings):
    assert server_base("https://job-apply.onrender.com/") == "https://job-apply.onrender.com"
    assert server_base("https://example.com/backend/") == "https://example.com/backend"
    assert server_base("http://localhost:8000") == "http://localhost:8000"
    assert server_base("http://127.0.0.1:9000/") == "http://127.0.0.1:9000"
    for bad in ("http://job-apply.onrender.com", "ftp://example.com", "example.com", "https://", "https://u:p@example.com",
                "https://example.com/?x=1"):
        with pytest.raises(PushError):
            server_base(bad)

    def never(*args):
        raise AssertionError("no request may be sent to a plain-http server")

    out = io.StringIO()
    code = push.main(["--server", "http://job-apply.onrender.com", "resume"], settings=mac_settings, transport=never, out=out)
    assert code == 1 and "Use https://" in out.getvalue()


def test_cli_arguments():
    with pytest.raises(SystemExit):
        push.parse_args(["--server", SERVER, "login", "indeed"])
    with pytest.raises(SystemExit):
        push.parse_args(["login", "all"])  # --server is required
    args = push.parse_args(["--server", SERVER, "--password-env", "MY_PW", "database"])
    assert (args.command, args.path, args.password_env) == ("database", None, "MY_PW")
