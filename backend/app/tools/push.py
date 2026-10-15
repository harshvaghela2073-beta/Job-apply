"""Push the Mac's LinkedIn / Naukri login, the resume or the database to a deployed backend
(docs/API_CONTRACT.md, "Deployment: login, pushing logins and data"). Run from ``backend/``::

    .venv/bin/python -m app.tools.push --server https://<backend> login linkedin|naukri|all
    .venv/bin/python -m app.tools.push --server https://<backend> resume [PATH]
    .venv/bin/python -m app.tools.push --server https://<backend> database [PATH]

- ``login`` reads the platform's cookies from this Mac's Chrome exactly like Connect does (the most
  recently used Chrome profile that is logged in; macOS asks for "Chrome Safe Storage" access) and
  sends them; the backend installs them and checks the login.
- ``resume`` uploads the PDF (default: the local ``RESUME_PATH``).
- ``database`` uploads a consistent snapshot (SQLite backup API) of the local database, or of PATH.

The app password is asked for (or read from ``$JOB_APPLY_PASSWORD``, see ``--password-env``) when
the server needs a login. HTTPS is required except for localhost. Only standard-library HTTP is
used. Nothing secret is printed: no cookie values, no password, no token.
"""

import argparse
import getpass as getpass_module
import json
import os
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import TextIO
from urllib.parse import quote, urlsplit

from sqlalchemy.engine import make_url

from app.browser.chrome_cookies import ChromeCookieReader, ChromeCookiesError
from app.config import Settings, get_settings
from app.models import ConnectionStatus, Platform
from app.services.connections import LOGIN_SPECS, PLATFORM_ORDER

# (method, url, headers, body) -> (HTTP status, response body)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
RESUME_MAX_BYTES = 10 * 1024 * 1024
DATABASE_MAX_BYTES = 100 * 1024 * 1024
TIMEOUT_SECONDS = 300  # the backend checks a pushed login in a browser before it answers


class PushError(Exception):
    """A problem to print (without a traceback)."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect would resend the Authorization header elsewhere: treat it as an error instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(_NoRedirects())
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise PushError(f"Couldn't reach {urlsplit(url).netloc}: {reason}") from error


def server_base(server: str) -> str:
    """The server's base URL. HTTPS is required, except for localhost."""
    parts = urlsplit(server.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise PushError(f"--server must be a URL such as https://my-backend.onrender.com (got {server!r}).")
    if parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
        raise PushError("Use https:// for the server (plain http is only allowed for localhost).")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise PushError("--server must be a plain URL, without a query, fragment or user name.")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


class Client:
    def __init__(self, base: str, transport: Transport):
        self.base = base
        self.transport = transport
        self.token: str | None = None

    def request(self, method: str, path: str, *, json_body=None, body: bytes | None = None,
                content_type: str | None = None) -> tuple[int, object]:
        headers = {"Accept": "application/json"}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        status, raw = self.transport(method, self.base + path, headers, body)
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        return status, data

    def call(self, method: str, path: str, **kwargs) -> dict:
        """Like ``request``, but anything other than 2xx raises ``PushError`` with the server's reason."""
        status, data = self.request(method, path, **kwargs)
        if 200 <= status < 300 and isinstance(data, dict):
            return data
        raise PushError(_failure(status, data))


def _failure(status: int, data) -> str:
    if 200 <= status < 300:
        return "The answer wasn't from the Job-apply backend; check the --server URL (the backend, not the frontend)."
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, list):  # FastAPI validation errors
        detail = "; ".join(str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail)
    if status == 401:
        return f"The server refused the login ({detail or 'HTTP 401'})."
    if 300 <= status < 400:
        return f"The server answered with a redirect (HTTP {status}); check the --server URL."
    return f"The server answered HTTP {status}: {detail or 'no details'}"


def log_in(client: Client, password_env: str, ask_password: Callable[[str], str], out: TextIO) -> None:
    """Get a token when the server needs a login (password from the environment, else asked)."""
    status = client.call("GET", "/api/auth/status")
    if not status.get("required"):
        return
    password = os.environ.get(password_env) or ask_password(f"App password for {client.base}: ")
    code, data = client.request("POST", "/api/auth/login", json_body={"password": password})
    if code == 401:
        raise PushError("Wrong app password.")
    if code == 429:
        raise PushError("Too many wrong passwords. Try again in 15 minutes.")
    if code != 200 or not isinstance(data, dict) or not data.get("token"):
        raise PushError(_failure(code, data))
    client.token = data["token"]
    print("Logged in to the server.", file=out)


