import re

import pytest

from app.models import Template, TemplateKind

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def create(client, **body):
    body.setdefault("question", "Q")
    body.setdefault("answer", "A")
    response = client.post("/api/templates", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_list_empty(client):
    assert client.get("/api/templates").json() == {"items": [], "total": 0}


def test_create_defaults_to_answer(client):
    data = create(client, question="  What tech stack do you know?  ", answer="Java, Go, Python.")
    assert set(data) == {
        "id", "kind", "question", "answer", "subject", "is_answered", "created_at", "updated_at",
    }
    assert data["kind"] == "answer"
    assert data["question"] == "What tech stack do you know?"
    assert data["answer"] == "Java, Go, Python."
    assert data["subject"] is None
    assert data["is_answered"] is True
    assert ISO_Z.match(data["created_at"]) and ISO_Z.match(data["updated_at"])


def test_line_breaks_are_kept(client):
    data = create(client, answer="First paragraph.\r\n\r\nSecond | paragraph,\nline two.\n")
    assert data["answer"] == "First paragraph.\n\nSecond | paragraph,\nline two."


def test_subject(client):
    email = create(client, kind="email", question="Cold email", subject="Application: [Role]")
    assert email["kind"] == "email" and email["subject"] == "Application: [Role]"

    inmail = create(client, kind="linkedin_message", question="InMail", subject="Hello")
    assert inmail["subject"] == "Hello"

    note = create(client, kind="linkedin_message", question="Connection note", subject="   ")
    assert note["subject"] is None  # blank means none

    answer = create(client, kind="answer", question="Notice period?", subject="ignored")
    assert answer["subject"] is None  # answers never have a subject


@pytest.mark.parametrize("answer", ["", "TBD", "tbd", "  TBD  "])
def test_unanswered(client, answer):
    assert create(client, answer=answer)["is_answered"] is False


def test_order_and_kind_filter(client):
    email = create(client, kind="email", question="E1")
    answer1 = create(client, kind="answer", question="A1")
    message = create(client, kind="linkedin_message", question="L1")
    answer2 = create(client, question="A2")

    data = client.get("/api/templates").json()
    assert data["total"] == 4
    # By kind (answer, linkedin_message, email), then id.
    assert [t["id"] for t in data["items"]] == [answer1["id"], answer2["id"], message["id"], email["id"]]

    data = client.get("/api/templates", params={"kind": "answer"}).json()
    assert data["total"] == 2
    assert [t["id"] for t in data["items"]] == [answer1["id"], answer2["id"]]
    data = client.get("/api/templates", params={"kind": "email"}).json()
    assert [t["id"] for t in data["items"]] == [email["id"]]

    data = client.get("/api/templates", params={"limit": 2, "offset": 1}).json()
    assert data["total"] == 4
    assert [t["id"] for t in data["items"]] == [answer2["id"], message["id"]]

    assert client.get("/api/templates", params={"kind": "sms"}).status_code == 422


def test_update(client, session):
    created = create(client, kind="email", question="Cold email", answer="Hi", subject="Hello")

    # Leaving out kind and subject keeps them.
    response = client.put(
        f"/api/templates/{created['id']}", json={"question": "Cold email v2", "answer": "Hi\n\nBye"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == created["id"]
    assert data["kind"] == "email" and data["subject"] == "Hello"
    assert data["question"] == "Cold email v2" and data["answer"] == "Hi\n\nBye"
    assert data["created_at"] == created["created_at"]
    assert data["updated_at"] >= created["updated_at"]

    # An explicit null clears the subject.
    data = client.put(
        f"/api/templates/{created['id']}", json={"question": "Q", "answer": "A", "subject": None}
    ).json()
    assert data["subject"] is None

    # Changing the kind to answer drops the subject.
    client.put(f"/api/templates/{created['id']}", json={"question": "Q", "answer": "A", "subject": "S"})
    data = client.put(
        f"/api/templates/{created['id']}", json={"kind": "answer", "question": "Q", "answer": "TBD"}
    ).json()
    assert data["kind"] == "answer" and data["subject"] is None and data["is_answered"] is False

    session.expire_all()
    stored = session.get(Template, created["id"])
    assert stored.kind == TemplateKind.answer and stored.answer == "TBD"


def test_delete(client):
    created = create(client)
    assert client.delete(f"/api/templates/{created['id']}").status_code == 204
    assert client.get("/api/templates").json()["total"] == 0
    assert client.delete(f"/api/templates/{created['id']}").status_code == 404


def test_404s(client):
    assert client.put("/api/templates/99", json={"question": "Q", "answer": "A"}).status_code == 404
    assert client.delete("/api/templates/99").status_code == 404


def test_validation(client):
    assert client.post("/api/templates", json={"question": "  ", "answer": "A"}).status_code == 422
    assert client.post("/api/templates", json={"question": "Q"}).status_code == 422
    assert client.post("/api/templates", json={"kind": "sms", "question": "Q", "answer": "A"}).status_code == 422
    created = create(client)
    response = client.put(f"/api/templates/{created['id']}", json={"question": "", "answer": "A"})
    assert response.status_code == 422
