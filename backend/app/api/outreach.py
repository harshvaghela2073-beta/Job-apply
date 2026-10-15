"""The outreach queue (hiring-manager emails, connection notes and InMails), and the test sends
that come before switching on automatic outreach (docs/API_CONTRACT.md, "Automatic email and
LinkedIn outreach"):

- ``POST /outreach/test-email``: the email template with the resume, to the candidate's own
  ``SMTP_USER``. Nothing is recorded.
- ``POST /outreach/test-note?profile_url=&dry_run=true``: opens the profile and fills the note; a
  dry run closes the dialog without sending. ``dry_run=false`` sends that one note and records it.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlmodel import Session, col, select

from app.api.deps import PaginationDep, SessionDep, paginate
from app.models import (
    ConnectionNoteTooLong,
    Job,
    OutreachKind,
    OutreachMessage,
    OutreachStatus,
    Platform,
    check_connection_note,
)
from app.scanner.base import LoginWall, ScanBlocked
from app.schemas import OutreachMessageRead, OutreachPatch, Page, TestEmailRead, TestNoteRead
from app.services.mailer import TEST_JOB_TITLE, Mailer, MailError, SmtpConfig, build_email
from app.services.outreach import DraftProblem, email_template, note_templates, sent_this_month, sent_today
from app.sources.linkedin import outreach as linkedin
from app.sources.linkedin.easy_apply import LOGIN_EXPIRED, SECURITY_CHECK
from app.sources.linkedin.login import LINKEDIN_LOGIN
from app.sources.linkedin.parse import normalize_profile_url
from app.timeutils import utcnow

router = APIRouter(prefix="/outreach", tags=["outreach"])

NOTE_TIMEOUT_SECONDS = 180
DRY_RUN_HELP = "Fill the note in the dialog, then close it without sending. false sends this one note."


@router.get("", response_model=Page[OutreachMessageRead])
def list_outreach(
    session: SessionDep,
    page: PaginationDep,
    kind: OutreachKind | None = None,
    status: OutreachStatus | None = None,
) -> Page[OutreachMessageRead]:
    stmt = select(OutreachMessage)
    if kind is not None:
        stmt = stmt.where(OutreachMessage.kind == kind)
    if status is not None:
        stmt = stmt.where(OutreachMessage.status == status)
    items, total = paginate(
        session, stmt, page, col(OutreachMessage.created_at).desc(), col(OutreachMessage.id).desc()
    )
    return Page(items=[OutreachMessageRead.model_validate(m) for m in items], total=total)


def _get_message(session: Session, message_id: int) -> OutreachMessage:
    message = session.get(OutreachMessage, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail=f"Outreach message {message_id} not found")
    return message


def _require_status(message: OutreachMessage, allowed: set[OutreachStatus], action: str) -> None:
    if message.status not in allowed:
        allowed_text = " or ".join(sorted(s.value for s in allowed))
        raise HTTPException(
            status_code=409,
            detail=f"Cannot {action} a message that is '{message.status.value}'; "
            f"it must be {allowed_text}.",
        )


def _save(session: Session, message: OutreachMessage) -> OutreachMessageRead:
    session.add(message)
    session.commit()
    session.refresh(message)
    return OutreachMessageRead.model_validate(message)


@router.patch("/{message_id}", response_model=OutreachMessageRead)
def edit_message(message_id: int, body: OutreachPatch, session: SessionDep) -> OutreachMessageRead:
    message = _get_message(session, message_id)
    _require_status(message, {OutreachStatus.draft}, "edit")
    if body.body is not None:
        try:
            check_connection_note(message.kind, body.body)
        except ConnectionNoteTooLong as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        message.body = body.body
    if "subject" in body.model_fields_set:
        message.subject = body.subject
    return _save(session, message)


@router.post("/{message_id}/approve", response_model=OutreachMessageRead)
def approve_message(message_id: int, session: SessionDep) -> OutreachMessageRead:
    message = _get_message(session, message_id)
    _require_status(message, {OutreachStatus.draft}, "approve")
    message.status = OutreachStatus.approved
    message.approved_at = utcnow()
    return _save(session, message)


@router.post("/{message_id}/reject", response_model=OutreachMessageRead)
def reject_message(message_id: int, session: SessionDep) -> OutreachMessageRead:
    message = _get_message(session, message_id)
    _require_status(message, {OutreachStatus.draft, OutreachStatus.approved}, "reject")
    message.status = OutreachStatus.rejected
    return _save(session, message)


# ---------------------------------------------------------------------------
# Test sends


@router.post("/test-email", response_model=TestEmailRead)
def test_email(request: Request) -> TestEmailRead:
    """Send the email template, with the resume, to the candidate's own SMTP_USER."""
    state = request.app.state
    settings = state.settings
    config = SmtpConfig.from_settings(settings)
    problem = config.problem()
    if problem:
        raise HTTPException(status_code=409, detail=f"{problem}.")
    if not settings.resume_path.is_file():
        raise HTTPException(status_code=409, detail="The resume file (RESUME_PATH) is missing.")
    with Session(state.engine) as session:
        template = email_template(session)
    if template is None:
        raise HTTPException(status_code=409, detail="There is no email template in the Template Library.")
    try:
        message = build_email(
            template, config=config, to=config.user, resume_path=settings.resume_path, job_title=TEST_JOB_TITLE, test=True
        )
    except DraftProblem as error:
        raise HTTPException(status_code=409, detail=f"{error}.") from None
    try:
        Mailer(config, getattr(state, "smtp_transport", None)).send(message)
    except MailError as error:
        raise HTTPException(status_code=502, detail=error.text) from None
    return TestEmailRead(sent=True, to=config.user, subject=str(message["Subject"]))