def push_login(client: Client, platform: Platform, reader: ChromeCookieReader, out: TextIO) -> bool:
    """Send one platform's login from this Mac's Chrome. Returns whether the server confirmed it."""
    spec = LOGIN_SPECS[platform]
    name = spec.display_name
    try:
        newest = reader.newest_logged_in(spec.cookie_domain, spec.chrome_login_cookie)
        if newest is None:
            print(f"{name}: not logged in to {name} in Chrome on this Mac. Log in at {spec.login_url} first.", file=out)
            return False
        cookies = reader.read(newest, spec.cookie_domain)
    except ChromeCookiesError as error:
        print(f"{name}: {error}", file=out)
        return False
    print(f"{name}: sending {len(cookies)} cookies from Chrome profile {newest.profile!r}...", file=out)
    row = client.call(
        "PUT", f"/api/connections/{platform.value}/cookies",
        json_body={"cookies": [cookie.to_storage_cookie() for cookie in cookies]},
    )
    status = row.get("status")
    if status == ConnectionStatus.connected:
        account = row.get("account_name")
        print(f"{name}: connected{f' as {account}' if account else ''}.", file=out)
        return True
    message = row.get("message") or "the login check hasn't finished; see the app."
    print(f"{name}: {status} - {message}", file=out)
    return False


def push_resume(client: Client, path: Path, out: TextIO) -> None:
    if not path.is_file():
        raise PushError(f"No resume at {path}.")
    data = path.read_bytes()
    if len(data) > RESUME_MAX_BYTES:
        raise PushError(f"{path.name} is larger than 10 MB.")
    if not data.startswith(b"%PDF"):
        raise PushError(f"{path.name} isn't a PDF file.")
    result = client.call("PUT", "/api/admin/resume", body=data, content_type="application/pdf")
    print(f"Resume uploaded ({result.get('bytes')} bytes).", file=out)


def local_database(settings: Settings) -> Path:
    url = make_url(settings.database_url)
    database = url.database if url.get_backend_name() == "sqlite" else None
    if not database or database == ":memory:" or database.startswith("file:"):
        raise PushError("DATABASE_URL isn't a SQLite file; pass the database file's path.")
    return Path(database)


def snapshot(source: Path, target: Path) -> None:
    """A consistent copy of ``source`` (also while the local backend writes to it), as one
    self-contained file."""
    if not source.is_file():
        raise PushError(f"No database at {source}.")
    try:
        with closing(sqlite3.connect(f"file:{quote(str(source))}?mode=rw", uri=True, timeout=30)) as src, \
                closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.DatabaseError as error:
        raise PushError(f"{source.name} couldn't be read as a SQLite database ({error}).") from error


def push_database(client: Client, path: Path, out: TextIO) -> None:
    folder = tempfile.mkdtemp(prefix="job-apply-push-")
    copy = Path(folder) / "snapshot.db"
    try:
        snapshot(path, copy)
        size = copy.stat().st_size
        if size > DATABASE_MAX_BYTES:
            raise PushError(f"The database snapshot is {size // (1024 * 1024)} MB; the server takes at most 100 MB.")
        result = client.call("PUT", "/api/admin/database", body=copy.read_bytes(), content_type="application/octet-stream")
    finally:
        for leftover in Path(folder).iterdir():
            leftover.unlink()
        os.rmdir(folder)
    print(
        f"Database uploaded: {result.get('jobs')} jobs, {result.get('templates')} templates, "
        f"{result.get('profile_fields')} profile fields.",
        file=out,
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.push",
        description="Push this Mac's LinkedIn/Naukri login, the resume or the database to the deployed backend.",
    )
    parser.add_argument("--server", required=True, help="the backend's URL, e.g. https://job-apply.onrender.com")
    parser.add_argument(
        "--password-env", default="JOB_APPLY_PASSWORD", metavar="NAME",
        help="environment variable with the app password (default: JOB_APPLY_PASSWORD); asked for if unset",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="send the login from this Mac's Chrome")
    login.add_argument("platform", choices=[*(p.value for p in PLATFORM_ORDER), "all"])
    resume = commands.add_parser("resume", help="upload the resume PDF")
    resume.add_argument("path", nargs="?", type=Path, help="default: RESUME_PATH")
    database = commands.add_parser("database", help="upload a snapshot of the database")
    database.add_argument("path", nargs="?", type=Path, help="default: the local database (DATABASE_URL)")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    transport: Transport = urllib_transport,
    reader: ChromeCookieReader | None = None,
    ask_password: Callable[[str], str] = getpass_module.getpass,
    out: TextIO | None = None,
) -> int:
    """Returns the exit code. Tests pass ``transport`` (the in-process app), ``reader`` and
    ``ask_password``."""
    args = parse_args(argv)
    out = out or sys.stdout
    try:
        client = Client(server_base(args.server), transport)
        settings = settings or get_settings()
        log_in(client, args.password_env, ask_password, out)
        if args.command == "login":
            reader = reader or ChromeCookieReader(settings.chrome_user_data_dir)
            if not reader.available:
                raise PushError(f"Google Chrome's data wasn't found at {reader.user_data_dir} (macOS only).")
            platforms = list(PLATFORM_ORDER) if args.platform == "all" else [Platform(args.platform)]
            results = [push_login(client, platform, reader, out) for platform in platforms]
            return 0 if all(results) else 1
        if args.command == "resume":
            push_resume(client, args.path or settings.resume_path, out)
        else:
            push_database(client, args.path or local_database(settings), out)
        return 0
    except PushError as error:
        print(f"Error: {error}", file=sys.stderr if out is sys.stdout else out)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
