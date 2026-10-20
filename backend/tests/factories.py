"""Small helpers to insert rows in tests."""

from itertools import count

from sqlmodel import Session

from app.models import (
    ApplyType,
    Job,
    JobStatus,
    OutreachKind,
    OutreachMessage,
    OutreachStatus,
    Platform,
    ProfileField,
    Template,
    TemplateKind,
    UnansweredQuestion,
)

_ids = count(1)


def make_job(session: Session, **overrides) -> Job:
    n = next(_ids)
    values = dict(
        platform=Platform.linkedin,
        external_id=f"ext-{n}",
        title=f"Engineer #{n}",  # "Engineer 3" would be a level-3 (senior) title
        company=f"[Sample] Company {n}",
        location="Bengaluru",
        url=f"https://example.com/jobs/{n}",
        short_description="Python, 3-5 yrs.",
        apply_type=ApplyType.company_site,
        status=JobStatus.needs_manual,
    )
    values.update(overrides)
    job = Job(**values)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def make_message(session: Session, **overrides) -> OutreachMessage:
    values = dict(
        kind=OutreachKind.email,
        recipient_name="Priya Sharma",
        recipient_email="priya@example.com",
        subject="Application",
        body="Hi Priya, ...",
        status=OutreachStatus.draft,
    )
    values.update(overrides)
    message = OutreachMessage(**values)
    session.add(message)
    session.commit()
    session.refresh(message)
    return message


def make_question(session: Session, **overrides) -> UnansweredQuestion:
    values = dict(question="Are you open to relocating to Pune?", platform=Platform.naukri)
    values.update(overrides)
    question = UnansweredQuestion(**values)
    session.add(question)
    session.commit()
    session.refresh(question)
    return question


def make_template(session: Session, **overrides) -> Template:
    values = dict(kind=TemplateKind.answer, question="Why a change?", answer="Growth.")
    values.update(overrides)
    template = Template(**values)
    session.add(template)
    session.commit()
    session.refresh(template)
    return template


def make_field(session: Session, **overrides) -> ProfileField:
    values = dict(key="name", label="Name", section="Personal", value="Asha Rao", position=1)
    values.update(overrides)
    field = ProfileField(**values)
    session.add(field)
    session.commit()
    session.refresh(field)
    return field
