"""Applying to Naukri jobs with Naukri's own one-click "Apply" button, using the saved login.

The candidate turned this on on 2026-10-13 ("if you find one click apply then do it"). Rules:

- Only jobs found today with apply type ``one_click`` and status ``found``, most important
  (lowest ``priority``) first.
- At most ``APPLY_MAX_PER_RUN`` per run and ``NAUKRI_DAILY_CAP`` per day, with a random
  ``APPLY_WAIT_SECONDS`` pause between applications.
- Recruiter questions (the chatbot drawer) are answered from the profile and the Template Library
  (``chatbot.py``, asked for on 2026-10-14); a question without an answer is never guessed: the job
  becomes ``needs_manual`` and the question goes to the Template Library's unanswered questions.
- The run stops at a login wall, a block page or Naukri's daily quota.
- ``pause_naukri`` (a setting, 2026-10-14) pauses the search and auto-apply without opening a browser.

Selectors and texts live in ``selectors.py`` (``APPLY_*``, ``CHATBOT_*``); they need a live check.
"""

import logging
import random
from collections.abc import Sequence

from sqlmodel import Session

from app.browser import BrowserSession, BrowserUnavailable
from app.models import ApplyType, Job, Platform, ScanKind, ScanStatus
from app.scanner import applying
from app.scanner.applying import ApplyAttempt, ApplyResult, StoredJob, record_attempt  # noqa: F401 - re-exported
from app.scanner.base import LoginWall, ScanBlocked, ScanContext, ScanOutcome, capture_failure
from app.services.app_settings import AUTO_APPLY_NAUKRI, PAUSE_NAUKRI, get_flag
from app.services.mirror import refresh_mirror
from app.sources.html import first, select_any, soup, text_of, visible_text
from app.sources.linkedin.answers import AnswerBook, save_learned
from app.sources.naukri import selectors as S
from app.sources.naukri.chatbot import answer_questions, preview_answers
from app.sources.naukri.login import NAUKRI_LOGIN
from app.sources.naukri.parse import (
    already_applied,
    apply_outcome,
    block_reason,
    job_closed,
    page_logged_out,
    parse_job_page_apply,
)
from app.sources.naukri.screening import ApplyRules, load_rules, parse_job_facts, skip_reason

logger = logging.getLogger(__name__)

APPLY_MAX_PER_RUN = 10
APPLY_WAIT_SECONDS = (20.0, 45.0)
CONFIRM_TIMEOUT_SECONDS = 20
QUESTION_WAIT_SECONDS = 10
LATE_BUTTON_SECONDS = 5  # the header can render after the page "loads"
PAUSED = "Naukri is paused."


def paused(session: Session) -> bool:
    return get_flag(session, PAUSE_NAUKRI)


def _page_text(html: str) -> str:
    return visible_text(html).lower()


def _questions(html: str) -> list[str]:
    doc = soup(html)
    seen: list[str] = []
    for node in select_any(doc, S.APPLY_QUESTION):
        text = " ".join(text_of(node).split())
        if text and text not in seen and not any(t in text.lower() for t in S.APPLY_QUESTIONNAIRE_TEXT):
            seen.append(text)
    return seen[:10]


def _drawer_open(html: str) -> bool:
    return first(soup(html), S.APPLY_QUESTIONNAIRE) is not None or any(
        t in _page_text(html) for t in S.APPLY_QUESTIONNAIRE_TEXT
    )


def _state_after_click(html: str, url: str) -> ApplyAttempt | None:
    """What the page shows after clicking Apply, or None while nothing has changed yet."""
    outcome = apply_outcome(html, url)
    if outcome is not None:
        return outcome
    if _drawer_open(html):
        return ApplyAttempt(ApplyResult.questionnaire, "Naukri asked the recruiter's questions.", _questions(html))
    return None


def _after_failed_click(browser: BrowserSession) -> ApplyAttempt | None:
    """A click that "failed" because the page navigated mid-click (seen live on 2026-10-14: the job was
    applied and the page went to saveApply). What the page shows a few seconds later, or None."""
    for _ in range(LATE_BUTTON_SECONDS):
        browser.wait(1.0)
        try:
            state = _state_after_click(browser.html(), browser.current_url())
        except Exception:  # noqa: BLE001 - still navigating
            continue
        if state is not None:
            return state
    return None


