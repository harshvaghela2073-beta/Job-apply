"""PUT /api/admin/resume and PUT /api/admin/database (docs/API_CONTRACT.md, "Deployment: login,
pushing logins and data")."""

import os
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import inspect
from sqlmodel import Session, select

from app.db import create_db_engine, init_db
from app.models import Connection, ConnectionStatus, Platform, ScanKind, ScanRun, ScanStatus
from app.scheduler.runner import ScansRunning
from app.services.app_settings import RUN_INTERVALS, set_minutes
from tests.conftest import FAKE_PDF, make_app
from tests.factories import make_field, make_job, make_template
from tests.scan_helpers import SEARCH_FIELDS
from tests.test_scans import FakeScanner, wait_until

NEW_PDF = b"%PDF-1.7\n% the new resume\n%%EOF\n"


def upload_resume(client, body, content_type="application/pdf", headers=None):
    headers = {**({"Content-Type": content_type} if content_type else {}), **(headers or {})}
    return client.put("/api/admin/resume", content=body, headers=headers)


def upload_db(client, body, content_type="application/octet-stream"):
    headers = {"Content-Type": content_type} if content_type else {}
    return client.put("/api/admin/database", content=body, headers=headers)


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if ".upload-" in p.name or p.name.endswith(".tmp"))


# ---------------------------------------------------------------------------
# Resume


def test_resume_upload_replaces_the_file(client, resume_path):
    response = upload_resume(client, NEW_PDF)
    assert response.status_code == 200 and response.json() == {"bytes": len(NEW_PDF)}
    assert resume_path.read_bytes() == NEW_PDF
    assert client.get("/api/profile/resume").content == NEW_PDF
    assert leftovers(resume_path.parent) == []


def test_resume_upload_creates_the_folder(settings, fake_browser, tmp_path):
    target = tmp_path / "disk" / "resume" / "Resume.pdf"
    with TestClient(make_app(settings.model_copy(update={"resume_path": target}), fake_browser)) as client:
        assert client.get("/api/profile").json()["summary"]["resume"] is None
        assert upload_resume(client, NEW_PDF, "application/pdf; charset=binary").status_code == 200
        assert target.read_bytes() == NEW_PDF
        assert client.get("/api/profile").json()["summary"]["resume"]["filename"] == "Resume.pdf"


def test_resume_upload_rejects(client, resume_path, monkeypatch):
    for content_type in ("text/plain", "application/octet-stream", None):
        assert upload_resume(client, NEW_PDF, content_type).status_code == 415
    not_pdf = upload_resume(client, b"<html>not a pdf</html>")
    assert not_pdf.status_code == 422 and not_pdf.json()["detail"] == "That isn't a PDF file."
    assert upload_resume(client, b"").status_code == 422
    too_big = upload_resume(client, b"%PDF" + b"0" * (10 * 1024 * 1024))
    assert too_big.status_code == 413 and too_big.json()["detail"] == "The file is larger than 10 MB."
    # Without a Content-Length (chunked), the stream stops at the limit.
    monkeypatch.setattr("app.api.admin.RESUME_MAX_BYTES", 1000)
    chunks = (b"%PDF" if i == 0 else b"0" * 400 for i in range(10))
    assert upload_resume(client, chunks).status_code == 413
    assert resume_path.read_bytes() == FAKE_PDF  # unchanged every time
    assert leftovers(resume_path.parent) == []


