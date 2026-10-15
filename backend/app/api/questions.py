"""Form questions the tool met but couldn't answer from the Template Library."""

from fastapi import APIRouter, HTTPException
from sqlmodel import col, select

from app.api.deps import PaginationDep, SessionDep, SettingsDep, paginate
from app.models import TemplateKind, UnansweredQuestion
from app.schemas import AnswerBody, Page, TemplateRead, UnansweredQuestionRead
from app.services.mirror import refresh_mirror
from app.services.templates import create_template
from app.timeutils import utcnow

router = APIRouter(prefix="/questions", tags=["questions"])


@router.get("/unanswered", response_model=Page[UnansweredQuestionRead])
def list_unanswered(session: SessionDep, page: PaginationDep) -> Page[UnansweredQuestionRead]:
    stmt = select(UnansweredQuestion).where(col(UnansweredQuestion.answered_at).is_(None))
    items, total = paginate(
        session, stmt, page, col(UnansweredQuestion.seen_at).desc(), col(UnansweredQuestion.id).desc()
    )
    return Page(items=[UnansweredQuestionRead.model_validate(q) for q in items], total=total)


@router.post("/{question_id}/answer", response_model=TemplateRead, status_code=201)
def answer_question(
    question_id: int, body: AnswerBody, session: SessionDep, settings: SettingsDep
) -> TemplateRead:
    question = session.get(UnansweredQuestion, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail=f"Question {question_id} not found")
    if question.answered_at is not None:
        raise HTTPException(status_code=409, detail=f"Question {question_id} is already answered")
    # One transaction: the template is added and the question marked answered, or neither.
    template = create_template(
        session, kind=TemplateKind.answer, question=question.question, answer=body.answer, subject=None
    )
    question.answered_at = utcnow()
    session.add(question)
    session.commit()
    session.refresh(template)
    refresh_mirror(session, settings)
    return TemplateRead.model_validate(template)