def apply_on_page(
    browser: BrowserSession,
    url: str,
    *,
    dry_run: bool = False,
    kind: str = "naukri_apply",
    rules: ApplyRules | None = None,
    stored: StoredJob | None = None,
    answers: AnswerBook | None = None,
    known_questions: Sequence[str] = (),
) -> ApplyAttempt:
    """Open a Naukri job page with the saved login and click its one-click Apply button, unless one
    of the candidate's skip rules matches (``rules``). With ``answers``, the recruiter's questions
    are answered from the profile and Template Library; without, the job is left for the candidate.

    A dry run never clicks Apply (on a job without questions that click applies). It previews the
    answers to ``known_questions``, the questions the job asked on its last try.

    Raises ``LoginWall`` / ``ScanBlocked`` when the login is gone or Naukri blocks the page.
    """
    selectors = ", ".join(S.APPLY_BUTTON + S.COMPANY_SITE_BUTTON + S.APPLIED_MARKER)
    page = browser.goto(url, wait_for=selectors, timeout=40)
    if NAUKRI_LOGIN.is_login_wall(page.url):
        raise LoginWall("Naukri redirected to its login page; the saved login has expired.")
    reason = block_reason(page)
    if reason:
        raise ScanBlocked(reason)

    html = browser.html()
    if page_logged_out(html):
        raise LoginWall("Naukri shows the job page logged out; the saved login has expired.")
    apply_type = parse_job_page_apply(html)
    for _ in range(LATE_BUTTON_SECONDS if apply_type is None and not job_closed(html) else 0):
        browser.wait(1.0)
        html = browser.html()
        apply_type = parse_job_page_apply(html)
        if apply_type is not None:
            break
    if already_applied(html):
        return ApplyAttempt(ApplyResult.already_applied, "Naukri shows this job as already applied.")
    if apply_type == ApplyType.company_site:
        return ApplyAttempt(ApplyResult.company_site, "This job is applied to on the company's site.")
    if apply_type is None:
        if closed := job_closed(html):
            return ApplyAttempt(ApplyResult.skipped, f'The job no longer takes applications (Naukri says "{closed}").')
        note = capture_failure(browser, kind)
        return ApplyAttempt(ApplyResult.no_button, "No Apply button on the job page." + note)
    facts = parse_job_facts(html)
    if rules is not None:
        reason = skip_reason(
            rules,
            facts,
            stored_company=stored.company if stored else "",
            stored_text=stored.text if stored else "",
            stored_experience=stored.experience if stored else "",
        )
        if reason:
            return ApplyAttempt(ApplyResult.skipped, reason, facts=facts.as_dict())
    if dry_run:
        return _dry_run(facts.as_dict(), answers, known_questions)

    if not browser.click(", ".join(S.APPLY_BUTTON)):
        why = getattr(browser, "last_click_problem", "")
        state = _after_failed_click(browser)
        if state is None:
            note = capture_failure(browser, kind)
            return ApplyAttempt(
                ApplyResult.no_button, f"The Apply button couldn't be clicked ({why or 'unknown reason'})." + note
            )
        logger.info("Naukri's Apply click reported %r, but the page then showed %s", why[:200], state.result)
    else:
        state = None

    waited = 0.0
    while state is None and waited < CONFIRM_TIMEOUT_SECONDS:
        browser.wait(1.0)
        waited += 1.0
        browser.close_extra_pages()
        state = _state_after_click(browser.html(), browser.current_url())
    if state is None:
        note = capture_failure(browser, kind)
        return ApplyAttempt(ApplyResult.no_confirmation, "Clicked Apply but Naukri showed no confirmation." + note)
    state.clicked = True
    if state.result != ApplyResult.questionnaire:
        return state
    if answers is not None:
        return answer_questions(browser, answers, kind=kind, facts=facts.as_dict())
    # The drawer opens with a typing indicator; give the questions a few seconds to appear.
    for _ in range(QUESTION_WAIT_SECONDS):
        if state.questions:
            break
        browser.wait(1.0)
        state.questions = _questions(browser.html())
    state.detail += capture_failure(browser, kind)
    return state


def _dry_run(facts: dict, answers: AnswerBook | None, known_questions: Sequence[str]) -> ApplyAttempt:
    detail = "Found Naukri's one-click Apply button (not clicked: dry run)."
    unanswered: list[str] = []
    if known_questions and answers is not None:
        rows = preview_answers(answers, known_questions)
        facts["questions"] = rows
        unanswered = [row["question"] for row in rows if row["answer"] is None]
        detail += f" On its last try it asked {len(rows)} recruiter question(s); " + (
            f"{len(unanswered)} still have no answer." if unanswered else "all have an answer now."
        )
    else:
        detail += " Naukri shows any recruiter questions only after Apply is clicked."
    return ApplyAttempt(ApplyResult.would_apply, detail, unanswered, facts)


def applied_today(session: Session, now) -> int:
    return applying.applied_today(session, now, Platform.naukri, ApplyType.one_click)


