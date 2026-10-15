"""Profile-based answers for Easy Apply questions no template covers (2026-10-14), saved for reuse."""

import pytest
from sqlmodel import Session, select

from app.models import Template, TemplateKind
from app.sources.linkedin.answers import AnswerBook, FormField, save_learned

PROFILE = {
    "skills": "Java, Spring Boot, Golang, Python, SQL, Redis, Apache Kafka, Kubernetes",
    "form_years_experience": "1.9 (2 when the form only accepts whole numbers)",
    "location": "Bengaluru",
    "graduation": "Mathematics and Computing, DA-IICT, 2021–2025",
    "linkedin_url": "https://www.linkedin.com/in/someone/",
    "current_company": "Acme",
    "designation": "Software Development Engineer 1",
}
TEMPLATES = [("Notice period", "30 days"), ("Why are you a good fit? (short answer)", "I build backend systems.")]


def book() -> AnswerBook:
    return AnswerBook(profile=dict(PROFILE), templates=list(TEMPLATES))


@pytest.mark.parametrize(
    ("label", "kind", "options", "expected"),
    [
        ("How many years of work experience do you have with Java?", "numeric", (), "1.9"),
        ("How many years of experience do you have in Go?", "numeric", (), "1.9"),
        ("How many years of experience do you have with Angular?", "numeric", (), "0"),
        # Abbreviations seen live on 2026-10-14 (TCS)
        ("How many year of exp you have in Java 17 or Java 21?", "text", (), "1.9"),
        ("How many years of exp you have in Azure devops", "text", (), "0"),
        ("Do you have experience with Apache Kafka?", "radio", ("Yes", "No"), "Yes"),
        ("Do you have experience with React?", "radio", ("Yes", "No"), "No"),
        ("Will you require visa sponsorship?", "select", ("Select an option", "Yes", "No"), "No"),
        ("Are you legally authorized to work in India?", "radio", ("Yes", "No"), "Yes"),
        ("Are you comfortable commuting to this job's location (Bengaluru)?", "radio", ("Yes", "No"), "Yes"),
        ("Are you willing to relocate to Pune?", "radio", ("Yes", "No"), "No"),
        ("Have you completed the following level of education: Bachelor's Degree?", "radio", ("Yes", "No"), "Yes"),
        ("Can you start immediately?", "radio", ("Yes", "No"), "No"),
        ("Can you join within 30 days?", "radio", ("Yes", "No"), "Yes"),
        ("Gender", "select", ("Male", "Female", "Prefer not to say"), "Prefer not to say"),
        ("How did you hear about this job?", "select", ("Naukri", "LinkedIn", "Referral"), "LinkedIn"),
        ("LinkedIn Profile URL", "text", (), "https://www.linkedin.com/in/someone/"),
        ("Current company", "text", (), "Acme"),
        ("Why do you want to join our team?", "textarea", (), "I build backend systems."),
    ],
)
def test_inferred_answers(label, kind, options, expected):
    answers = book()
    answer = answers.answer(FormField(id="1", label=label, type=kind, required=True, options=options))
    assert answer.value == expected
    assert answers.learned[-1] == (label, expected)


def test_unknown_questions_stay_unanswered():
    answers = book()
    answer = answers.answer(FormField(id="1", label="What is your favourite design pattern?", type="text", required=True))
    assert answer.value is None and answers.learned == []


def test_learned_answers_are_reused_and_saved(client):
    answers = book()
    question = FormField(id="1", label="Will you require visa sponsorship?", type="radio", required=True, options=("Yes", "No"))
    answers.answer(question)
    again = answers.answer(question)  # the second time it comes from the remembered answer
    assert again.value == "No"
    with Session(client.app.state.engine) as session:
        assert save_learned(session, answers.learned + answers.learned) == 1
        session.commit()
        stored = session.exec(select(Template).where(Template.kind == TemplateKind.answer)).all()
        assert [(t.question, t.answer) for t in stored] == [("Will you require visa sponsorship?", "No")]
        assert save_learned(session, answers.learned) == 0  # never twice


@pytest.mark.parametrize(
    ("label", "expected"),
    [("Current ctc", "12"), ("exp ctc", "18"), ("Notice period", "30")],
)
def test_rejected_answers_are_retried_as_plain_numbers(label, expected):
    """LinkedIn's "Invalid input" on a text box that takes numbers only (TCS, 2026-10-14)."""
    answers = AnswerBook(profile={**PROFILE, "current_ctc": "12 LPA", "form_expected_ctc": "18 LPA"}, templates=list(TEMPLATES))
    first = answers.answer(FormField(id="1", label=label, type="text", required=True))
    assert first.value is not None and first.value != expected  # "12 LPA", "18 LPA", "30 days"
    again = answers.answer(FormField(id="1", label=label, type="text", required=True, error="Invalid input"))
    assert again.value == expected and "plain number" in again.reason


def test_plain_numbers_and_unrelated_answers_are_kept_on_an_error():
    answers = book()
    field = FormField(id="1", label="How many years of experience do you have with Java?", type="text", required=True,
                      error="Invalid input")
    assert answers.answer(field).value == "1.9"
    url = FormField(id="2", label="LinkedIn Profile URL", type="text", required=True, error="Invalid input")
    assert answers.answer(url).value == "https://www.linkedin.com/in/someone/"
