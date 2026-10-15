from sqlmodel import select

from app.models import Platform, Template, TemplateKind, UnansweredQuestion
from app.timeutils import utcnow
from tests.factories import make_question, make_template


def test_list_unanswered(client, session):
    open_question = make_question(session, platform=Platform.linkedin)
    make_question(session, answered_at=utcnow())

    data = client.get("/api/questions/unanswered").json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["id"] == open_question.id
    assert item["platform"] == "linkedin"
    assert item["answered_at"] is None
    assert item["seen_at"].endswith("Z")
    assert set(item) == {"id", "question", "platform", "job_id", "seen_at", "answered_at"}


def test_answer_creates_answer_template_and_sets_answered_at(client, session):
    make_template(session, kind=TemplateKind.email, question="Cold email", subject="Hi")
    question = make_question(session, question="Are you open to working from Pune | Mumbai?")

    response = client.post(
        f"/api/questions/{question.id}/answer", json={"answer": "Yes, Pune.\r\nNot Mumbai.\n"}
    )
    assert response.status_code == 201
    data = response.json()
    assert data["kind"] == "answer"
    assert data["question"] == "Are you open to working from Pune | Mumbai?"
    assert data["answer"] == "Yes, Pune.\nNot Mumbai."
    assert data["subject"] is None
    assert data["is_answered"] is True

    session.expire_all()
    assert session.get(UnansweredQuestion, question.id).answered_at is not None
    stored = session.get(Template, data["id"])
    assert stored.kind == TemplateKind.answer and stored.answer == "Yes, Pune.\nNot Mumbai."
    assert client.get("/api/questions/unanswered").json()["total"] == 0

    answers = client.get("/api/templates", params={"kind": "answer"}).json()["items"]
    assert [t["id"] for t in answers] == [data["id"]]

    # Answering twice is a conflict and adds nothing.
    again = client.post(f"/api/questions/{question.id}/answer", json={"answer": "No"})
    assert again.status_code == 409
    assert len(session.exec(select(Template)).all()) == 2


def test_answer_validation_and_404(client, session):
    question = make_question(session)
    assert client.post(f"/api/questions/{question.id}/answer", json={"answer": "  "}).status_code == 422
    assert client.post("/api/questions/999/answer", json={"answer": "Yes"}).status_code == 404
    session.expire_all()
    assert session.get(UnansweredQuestion, question.id).answered_at is None
    assert session.exec(select(Template)).all() == []