def pending_jobs(session: Session, now, limit: int) -> list[Job]:
    return applying.pending_jobs(session, now, limit, Platform.naukri, ApplyType.one_click)


class NaukriApplier:
    """Runs after the Naukri scan: applies to today's one-click jobs while auto-apply is on."""

    kind = ScanKind.naukri_apply
    platform = Platform.naukri

    def run(self, ctx: ScanContext) -> ScanOutcome:
        with Session(ctx.engine) as session:
            if paused(session):
                return ScanOutcome(status=ScanStatus.skipped, message=PAUSED)
            if not get_flag(session, AUTO_APPLY_NAUKRI):
                return ScanOutcome(status=ScanStatus.skipped, message="Auto-apply is off.")
            remaining = ctx.settings.naukri_daily_cap - applied_today(session, ctx.now)
            if remaining <= 0:
                return ScanOutcome(
                    status=ScanStatus.skipped,
                    message=f"Today's Naukri limit of {ctx.settings.naukri_daily_cap} applications is reached.",
                )
            jobs = pending_jobs(session, ctx.now, min(remaining, APPLY_MAX_PER_RUN))
            rules = load_rules(session)
            answers = AnswerBook.load(session)  # once per run; learned answers are saved at the end
        if not jobs:
            return ScanOutcome(status=ScanStatus.ok, message="No new one-click jobs to apply to.")
        ctx.connections.sync_from_chrome(self.platform)
        if not ctx.connections.login_usable(self.platform):
            return ScanOutcome(status=ScanStatus.skipped, message="Naukri isn't connected, so nothing was applied.")

        profile_dir = ctx.browsers.profile_dir(self.platform)
        targets = [
            (job.id, StoredJob(job.url, job.company, job.short_description or "", job.experience or ""), job.title)
            for job in jobs
        ]

        def task(driver):
            browser = driver.open(profile_dir=profile_dir, headless=ctx.settings.scan_headless)
            results: list[tuple[int, ApplyAttempt]] = []
            failure = None
            try:
                clicked = False
                for job_id, stored, title in targets:
                    if ctx.stop.is_set():
                        break
                    if clicked:  # pause between applications, not after a skipped job
                        browser.wait(random.uniform(*APPLY_WAIT_SECONDS))
                    attempt = apply_on_page(
                        browser, stored.url, kind=self.kind.value, rules=rules, stored=stored, answers=answers
                    )
                    clicked = attempt.result != ApplyResult.skipped
                    logger.info("Naukri apply %s at %s: %s %s", title, stored.company, attempt.result, attempt.detail)
                    results.append((job_id, attempt))
                    if attempt.result == ApplyResult.quota_reached:
                        break
            except LoginWall as error:
                failure = str(error)
                ctx.connections.record_login_lost(self.platform, "The saved Naukri login has expired. Connect again.")
            except (ScanBlocked, BrowserUnavailable) as error:
                failure = str(error) + capture_failure(browser, self.kind.value)
            except Exception as error:  # noqa: BLE001 - keep what was done so far
                logger.exception("Naukri apply stopped")
                failure = f"Applying stopped: {str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__}"
            finally:
                browser.close()
            return results, failure

        results, failure = ctx.browsers.worker(self.platform.value).submit(task).result()
        counts: dict[ApplyResult, int] = {}
        with Session(ctx.engine) as session:
            for job_id, attempt in results:
                job = session.get(Job, job_id)
                if job is not None:
                    record_attempt(session, job, attempt)
                counts[attempt.result] = counts.get(attempt.result, 0) + 1
            learned = save_learned(session, answers.learned)
            session.commit()
            if learned:
                refresh_mirror(session, ctx.settings)

        applied = counts.get(ApplyResult.applied, 0)
        parts = [f"Applied to {applied}"]
        labels = {
            ApplyResult.already_applied: "already applied",
            ApplyResult.skipped: "skipped by your rules",
            ApplyResult.questionnaire: "need your answers (recruiter questions)",
            ApplyResult.company_site: "turned out to be company-site jobs",
            ApplyResult.no_button: "had no Apply button",
            ApplyResult.no_confirmation: "showed no confirmation",
            ApplyResult.quota_reached: "hit Naukri's daily limit",
        }
        parts += [f"{counts[r]} {label}" for r, label in labels.items() if counts.get(r)]
        message = ", ".join(parts) + "."
        if learned:
            message += f" Saved {learned} answer(s) worked out from your profile to the Template Library."
        if failure:
            message += f" Stopped: {failure}"
        status = ScanStatus.failed if failure and not results else ScanStatus.ok
        return ScanOutcome(status=status, found=len(results), new=applied, message=message)
