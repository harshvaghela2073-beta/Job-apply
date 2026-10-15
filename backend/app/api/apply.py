"""Apply to one job now: a Naukri one-click job or a LinkedIn Easy Apply job (also used to try the
flow on a single job first, with ``dry_run``)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlmodel import Session

from app.models import ApplyType, Job, JobStatus, Platform
from app.scanner import applying
from app.scanner.applying import StoredJob, record_attempt
from app.scanner.base import LoginWall, ScanBlocked
from app.schemas import ApplyAttemptRead, JobRead
from app.sources.linkedin import easy_apply as linkedin
from app.services.mirror import refresh_mirror
from app.sources.linkedin.answers import AnswerBook, save_learned
from app.sources.linkedin.login import LINKEDIN_LOGIN
from app.sources.linkedin.screening import load_rules as linkedin_rules
from app.sources.naukri.apply import apply_on_page
from app.sources.naukri.chatbot import recorded_questions
from app.sources.naukri.screening import load_rules as naukri_rules

router = APIRouter(prefix="/jobs", tags=["jobs"])

DRY_RUN_HELP = (
    "Naukri: open the job page and report the Apply button without clicking it, with the answers to the "
    "recruiter questions the job asked on its last try. LinkedIn: fill every step of the Easy Apply form "
    "it can, never submit, and discard the draft."
)
# A LinkedIn form can take several steps with pauses; Naukri's recruiter questions come one at a time.
TIMEOUTS = {Platform.naukri: 300, Platform.linkedin: 300}
NAMES = {Platform.naukri: "Naukri", Platform.linkedin: "LinkedIn"}


@router.post("/{job_id}/apply", response_model=ApplyAttemptRead)
def apply_now(
    job_id: int,
    request: Request,
    dry_run: Annotated[bool, Query(description=DRY_RUN_HELP)] = False,
) -> ApplyAttemptRead:
    state = request.app.state
    with Session(state.engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        platform = Platform(job.platform)
        naukri_job = platform == Platform.naukri and job.apply_type == ApplyType.one_click
        linkedin_job = platform == Platform.linkedin and job.apply_type == ApplyType.easy_apply
        if not (naukri_job or linkedin_job):
            raise HTTPException(
                status_code=422,
                detail="Only Naukri one-click and LinkedIn Easy Apply jobs can be applied to from the app.",
            )
        if job.status == JobStatus.applied:
            raise HTTPException(status_code=409, detail="This job is already applied to.")
        stored = StoredJob(job.url, job.company, job.short_description or "", job.experience or "", job.title)
        answers = AnswerBook.load(session)
        known: list[str] = []  # Naukri dry run: the recruiter questions of the last try
        if linkedin_job:
            rules = linkedin_rules(session)
            cap = state.settings.easy_apply_daily_cap
            if not dry_run and applying.applied_today(session, None, Platform.linkedin, ApplyType.easy_apply) >= cap:
                raise HTTPException(status_code=409, detail=f"Today's Easy Apply limit of {cap} applications is reached.")
        else:
            rules = naukri_rules(session)
            if dry_run:
                known = recorded_questions(session, job.id, job.status_reason)
    name = NAMES[platform]
    if state.scans.running_for(platform):  # the other platform's scan uses its own browser
        raise HTTPException(status_code=409, detail=f"A {name} scan is running; try again when it has finished.")
    connections = state.connections
    connections.sync_from_chrome(platform)
    if not connections.login_usable(platform):
        raise HTTPException(status_code=409, detail=f"{name} isn't connected. Connect it first.")

    profile_dir = state.browsers.profile_dir(platform)
    headless = state.settings.scan_headless

    def task(driver):
        browser = driver.open(profile_dir=profile_dir, headless=headless)
        try:
            if linkedin_job:
                if not LINKEDIN_LOGIN.has_auth_cookie(browser.cookies()):
                    raise LoginWall(linkedin.LOGIN_EXPIRED)
                return linkedin.apply_easy(browser, stored.url, answers, dry_run=dry_run, rules=rules, stored=stored)
            return apply_on_page(
                browser, stored.url, dry_run=dry_run, rules=rules, stored=stored, answers=answers, known_questions=known
            )
        finally:
            browser.close()

    try:
        attempt = state.browsers.worker(platform.value).submit(task).result(timeout=TIMEOUTS[platform])
    except LoginWall as error:
        message = f"The saved {name} login has expired. Connect again."
        if "security check" in str(error).lower():
            message = linkedin.SECURITY_CHECK
        connections.record_login_lost(platform, message)
        raise HTTPException(status_code=409, detail=message) from None
    except ScanBlocked as error:
        raise HTTPException(status_code=502, detail=str(error)) from None

    with Session(state.engine) as session:
        job = session.get(Job, job_id)
        if not dry_run:
            record_attempt(session, job, attempt)
        if answers.learned:
            # Profile-based answers go to the Template Library for the next form (dry runs too).
            attempt.facts["learned_answers"] = [{"question": q, "answer": a} for q, a in answers.learned]
            if save_learned(session, answers.learned):
                session.commit()
                refresh_mirror(session, state.settings)
        session.commit()
        session.refresh(job)
        return ApplyAttemptRead(
            result=attempt.result.value,
            detail=attempt.detail,
            questions=attempt.questions,
            facts=attempt.facts,
            job=JobRead.model_validate(job),
        )