def test_resume_write_is_atomic(client, resume_path, monkeypatch):
    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("app.services.files.os.replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        upload_resume(client, NEW_PDF)
    assert resume_path.read_bytes() == FAKE_PDF
    assert leftovers(resume_path.parent) == []


def test_uploads_need_the_app_login(settings, fake_browser):
    settings = settings.model_copy(update={"app_password": SecretStr("pw-for-uploads")})
    with TestClient(make_app(settings, fake_browser)) as client:
        assert upload_resume(client, NEW_PDF).status_code == 401
        assert upload_db(client, b"x").status_code == 401
        token = client.post("/api/auth/login", json={"password": "pw-for-uploads"}).json()["token"]
        assert upload_resume(client, NEW_PDF, headers={"Authorization": f"Bearer {token}"}).status_code == 200


# ---------------------------------------------------------------------------
# Database


def make_db(path: Path, *, jobs=3, templates=2, fields=2, page_size: int | None = None, prepare=None) -> bytes:
    """A database file like a Mac's, returned as bytes."""
    if page_size:
        with sqlite3.connect(path) as connection:
            connection.execute(f"PRAGMA page_size={page_size}")
            connection.execute("CREATE TABLE _first(a)")
    engine = create_db_engine(f"sqlite:///{path}")
    init_db(engine)
    with Session(engine) as session:
        for n in range(jobs):
            make_job(session, title=f"Uploaded job {n}", external_id=f"up-{n}")
        for n in range(templates):
            make_template(session, question=f"Uploaded question {n}?")
        for n in range(fields):
            make_field(session, key=f"field_{n}", label=f"Field {n}", value=f"Uploaded value {n}", position=n)
        if prepare:
            prepare(session)
    engine.dispose()
    return path.read_bytes()


def test_database_upload_replaces_the_data(client, settings, tmp_path, profile_path):
    app = client.app
    with Session(app.state.engine) as session:
        make_job(session, title="Server job")
        login = session.get(Connection, Platform.linkedin)
        login.status, login.account_name = ConnectionStatus.connected, "Server Login"
        session.add(login)
        session.commit()

    def prepare(session):
        set_minutes(session, RUN_INTERVALS[Platform.linkedin], 45)
        session.add(ScanRun(kind=ScanKind.naukri_jobs, status=ScanStatus.running))
        session.add(Connection(platform=Platform.linkedin, status=ConnectionStatus.disconnected))
        session.commit()

    body = make_db(tmp_path / "mac.db", prepare=prepare)
    response = upload_db(client, body)
    assert response.status_code == 200
    assert response.json() == {"jobs": 3, "templates": 2, "profile_fields": 2}

    jobs = client.get("/api/jobs").json()
    assert jobs["total"] == 3 and {j["title"] for j in jobs["items"]} == {f"Uploaded job {n}" for n in range(3)}
    assert "Uploaded value 1" in profile_path.read_text()  # the mirror follows the new data
    # This server's own login is kept, not the Mac's.
    linkedin = client.get("/api/connections").json()["items"][0]
    assert (linkedin["status"], linkedin["account_name"]) == ("connected", "Server Login")
    # A run the snapshot caught mid-way is closed; the uploaded scan interval is used.
    run = client.get("/api/scans").json()["items"][0]
    assert run["status"] == "failed" and run["message"].startswith("Interrupted")
    assert app.state.scheduler.interval_minutes(Platform.linkedin) == 45

    # The app keeps working on the new database: writes, reads, a second upload.
    added = client.put("/api/profile/fields", json={"fields": [{"key": "city", "label": "City", "section": "Personal", "value": "Pune"}]})
    assert added.status_code == 200
    assert client.get("/api/dashboard/summary").status_code == 200
    with Session(app.state.engine) as session:
        assert len(session.exec(select(Connection)).all()) == 2
    live = Path(app.state.engine.url.database)
    with sqlite3.connect(live) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT count(*) FROM profile_field").fetchone() == (3,)
    assert upload_db(client, make_db(tmp_path / "mac2.db", jobs=1, templates=0, fields=0)).json() == {
        "jobs": 1, "templates": 0, "profile_fields": 0,
    }
    assert leftovers(live.parent) == []


def test_database_upload_adds_missing_columns(client, tmp_path):
    def drop_column(session):
        session.connection().exec_driver_sql("ALTER TABLE job DROP COLUMN contact_checked_at")
        session.commit()

    body = make_db(tmp_path / "old.db", prepare=drop_column)
    with sqlite3.connect(tmp_path / "old.db") as connection:
        assert "contact_checked_at" not in {row[1] for row in connection.execute("PRAGMA table_info(job)")}
    assert upload_db(client, body).status_code == 200
    columns = {c["name"] for c in inspect(client.app.state.engine).get_columns("job")}
    assert "contact_checked_at" in columns
    assert client.get("/api/jobs").json()["total"] == 3


def test_database_upload_with_another_page_size(client, tmp_path):
    body = make_db(tmp_path / "big-pages.db", page_size=8192)
    assert int.from_bytes(body[16:18], "big") == 8192
    assert upload_db(client, body).json() == {"jobs": 3, "templates": 2, "profile_fields": 2}
    assert client.get("/api/jobs").json()["total"] == 3


def test_database_upload_rejects_bad_files(client, tmp_path, monkeypatch):
    with Session(client.app.state.engine) as session:
        make_job(session, title="Server job")
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE notes(text)")
    damaged = bytearray(make_db(tmp_path / "damaged.db"))
    damaged[100:4096 * 3] = os.urandom(4096 * 3 - 100)
    cases = [
        (b"definitely not a database", "That isn't a SQLite database file."),
        (b"", "That isn't a SQLite database file."),
        (other.read_bytes(), "That database doesn't have this app's tables (missing: job, template, profile_field)."),
    ]
    for body, detail in cases:
        response = upload_db(client, body)
        assert response.status_code == 422 and response.json()["detail"] == detail
    response = upload_db(client, bytes(damaged))
    assert response.status_code == 422 and response.json()["detail"].startswith(("That isn't a usable", "The database file is damaged"))

    assert upload_db(client, other.read_bytes(), "application/pdf").status_code == 415
    monkeypatch.setattr("app.api.admin.DATABASE_MAX_BYTES", 1024)
    too_big = upload_db(client, other.read_bytes() + b"\0" * 2048)
    assert too_big.status_code == 413 and too_big.json()["detail"] == "The file is larger than 0 MB."
    # Nothing changed and no temp files are left.
    assert [j["title"] for j in client.get("/api/jobs").json()["items"]] == ["Server job"]
    assert leftovers(Path(client.app.state.engine.url.database).parent) == []


def test_database_upload_409_while_a_scan_runs(settings, fake_browser, tmp_path):
    gate = threading.Event()
    scanner = FakeScanner(ScanKind.naukri_jobs, gate=gate)
    app = make_app(settings, fake_browser, scanners=[scanner])
    body = make_db(tmp_path / "mac.db")
    with TestClient(app) as client:
        assert client.put("/api/profile/fields", json={"fields": SEARCH_FIELDS}).status_code == 200
        assert client.post("/api/scans/run", params={"platform": "naukri"}).status_code == 202
        assert scanner.started.wait(5)
        response = upload_db(client, body)
        assert response.status_code == 409
        assert response.json()["detail"] == "A scan is running. Try again when it has finished."
        gate.set()
        assert wait_until(lambda: not app.state.scans.running)
        assert upload_db(client, body).status_code == 200


def test_hold_keeps_scans_from_starting(settings, fake_browser):
    gate = threading.Event()
    scanner = FakeScanner(ScanKind.naukri_jobs, gate=gate)
    app = make_app(settings, fake_browser, scanners=[scanner])
    scans = app.state.scans
    with TestClient(app):
        with scans.hold():
            assert scans.start(Platform.naukri) == []
        assert scans.start(Platform.naukri) == [Platform.naukri]
        with pytest.raises(ScansRunning), scans.hold():
            pass
        gate.set()
        assert scans.wait(5)
