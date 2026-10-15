import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import Settings
from app.main import create_app
from app.models import ProfileField, Template
from tests.factories import make_question

EXPECTED = """\
> **Generated file. Do not edit by hand.** The Job-apply backend rewrites this whole file from its database after every profile or template change, so manual edits are lost. Change templates in the app's Template Library; change profile details by asking Claude (or through the API).

# Candidate Profile

## Personal

| Field | Value |
|---|---|
| Name | Asha Rao |
| Address | Flat 4 \\| Block B<br>Pune |

## Current job

| Field | Value |
|---|---|
| Company | Acme |

## Application answers

### What tech stack are you most familiar with?

I have worked with Java | Go and Python.

Mostly backend services.
Some React.

### Why a change?

_No answer yet._

## LinkedIn messages

### Connection request: openings at [Company]

Hi [Name], I'd like to connect.

## Emails

### Cold email to a hiring manager

Subject: Application: [Role] at [Company]

Hi [Name],

Please find my resume attached.
"""


def write_sample(client):
    fields = [
        {"key": "name", "label": "Name", "section": "Personal", "value": "Asha Rao"},
        {"key": "current_company", "label": "Company", "section": "Current job", "value": "Acme"},
        {"key": "address", "label": "Address", "section": "Personal", "value": "Flat 4 | Block B\nPune"},
    ]
    assert client.put("/api/profile/fields", json={"fields": fields}).status_code == 200
    templates = [
        {"kind": "email", "question": "Cold email to a hiring manager",
         "answer": "Hi [Name],\n\nPlease find my resume attached.",
         "subject": "Application: [Role] at [Company]"},
        {"question": "What tech stack are you most familiar with?",
         "answer": "I have worked with Java | Go and Python.\r\n\r\nMostly backend services.\nSome React."},
        {"kind": "linkedin_message", "question": "Connection request: openings at [Company]",
         "answer": "Hi [Name], I'd like to connect."},
        {"question": "Why a change?", "answer": ""},
    ]
    for body in templates:
        assert client.post("/api/templates", json=body).status_code == 201


def test_mirror_content(client, profile_path: Path):
    assert not profile_path.parent.exists()  # the directory is created on the first write
    write_sample(client)
    assert profile_path.read_text(encoding="utf-8") == EXPECTED


def test_reads_never_write_the_mirror(client, profile_path: Path):
    client.get("/api/profile")
    client.get("/api/templates")
    assert not profile_path.exists()


def test_mirror_follows_every_write(client, profile_path: Path):
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("hand-written notes that the backend must replace\n")

    created = client.post("/api/templates", json={"question": "Notice period?", "answer": "30 days"}).json()
    text = profile_path.read_text()
    assert text.startswith("> **Generated file. Do not edit by hand.**")
    assert "hand-written" not in text
    assert "### Notice period?\n\n30 days\n" in text

    client.put(f"/api/templates/{created['id']}", json={"question": "Notice period?", "answer": "60 days"})
    assert "### Notice period?\n\n60 days\n" in profile_path.read_text()

    client.delete(f"/api/templates/{created['id']}")
    assert "Notice period?" not in profile_path.read_text()

    client.put("/api/profile/fields", json={"fields": [
        {"key": "phone", "label": "Phone", "section": "Personal", "value": "98765 43210"},
    ]})
    assert "| Phone | 98765 43210 |\n" in profile_path.read_text()

    client.delete("/api/profile/fields/phone")
    assert "98765" not in profile_path.read_text()

    # Atomic writes leave no temp files behind.
    assert [p.name for p in profile_path.parent.iterdir()] == ["CANDIDATE_PROFILE.md"]


def test_mirror_after_answering_a_question(client, profile_path: Path):
    with Session(client.app.state.engine) as session:
        question = make_question(session, question="Open to Pune | Mumbai?")
    client.post(f"/api/questions/{question.id}/answer", json={"answer": "Yes, Pune."})
    assert "## Application answers\n\n### Open to Pune | Mumbai?\n\nYes, Pune.\n" in profile_path.read_text()


@pytest.fixture
def broken_client(tmp_path: Path, resume_path: Path):
    """PROFILE_PATH is inside a regular file, so the mirror can never be written."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'broken.db'}",
        profile_path=blocker / "CANDIDATE_PROFILE.md",
        resume_path=resume_path,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_mirror_failure_does_not_fail_requests(broken_client, caplog):
    with caplog.at_level(logging.ERROR, logger="app.services.mirror"):
        created = broken_client.post("/api/templates", json={"question": "Q", "answer": "A"})
        assert created.status_code == 201
        template_id = created.json()["id"]
        assert broken_client.put(f"/api/templates/{template_id}", json={"question": "Q2", "answer": "A"}).status_code == 200
        assert broken_client.delete(f"/api/templates/{template_id}").status_code == 204

        fields = [{"key": "name", "label": "Name", "section": "Personal", "value": "Asha"}]
        response = broken_client.put("/api/profile/fields", json={"fields": fields})
        assert response.status_code == 200
        assert response.json()["summary"]["name"] == "Asha"

        with Session(broken_client.app.state.engine) as session:
            question = make_question(session)
        answered = broken_client.post(f"/api/questions/{question.id}/answer", json={"answer": "Yes"})
        assert answered.status_code == 201

        assert broken_client.delete("/api/profile/fields/name").status_code == 204

    failures = [r for r in caplog.records if "mirror" in r.getMessage()]
    assert len(failures) == 6
    assert all(r.exc_info for r in failures)

    # The database is the source of truth and kept every change.
    with Session(broken_client.app.state.engine) as session:
        assert [t.question for t in session.exec(select(Template)).all()] == ["Are you open to relocating to Pune?"]
        assert session.exec(select(ProfileField)).all() == []