@router.post("/test-note", response_model=TestNoteRead)
def test_note(
    request: Request,
    profile_url: Annotated[str, Query(description="A LinkedIn profile link: https://www.linkedin.com/in/<name>/")],
    dry_run: Annotated[bool, Query(description=DRY_RUN_HELP)] = True,
) -> TestNoteRead:
    url = normalize_profile_url(profile_url)
    if url is None:
        raise HTTPException(status_code=422, detail="profile_url must be a LinkedIn profile link (https://www.linkedin.com/in/...).")
    state = request.app.state
    with Session(state.engine) as session:
        templates = note_templates(session)
        if not templates:
            raise HTTPException(status_code=409, detail="There is no LinkedIn message template for connection notes.")
        job = session.exec(
            select(Job).where(Job.contact_profile_url == url).order_by(col(Job.found_at).desc()).limit(1)
        ).first()
        job_info = linkedin.JobInfo.of(job) if job is not None else None
        if not dry_run:
            if url in linkedin.contacted_profiles(session):
                raise HTTPException(
                    status_code=409, detail="This profile already has a connection note or InMail; notes are never sent twice."
                )
            if sent_today(session, OutreachKind.connection_note) >= linkedin.OUTREACH_DAILY_CAP:
                raise HTTPException(
                    status_code=409, detail=f"Today's limit of {linkedin.OUTREACH_DAILY_CAP} connection notes is reached."
                )
            month_cap = state.settings.connection_notes_monthly_cap
            if sent_this_month(session, OutreachKind.connection_note) >= month_cap:
                raise HTTPException(status_code=409, detail=f"This month's limit of {month_cap} connection notes is reached.")
    if state.scans.running_for(Platform.linkedin):  # a Naukri scan uses its own browser
        raise HTTPException(status_code=409, detail="A LinkedIn scan is running; try again when it has finished.")
    connections = state.connections
    connections.sync_from_chrome(Platform.linkedin)
    if not connections.login_usable(Platform.linkedin):
        raise HTTPException(status_code=409, detail="LinkedIn isn't connected. Connect it first.")
    profile_dir = state.browsers.profile_dir(Platform.linkedin)
    headless = state.settings.scan_headless

    def task(driver):
        browser = driver.open(profile_dir=profile_dir, headless=headless)
        try:
            if not LINKEDIN_LOGIN.has_auth_cookie(browser.cookies()):
                raise LoginWall(LOGIN_EXPIRED)
            return linkedin.send_connection_note(
                browser, url, lambda name: linkedin.build_note(templates, job_info, name), dry_run=dry_run
            )
        finally:
            browser.close()

    try:
        attempt = state.browsers.worker(Platform.linkedin.value).submit(task).result(timeout=NOTE_TIMEOUT_SECONDS)
    except LoginWall as error:
        message = SECURITY_CHECK if "security check" in str(error).lower() else LOGIN_EXPIRED
        connections.record_login_lost(Platform.linkedin, message)
        raise HTTPException(status_code=409, detail=message) from None
    except ScanBlocked as error:
        raise HTTPException(status_code=502, detail=str(error)) from None
    except DraftProblem as error:
        raise HTTPException(status_code=409, detail=f"{error}.") from None
    if not dry_run:
        linkedin.record_note(
            state.engine, attempt, profile_url=url, name=attempt.name, job_id=job_info.id if job_info else None
        )
    return TestNoteRead(
        result=attempt.result.value,
        detail=attempt.detail,
        note=attempt.note,
        buttons_found=attempt.buttons,
        profile_url=url,
        name=attempt.name,
    )
