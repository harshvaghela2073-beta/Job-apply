"""Template Library: application answers, LinkedIn messages and emails, stored in the database.

Every create, update and delete regenerates the profile mirror file.
"""

from fastapi import APIRouter, HTTPException, Response
from sqlmodel import Session, select

from app.api.deps import PaginationDep, SessionDep, SettingsDep, paginate
from app.models import Template, TemplateKind
from app.schemas import Page, TemplateRead, TemplateWrite
from app.services.mirror import refresh_mirror
from app.services.templates import create_template as add_template
from app.services.templates import template_order

router = APIRouter(prefix="/templates", tags=["templates"])


def _get_template(session: Session, template_id: int) -> Template:
    template = session.get(Template, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found")
    return template


@router.get("", response_model=Page[TemplateRead])
def list_templates(
    session: SessionDep, page: PaginationDep, kind: TemplateKind | None = None
) -> Page[TemplateRead]:
    stmt = select(Template)
    if kind is not None:
        stmt = stmt.where(Template.kind == kind)
    items, total = paginate(session, stmt, page, *template_order())
    return Page(items=[TemplateRead.model_validate(t) for t in items], total=total)


@router.post("", response_model=TemplateRead, status_code=201)
def create_template(body: TemplateWrite, session: SessionDep, settings: SettingsDep) -> TemplateRead:
    template = add_template(
        session,
        kind=body.kind or TemplateKind.answer,
        question=body.question,
        answer=body.answer,
        subject=body.subject,
    )
    session.commit()
    session.refresh(template)
    refresh_mirror(session, settings)
    return TemplateRead.model_validate(template)


@router.put("/{template_id}", response_model=TemplateRead)
def update_template(
    template_id: int, body: TemplateWrite, session: SessionDep, settings: SettingsDep
) -> TemplateRead:
    template = _get_template(session, template_id)
    if body.kind is not None:
        template.kind = body.kind
    template.question = body.question
    template.answer = body.answer
    if "subject" in body.model_fields_set:
        template.subject = body.subject
    if template.kind == TemplateKind.answer:
        template.subject = None  # only emails and LinkedIn messages have a subject
    session.add(template)
    session.commit()
    session.refresh(template)
    refresh_mirror(session, settings)
    return TemplateRead.model_validate(template)


@router.delete("/{template_id}", status_code=204, response_class=Response)
def delete_template(template_id: int, session: SessionDep, settings: SettingsDep) -> Response:
    session.delete(_get_template(session, template_id))
    session.commit()
    refresh_mirror(session, settings)
    return Response(status_code=204)
