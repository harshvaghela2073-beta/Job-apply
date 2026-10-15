"""Template Library: ``Template`` rows in the database (see docs/API_CONTRACT.md)."""

from sqlalchemy import case
from sqlmodel import Session, col, select

from app.models import Template, TemplateKind


def template_order() -> tuple:
    """ORDER BY for the library: kind in contract order (answer, linkedin_message, email), then id."""
    kind_rank = case(
        *((Template.kind == kind, rank) for rank, kind in enumerate(TemplateKind)),
        else_=len(TemplateKind),
    )
    return kind_rank, col(Template.id)


def list_templates(session: Session) -> list[Template]:
    return list(session.exec(select(Template).order_by(*template_order())))


def create_template(
    session: Session,
    *,
    kind: TemplateKind,
    question: str,
    answer: str,
    subject: str | None,
) -> Template:
    """Add a template to the session (not committed). Answers never have a subject."""
    template = Template(kind=kind, question=question, answer=answer)
    template.subject = None if kind == TemplateKind.answer else subject
    session.add(template)
    return template
