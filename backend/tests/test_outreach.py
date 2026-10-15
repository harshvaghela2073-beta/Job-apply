import pytest

from app.models import (
    ConnectionNoteTooLong,
    OutreachKind,
    OutreachMessage,
    OutreachStatus,
)
from tests.factories import make_message


def test_list_outreach_filters(client, session):
    email = make_message(session)
    note = make_message(session, kind=OutreachKind.connection_note, subject=None, recipient_email=None)
    make_message(session, kind=OutreachKind.inmail, status=OutreachStatus.sent)

    data = client.get("/api/outreach").json()
    assert data["total"] == 3
    assert data["items"][0]["created_at"] >= data["items"][-1]["created_at"]

    data = client.get("/api/outreach", params={"kind": "connection_note"}).json()
    assert [m["id"] for m in data["items"]] == [note.id]

    data = client.get("/api/outreach", params={"status": "draft"}).json()
    assert {m["id"] for m in data["items"]} == {email.id, note.id}


def test_edit_draft(client, session):
    message = make_message(session)
    response = client.patch(f"/api/outreach/{message.id}", json={"subject": "New subject"})
    assert response.status_code == 200
    data = response.json()
    assert data["subject"] == "New subject"
    assert data["body"] == "Hi Priya, ..."

    data = client.patch(f"/api/outreach/{message.id}", json={"body": "Updated body"}).json()
    assert data["body"] == "Updated body" and data["subject"] == "New subject"


def test_edit_rejects_blank_body(client, session):
    message = make_message(session)
    assert client.patch(f"/api/outreach/{message.id}", json={"body": "   "}).status_code == 422


@pytest.mark.parametrize("status", ["approved", "sent", "rejected", "failed"])
def test_edit_non_draft_is_409(client, session, status):
    message = make_message(session, status=OutreachStatus(status))
    response = client.patch(f"/api/outreach/{message.id}", json={"body": "x"})
    assert response.status_code == 409
    assert isinstance(response.json()["detail"], str)


def test_connection_note_300_char_rule(client, session):
    note = make_message(session, kind=OutreachKind.connection_note, subject=None, recipient_email=None)

    ok = client.patch(f"/api/outreach/{note.id}", json={"body": "a" * 300})
    assert ok.status_code == 200 and len(ok.json()["body"]) == 300

    too_long = client.patch(f"/api/outreach/{note.id}", json={"body": "a" * 301})
    assert too_long.status_code == 422
    assert "300" in too_long.json()["detail"]
    session.expire_all()
    assert len(session.get(OutreachMessage, note.id).body) == 300

    # Emails and InMails have no such limit.
    email = make_message(session)
    assert client.patch(f"/api/outreach/{email.id}", json={"body": "a" * 2000}).status_code == 200


def test_connection_note_limit_enforced_on_create(session):
    with pytest.raises(ConnectionNoteTooLong):
        make_message(session, kind=OutreachKind.connection_note, body="a" * 301)
    session.rollback()


def test_approve(client, session):
    message = make_message(session)
    response = client.post(f"/api/outreach/{message.id}/approve")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "approved"
    assert data["approved_at"].endswith("Z")

    # Approving again (now non-draft) is a conflict.
    assert client.post(f"/api/outreach/{message.id}/approve").status_code == 409


@pytest.mark.parametrize("status", ["approved", "sent", "rejected", "failed"])
def test_approve_non_draft_is_409(client, session, status):
    message = make_message(session, status=OutreachStatus(status))
    assert client.post(f"/api/outreach/{message.id}/approve").status_code == 409


@pytest.mark.parametrize("status", ["draft", "approved"])
def test_reject_allowed(client, session, status):
    message = make_message(session, status=OutreachStatus(status))
    response = client.post(f"/api/outreach/{message.id}/reject")
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


@pytest.mark.parametrize("status", ["sent", "rejected", "failed"])
def test_reject_disallowed_is_409(client, session, status):
    message = make_message(session, status=OutreachStatus(status))
    assert client.post(f"/api/outreach/{message.id}/reject").status_code == 409


def test_outreach_404s(client):
    assert client.patch("/api/outreach/999", json={"body": "x"}).status_code == 404
    assert client.post("/api/outreach/999/approve").status_code == 404
    assert client.post("/api/outreach/999/reject").status_code == 404
