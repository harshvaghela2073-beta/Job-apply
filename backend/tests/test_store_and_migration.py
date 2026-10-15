import sqlite3
from datetime import timedelta

from sqlalchemy import inspect
from sqlmodel import Session, select

from app.db import add_missing_columns, create_db_engine, init_db
from app.models import ApplyType, Job, JobStatus, Platform
from app.scanner.base import Candidate
from app.scanner.store import dedupe, store_candidates
from app.timeutils import utcnow
from tests.factories import make_job


def candidate(external_id, priority=100, platform=Platform.naukri, **extra):
    values = dict(
        platform=platform,
        external_id=external_id,
        title="Backend Developer",
        company="Acme",
        url=f"https://example.com/{external_id}",
        apply_type=ApplyType.one_click,
        priority=priority,
        matched_role="Backend Developer",
        location="Bengaluru",
        posted_at=utcnow() - timedelta(hours=1),
        experience="1-4 Yrs",
    )
    values.update(extra)
    return Candidate(**values)


def test_dedupe_keeps_the_most_important():
    items = dedupe([candidate("1", 141), candidate("2", 10), candidate("1", 40), candidate("1", 40, location="Remote")])
    assert [(c.external_id, c.priority, c.location) for c in items] == [("1", 40, "Bengaluru"), ("2", 10, "Bengaluru")]
    # Same id on another platform is a different item.
    assert len(dedupe([candidate("1"), candidate("1", platform=Platform.linkedin)])) == 2


def test_store_inserts_new_and_leaves_existing_alone(client, session):
    existing = make_job(session, platform=Platform.naukri, external_id="1", status=JobStatus.applied,
                        title="Old title", applied_at=utcnow())
    found, new = store_candidates(client.app.state.engine, [candidate("1"), candidate("2", 140), candidate("2", 40)])
    assert (found, new) == (2, 1)
    session.expire_all()
    kept = session.get(Job, existing.id)
    assert kept.title == "Old title" and kept.status == JobStatus.applied
    stored = session.exec(select(Job).where(Job.external_id == "2")).one()
    assert stored.status == JobStatus.found and stored.priority == 40
    assert stored.contact_name is None and stored.contact_profile_url is None
    assert stored.matched_role == "Backend Developer" and stored.experience == "1-4 Yrs"
    assert stored.posted_at is not None
    assert store_candidates(client.app.state.engine, [candidate("2")]) == (1, 0)


OLD_JOB_TABLE = """
CREATE TABLE job (
    id INTEGER NOT NULL,
    platform VARCHAR(8) NOT NULL,
    external_id VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    company VARCHAR NOT NULL,
    location VARCHAR NOT NULL,
    url VARCHAR NOT NULL,
    apply_url VARCHAR,
    form_url VARCHAR,
    contact_email VARCHAR,
    post_url VARCHAR,
    short_description VARCHAR NOT NULL,
    apply_type VARCHAR(12) NOT NULL,
    status VARCHAR(12) NOT NULL,
    status_reason VARCHAR,
    found_at DATETIME NOT NULL,
    applied_at DATETIME,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_job_platform_external_id UNIQUE (platform, external_id)
);
INSERT INTO job VALUES (1, 'linkedin', '390', 'Engineer', 'Acme', 'Bengaluru', 'https://x', NULL, NULL, NULL,
    NULL, '', 'company_site', 'applied', NULL, '2026-10-12 05:00:00.000000', '2026-10-12 06:00:00.000000',
    '2026-10-12 06:00:00.000000');
CREATE TABLE profile_field (
    id INTEGER NOT NULL, "key" VARCHAR NOT NULL, label VARCHAR NOT NULL, section VARCHAR NOT NULL,
    value VARCHAR NOT NULL, position INTEGER NOT NULL, updated_at DATETIME NOT NULL, PRIMARY KEY (id)
);
INSERT INTO profile_field VALUES (1, 'name', 'Name', 'Personal', 'Harsh', 1, '2026-10-12 06:00:00');
"""


def test_startup_adds_missing_nullable_columns(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(OLD_JOB_TABLE)
    engine = create_db_engine(f"sqlite:///{path}")
    init_db(engine)

    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("job")}
    for name in ("posted_at", "experience", "matched_role", "priority", "contact_name", "contact_profile_url",
                 "contact_checked_at"):
        assert columns[name]["nullable"]
    assert {"connection", "scan_run"} <= set(inspector.get_table_names())
    with Session(engine) as session:
        job = session.get(Job, 1)
        assert (job.title, job.status, job.applied_at.isoformat()) == ("Engineer", JobStatus.applied, "2026-10-12T06:00:00+00:00")
        assert job.posted_at is None and job.priority is None
        assert job.contact_name is None and job.contact_profile_url is None and job.contact_checked_at is None
        job.priority = 5
        job.contact_name, job.contact_profile_url = "Priya Sharma", "https://www.linkedin.com/in/priya/"
        job.contact_checked_at = utcnow()
        session.add(job)
        session.commit()
    with Session(engine) as session:
        job = session.get(Job, 1)
        assert job.contact_profile_url == "https://www.linkedin.com/in/priya/" and job.contact_checked_at.tzinfo is not None
    assert add_missing_columns(engine) == []  # idempotent
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM profile_field").fetchall() == [("Harsh",)]
    engine.dispose()


def test_store_keeps_the_post_author(client, session):
    store_candidates(client.app.state.engine, [candidate(
        "urn:li:activity:9", platform=Platform.linkedin, apply_type=ApplyType.email, contact_email="hr@acme.com",
        contact_name="Priya Sharma", contact_profile_url="https://www.linkedin.com/in/priya-sharma/",
    )])
    stored = session.exec(select(Job).where(Job.external_id == "urn:li:activity:9")).one()
    assert (stored.contact_name, stored.contact_profile_url) == ("Priya Sharma", "https://www.linkedin.com/in/priya-sharma/")
    assert "contact_name" not in client.get(f"/api/jobs/{stored.id}").json()  # not part of the API's Job
