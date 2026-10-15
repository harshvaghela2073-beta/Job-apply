"""LinkedIn Easy Apply with the saved login (docs/API_CONTRACT.md, "LinkedIn search and Easy Apply";
decided by the candidate on 2026-10-13).

On ``https://www.linkedin.com/jobs/view/<id>/``:

1. "Applied ..." on the page -> ``already_applied``; an external "Apply" -> ``company_site``;
   "No longer accepting applications" -> ``skipped``; then the skip rules (``screening.py``).
2. Click "Easy Apply" and walk the modal, at most ``MAX_STEPS`` steps. Each step is ONE bounded page
   read (``BrowserSession.form_state``: the fields, the dialog's buttons and texts); fields are
   filled from ``answers.AnswerBook`` only. Then the footer button, found by aria-label or text
   inside the dialog and clicked by its handle: Next -> Review -> Submit application.
   LinkedIn's job pages keep a CPU core busy (2026-10-14), so every click and read runs from the
   page's own script with a time limit; Playwright's locator waits (visible, stable) time out there.
3. After Next / Review, LinkedIn's inline errors are read. A numeric experience field that wants a
   whole number gets 2 instead of 1.9 (once); any other rejected value makes the field unanswerable.
4. If a required field has no answer, the form is closed (Dismiss -> Discard), the job becomes
   ``needs_manual`` and the questions go to the unanswered questions. **Nothing is submitted with a
   guessed value.**
5. Before Submit, "Follow <company>" is unticked. Success = "Application sent" / "Your application
   was sent". The resume LinkedIn preselects is used; nothing is uploaded.

A dry run walks every step it can but never clicks Submit, then discards the draft; ``facts["fields"]``
lists every field with the value chosen (or null) and why.

``LinkedInApplier`` (scan kind ``linkedin_apply``) runs this for today's ``easy_apply`` jobs while the
``auto_apply_linkedin`` switch is on: at most ``APPLY_MAX_PER_RUN`` per run and
``EASY_APPLY_DAILY_CAP`` per day, a random 45-90 s pause after each application that clicked Easy
Apply, and it stops at a login wall, a security check, a block or LinkedIn's daily limit.

Selectors and texts live in ``selectors.py``; they need a live check.
"""

import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from sqlmodel import Session

from app.browser import BrowserSession, BrowserUnavailable
from app.browser.driver import PageResult
from app.models import ApplyType, Job, Platform, ScanKind, ScanStatus
from app.scanner import applying
from app.scanner.applying import ApplyAttempt, ApplyResult, StoredJob, record_attempt
from app.services.mirror import refresh_mirror
from app.scanner.base import LoginWall, ScanBlocked, ScanContext, ScanOutcome, capture_failure
from app.services.app_settings import AUTO_APPLY_LINKEDIN, get_flag
from app.sources.html import first, soup, text_of, visible_text
from app.sources.linkedin import selectors as S
from app.sources.linkedin.answers import save_learned, Answer, AnswerBook, FormField, norm
from app.sources.linkedin.login import LINKEDIN_LOGIN
from app.sources.linkedin.parse import parse_hiring_team, pick_contact
from app.scanner.matching import estimate_posted_at
from app.sources.linkedin.screening import ApplyRules, load_rules, parse_job_facts, skip_reason
from app.timeutils import to_utc, utcnow

logger = logging.getLogger(__name__)

APPLY_MAX_PER_RUN = 5
APPLY_WAIT_SECONDS = (45.0, 90.0)
MAX_STEPS = 8
MAX_ROUNDS = 16  # steps plus re-tries after inline errors
MODAL_WAIT_SECONDS = 15
# LinkedIn's job page can keep its main thread busy for minutes (seen live on 2026-10-14: one CPU core
# at 100%): page calls queue behind it and run late, so opening the form is waited for patiently.
OPEN_WAIT_SECONDS = 90
STEP_WAIT_SECONDS = 1.5
MAX_SILENT_READS = 3  # form reads in a row the page didn't answer before giving up
CONFIRM_TIMEOUT_SECONDS = 15
MAX_FAILED_IN_A_ROW = 2
OLD_JOB = timedelta(hours=36)  # older than this per its job page: not one of today's jobs

LOGIN_EXPIRED = "The saved LinkedIn login has expired. Connect again."
SECURITY_CHECK = "LinkedIn asked for a security check. Connect again and complete it in the window."
_APPLIED = re.compile(S.APPLIED_PATTERN, re.I)


# ---------------------------------------------------------------------------
# Reading the page


@dataclass
class JobPageState:
    applied: bool = False
    easy_apply: bool = False
    external_apply: bool = False
    external_url: str | None = None  # the company's apply page behind an external "Apply"
    closed: bool = False


def _status_blocks(doc) -> list[str]:
    """The text of the block around each "Application status" heading (the 2026-10-14 layout has no
    class names to find it by): "Application submitted 1 hour ago", "Applied 3 days ago"."""
    texts = []
    for string in doc.find_all(string=lambda s: s is not None and s.strip().lower() == S.APPLICATION_STATUS_HEADING):
        box = string.parent
        for _ in range(4):
            if box is None or box.parent is None:
                break
            box = box.parent
            text = text_of(box)
            if len(text) > len(S.APPLICATION_STATUS_HEADING) + 5:
                texts.append(text[:500])
                break
    return texts


def external_target(href: str | None) -> str | None:
    """The page an external "Apply" link leads to (unwrapping LinkedIn's safety redirect)."""
    if not href or not href.startswith("http"):
        return None
    if S.SAFETY_REDIRECT in href:
        target = parse_qs(urlparse(href).query).get("url", [""])[0]
        return target if target.startswith("http") else None
    return None if "linkedin.com" in urlparse(href).netloc else href


def posted_too_long_ago(posted: str, now) -> bool:
    """The job page says it was posted more than a day and a half ago (promoted listings show up in
    the 24-hour searches; 2026-10-15: a Cisco job "2 weeks ago")."""
    moment = estimate_posted_at(posted, now) if posted else None
    return moment is not None and to_utc(now) - moment > OLD_JOB


def read_job_page(html: str) -> JobPageState:
    doc = soup(html)
    state = JobPageState()
    areas = [text_of(node) for selector in S.APPLIED_MARKER for node in doc.select(selector)]
    areas.append(text_of(first(doc, S.JOB_TOP_CARD)))
    areas.extend(_status_blocks(doc))
    state.applied = any(_APPLIED.search(text) for text in areas if text)
    for selector in S.APPLY_BUTTON:
        for button in doc.select(selector):
            label = " ".join((button.get("aria-label") or text_of(button)).split()).lower()
            if S.EASY_APPLY_TEXT in label:
                state.easy_apply = True
            elif "apply" in label:
                state.external_apply = True
                state.external_url = state.external_url or external_target(button.get("href"))
    top = " ".join(areas).lower() + " " + " ".join(text_of(n) for s in S.CLOSED_MARKER for n in doc.select(s)).lower()
    state.closed = any(text in top for text in S.CLOSED_TEXT)
    return state


@dataclass
class ModalView:
    """One read of the Easy Apply dialog (``BrowserSession.form_state``)."""

    present: bool = False
    # next | review | submit | done | dismiss | continue | discard -> the button's handle (its
    # ``id`` for ``click_handle``). Only buttons inside the dialog (and other open dialogs, for
    # "discard") count: the job page has its own "Next" (a carousel) outside it.
    buttons: dict[str, str] = field(default_factory=dict)
    fields: list[FormField] = field(default_factory=list)
    success: bool = False
    limit: bool = False
    discard: str | None = None  # the Discard button of "Save this application?", when shown
    header: str = ""
    progress: str = ""
    responded: bool = True  # False: the page didn't answer in time

    @property
    def signature(self) -> tuple:
        """What tells one step from the next."""
        return (self.header, self.progress, tuple(f.key for f in self.fields))


def _button_kind(button: dict) -> str | None:
    if button.get("disabled"):
        return None
    for name in (button.get("label"), button.get("text")):
        kind = S.BUTTON_KINDS.get(" ".join(str(name or "").split()).lower())
        if kind:
            return kind
    return None


def _classify(buttons, into: dict[str, str]) -> None:
    for button in buttons or ():
        kind = _button_kind(button)
        if kind and kind not in into and button.get("id"):
            into[kind] = str(button["id"])


def read_state(state: dict | None) -> ModalView:
    """The driver's ``form_state`` result as a ``ModalView``; None (no answer) -> ``responded`` False."""
    if state is None:
        return ModalView(responded=False)
    view = ModalView(present=bool(state.get("present")))
    _classify(state.get("buttons"), view.buttons)
    texts = [str(state.get("text") or "")]
    others: dict[str, str] = {}
    for dialog in state.get("dialogs") or ():
        texts.append(str(dialog.get("text") or ""))
        _classify(dialog.get("buttons"), others)
    view.discard = view.buttons.pop("discard", None) or others.get("discard")
    if not view.present and "done" in others:  # a success dialog shown instead of the form
        view.buttons.setdefault("done", others["done"])
    text = " ".join(texts).lower()
    hits = [str(t).lower() for t in state.get("page_hits") or ()]
    view.success = any(t in text for t in S.SUCCESS_TEXT) or any(t in hits for t in S.SUCCESS_TEXT)
    view.limit = any(t in text for t in S.LIMIT_TEXT)
    view.header = " ".join(str(state.get("header") or "").split())
    view.progress = " ".join(str(state.get("progress") or "").split())
    view.fields = [FormField.from_dict(d) for d in state.get("fields") or ()] if view.present else []
    return view


def _read(browser: BrowserSession, *, watch_success: bool = False) -> ModalView:
    """One bounded read of the Easy Apply dialog (fields, buttons and texts in one page call)."""
    hints = {**S.FORM_HINTS, "watch_texts": list(S.SUCCESS_TEXT)} if watch_success else S.FORM_HINTS
    return read_state(browser.form_state(S.MODAL_SCOPE, hints))


def _press(browser: BrowserSession, view: ModalView, kind: str) -> bool:
    """Click the dialog's ``kind`` button from ``view``. A click that didn't answer is retried only
    while the same step is still shown (it may have gone through late; never click twice into the
    next step)."""
    handle = view.buttons.get(kind)
    if handle:
        if browser.click_handle(handle):
            return True
        browser.wait(1.0)
        again = _read(browser)
        if not again.responded:
            return False
        if again.signature != view.signature or not again.present:
            return True  # the step changed: the first click went through after all
    else:
        again = _read(browser)
    handle = again.buttons.get(kind)
    return bool(handle) and browser.click_handle(handle)


def _block_reason(result: PageResult) -> str | None:
    if result.status in S.BLOCK_STATUSES:
        return f"LinkedIn returned HTTP {result.status} to the job page; stopped."
    return None


def check_job_page(result: PageResult) -> None:
    """Raise ``LoginWall`` / ``ScanBlocked`` when the job page is a login wall, a security check or
    a block."""
    if LINKEDIN_LOGIN.is_security_check(result.url):
        raise LoginWall("LinkedIn showed a security check; stopped.")
    if LINKEDIN_LOGIN.is_login_wall(result.url):
        raise LoginWall("LinkedIn redirected to its login page; the saved login has expired.")
    reason = _block_reason(result)
    if reason:
        raise ScanBlocked(reason)


def is_follow_company(field: FormField) -> bool:
    if field.dom_id == S.FOLLOW_COMPANY_ID:
        return True
    return field.type == "checkbox" and re.search(S.FOLLOW_COMPANY_LABEL, norm(field.label)) is not None


# ---------------------------------------------------------------------------
# The form


class FieldReport:
    """``facts["fields"]``: one row per field and step, the latest answer winning."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._index: dict[tuple[int, str], int] = {}

    def add(self, step: int, field: FormField, answer: Answer) -> None:
        row = {
            "step": step,
            "label": field.label,
            "type": field.type,
            "required": field.required,
            "value": answer.value,
            "reason": answer.reason,
        }
        if field.options:
            row["options"] = list(field.options[:12])
        if field.error:
            row["error"] = field.error
        key = (step, field.key)
        if key in self._index:
            self.rows[self._index[key]] = row
        else:
            self._index[key] = len(self.rows)
            self.rows.append(row)

    def note(self, step: int, field: FormField, text: str) -> None:
        index = self._index.get((step, field.key))
        if index is not None:
            self.rows[index]["reason"] += f"; {text}"


def _control(field: FormField) -> str:
    """Which box on the page: its page id when it has one. A form can ask the same question twice
    (TCS, 2026-10-14: five questions repeated), so the question text alone mixes up the two boxes."""
    return f"{field.dom_id}|{field.key}" if field.dom_id else field.key


def _holds(field: FormField, value: str) -> bool:
    if field.type == "checkbox" and not field.options:
        return field.checked == (value == "true")
    return field.value.strip() == value.strip()


def _question(field: FormField) -> str:
    return field.label or f"(unlabelled {field.type} field)"


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def discard(browser: BrowserSession) -> str:
    """Close the Easy Apply form without submitting: Dismiss, then Discard in "Save this
    application?" (any open dialog with a Discard button). Returns a note for the detail when it
    didn't work."""
    silent = 0
    for _ in range(6):
        view = _read(browser)
        if not view.responded:
            silent += 1
            if silent >= 2:
                return " The form may still be open (LinkedIn's page stopped responding)."
        elif view.discard:
            if not browser.click_handle(view.discard):
                return " The draft couldn't be discarded (the Discard button didn't respond)."
        elif view.present and "dismiss" in view.buttons:
            if not browser.click_handle(view.buttons["dismiss"]):
                return " The form couldn't be closed (the Dismiss button didn't respond)."
        elif view.present:
            if not browser.click_button(f"^{S.DISMISS_LABEL}$", within=S.DIALOG_SCOPE):
                return " The form couldn't be closed (no Dismiss button)."
        else:
            return ""
        browser.wait(1.0)
    return " The form may still be open."


def _close_after_success(browser: BrowserSession) -> None:
    view = _read(browser)
    for kind in ("done", "dismiss"):
        handle = view.buttons.get(kind)
        if handle and browser.click_handle(handle):
            return


def _wait_for_form(browser: BrowserSession) -> ModalView | None:
    """The form once it shows a step (or the success / limit message). The clock is the browser's
    waits, with a wall-clock cap so slow page calls can't stretch it."""
    deadline = time.monotonic() + OPEN_WAIT_SECONDS
    waited = 0.0
    while waited < MODAL_WAIT_SECONDS and time.monotonic() < deadline:
        browser.wait(1.0)
        waited += 1.0
        browser.close_extra_pages()
        view = _read(browser)
        if view.present and (view.buttons.keys() & {"next", "review", "submit"} or view.success or view.limit):
            return view
        if view.present and "continue" in view.buttons:  # "Job search safety reminder" before the form
            browser.click_handle(view.buttons["continue"])
    return None


def _untick_follow(browser: BrowserSession, follow: FormField | None) -> str:
    if follow is None:
        return "not shown"
    if not follow.checked:
        return "was not ticked"
    if browser.fill_field(follow.raw, "false", S.FORM_HINTS):
        current = next((f for f in _read(browser).fields if is_follow_company(f)), None)
        if current is None or not current.checked:
            return "unticked"
    return "couldn't untick"


def _confirm(browser: BrowserSession, kind: str, facts: dict, steps: int) -> ApplyAttempt:
    applied = ApplyAttempt(
        ApplyResult.applied, f"Applied with LinkedIn Easy Apply ({steps} step(s)).", facts=facts, clicked=True
    )
    waited = 0.0
    deadline = time.monotonic() + CONFIRM_TIMEOUT_SECONDS * 3
    while waited < CONFIRM_TIMEOUT_SECONDS and time.monotonic() < deadline:
        browser.wait(1.0)
        waited += 1.0
        if _read(browser, watch_success=True).success:
            _close_after_success(browser)
            return applied
    # Last look at the whole page: the job page itself may say "Applied ..." by now.
    html = browser.html()
    if any(t in visible_text(html).lower() for t in S.SUCCESS_TEXT) or read_job_page(html).applied:
        _close_after_success(browser)
        return applied
    note = capture_failure(browser, kind)
    return ApplyAttempt(
        ApplyResult.no_confirmation, "Clicked Submit but LinkedIn showed no confirmation." + note, facts=facts, clicked=True
    )


def _fail(browser: BrowserSession, kind: str, detail: str, facts: dict) -> ApplyAttempt:
    note = capture_failure(browser, kind)
    detail = detail + " The draft was discarded; nothing was sent." + note + discard(browser)
    return ApplyAttempt(ApplyResult.no_confirmation, detail, facts=facts, clicked=True)


def walk_form(browser: BrowserSession, answers: AnswerBook, *, dry_run: bool, kind: str, facts: dict) -> ApplyAttempt:
    """Fill the open Easy Apply form step by step, then submit (or, in a dry run, discard).

    Each step is one bounded page read (``BrowserSession.form_state``: fields, the dialog's buttons
    and texts) and clicks by the buttons' handles, scoped to the dialog."""
    if _wait_for_form(browser) is None:
        return _fail(browser, kind, "The Easy Apply form didn't open.", facts)
    report = FieldReport()
    facts["fields"] = report.rows
    filled: dict[str, str] = {}
    steps = 1
    last_signature = None
    stalled = silent = 0
    for _ in range(MAX_ROUNDS):
        view = _read(browser)
        if not view.responded:
            silent += 1
            if silent >= MAX_SILENT_READS:
                return _fail(browser, kind, "LinkedIn's page stopped responding while the form was open.", facts)
            browser.wait(STEP_WAIT_SECONDS)
            continue
        silent = 0
        if view.success:
            _close_after_success(browser)
            return ApplyAttempt(ApplyResult.applied, "Applied with LinkedIn Easy Apply.", facts=facts, clicked=True)
        if view.limit:
            note = discard(browser)
            return ApplyAttempt(
                ApplyResult.quota_reached, "LinkedIn says today's Easy Apply limit is reached." + note,
                facts=facts, clicked=True,
            )
        if not view.present:
            note = capture_failure(browser, kind)
            return ApplyAttempt(
                ApplyResult.no_confirmation, "The Easy Apply form closed unexpectedly." + note, facts=facts, clicked=True
            )
        fields = view.fields
        signature = view.signature
        has_errors = any(f.error for f in fields)
        if last_signature is not None:
            if signature != last_signature:
                steps += 1
                stalled = 0
            elif not has_errors:
                stalled += 1
                if stalled >= 2:
                    return _fail(browser, kind, f"The form didn't move past step {steps}.", facts)
                browser.wait(STEP_WAIT_SECONDS)
                continue
        if steps > MAX_STEPS:
            return _fail(browser, kind, f"The form has more than {MAX_STEPS} steps.", facts)
        last_signature = signature

        follow = None
        unanswered: list[FormField] = []
        problems: list[str] = []
        changed = False
        for f in fields:
            if is_follow_company(f):
                follow = f
                continue
            answer = answers.answer(f)
            if answer.value is None and f.value.strip() and not f.error and f.value.strip().lower() != "select an option":
                # LinkedIn prefilled it from your account (e.g. the account's email): keep that.
                answer = Answer(f.value.strip(), "kept the value LinkedIn prefilled from your account")
            if answer.value is not None and f.error and filled.get(_control(f)) == answer.value:
                answer = Answer(None, f"no answer: LinkedIn rejected {answer.value!r} ({f.error})")
            report.add(steps, f, answer)
            if answer.value is None:
                if f.required or f.error:
                    unanswered.append(f)
                continue
            if _holds(f, answer.value) and not f.error:
                filled[_control(f)] = answer.value
                continue
            changed = True
            if browser.fill_field(f.raw, answer.value, S.FORM_HINTS):
                filled[_control(f)] = answer.value
            else:
                report.note(steps, f, "couldn't be filled in")
                if f.required:
                    problems.append(_question(f))
        if unanswered:
            questions = _unique([_question(f) for f in unanswered])
            note = discard(browser)
            return ApplyAttempt(
                ApplyResult.questionnaire,
                f"LinkedIn's form asks {len(questions)} required question(s) with no answer in your profile or "
                "Template Library; the draft was discarded and nothing was sent." + note,
                questions,
                facts,
                clicked=True,
            )
        if problems:
            return _fail(browser, kind, "Couldn't fill in: " + "; ".join(problems) + ".", facts)

        if changed:  # filling can re-render the footer: read its buttons again
            again = _read(browser)
            if again.responded and again.present:
                view = again
        if "submit" in view.buttons:
            facts["follow_company"] = _untick_follow(browser, follow)
            if facts["follow_company"] == "couldn't untick":
                return _fail(browser, kind, 'Couldn\'t untick "Follow company".', facts)
            if dry_run:
                note = discard(browser)
                return ApplyAttempt(
                    ApplyResult.would_apply,
                    f"Filled all {steps} step(s); stopped before Submit (dry run) and discarded the draft." + note,
                    facts=facts,
                    clicked=True,
                )
            if follow is not None and follow.checked:
                view = _read(browser)  # unticking re-rendered the step
            if not _press(browser, view, "submit"):
                return _fail(browser, kind, "The Submit button couldn't be clicked.", facts)
            return _confirm(browser, kind, facts, steps)
        button = "review" if "review" in view.buttons else "next" if "next" in view.buttons else None
        if button is None:
            return _fail(browser, kind, "The form showed no Next, Review or Submit button.", facts)
        if not _press(browser, view, button):
            return _fail(browser, kind, f'The "{button.title()}" button couldn\'t be clicked.', facts)
        browser.wait(STEP_WAIT_SECONDS)
    return _fail(browser, kind, "The form didn't finish.", facts)


def _open_form(browser: BrowserSession) -> bool:
    """Click the job page's Easy Apply button from the page's own script (one bounded call; on
    2026-10-14 ``Locator.is_visible`` waited 30 s and ``click`` 8 s on a busy page and failed).

    On a busy page the click can time out and still run a minute later (seen live on 2026-10-14:
    the form was open when the tool gave up). So after a click that didn't answer, the form is
    waited for patiently, and the button is clicked again only once, when the page answered and
    shows no form 10 seconds later. False only when the page answered and no click went through;
    a page that stops answering is left to ``walk_form``, which says so."""
    within = ", ".join(S.APPLY_BUTTON)
    if browser.click_button(S.EASY_APPLY_BUTTON_NAME, within=within):
        return True
    deadline = time.monotonic() + OPEN_WAIT_SECONDS * 2
    waited, silent, clicked_again = 0.0, 0, False
    while waited < OPEN_WAIT_SECONDS and time.monotonic() < deadline:
        browser.wait(2.0)
        waited += 2.0
        view = _read(browser)
        if view.present:
            return True  # the click went through after its time limit
        silent = 0 if view.responded else silent + 1
        if silent >= MAX_SILENT_READS:
            return True
        if view.responded and not clicked_again and waited >= 10:
            clicked_again = True
            if browser.click_button(S.EASY_APPLY_BUTTON_NAME, within=within) or browser.click_button(
                S.EASY_APPLY_ANYWHERE_NAME
            ):
                return True
    return False


def apply_easy(
    browser: BrowserSession,
    url: str,
    answers: AnswerBook,
    *,
    dry_run: bool = False,
    rules: ApplyRules | None = None,
    stored: StoredJob | None = None,
    kind: str = ScanKind.linkedin_apply.value,
) -> ApplyAttempt:
    """Open a LinkedIn job page with the saved login and apply with Easy Apply (see the module
    docstring). Raises ``LoginWall`` / ``ScanBlocked`` when the login is gone or LinkedIn blocks."""
    result = browser.goto(url, wait_for=S.JOB_PAGE_READY, timeout=40)
    check_job_page(result)
    html = browser.html()
    state = read_job_page(html)
    if not (state.applied or state.easy_apply or state.external_apply or state.closed):
        browser.wait(3.0)  # the top card renders late on slow loads
        html = browser.html()
        state = read_job_page(html)
    job_facts = parse_job_facts(html)
    facts = job_facts.as_dict()
    team = parse_hiring_team(html)  # "Meet the hiring team": who to send a connection note to later
    if team:
        facts["hiring_team"] = [person.as_dict() for person in team]
    if state.applied:
        return ApplyAttempt(ApplyResult.already_applied, "LinkedIn shows this job as already applied.", facts=facts)
    if not state.easy_apply:
        if state.external_apply:
            if state.external_url:
                facts["apply_url"] = state.external_url
            return ApplyAttempt(
                ApplyResult.company_site, "This job is applied to on the company's site (no Easy Apply).", facts=facts
            )
        if state.closed:
            return ApplyAttempt(ApplyResult.skipped, "LinkedIn says it's no longer accepting applications.", facts=facts)
        note = capture_failure(browser, kind)
        return ApplyAttempt(ApplyResult.no_button, "No Easy Apply button on the job page." + note, facts=facts)
    reason = skip_reason(rules, job_facts, stored) if rules is not None else None
    if reason is None and posted_too_long_ago(job_facts.posted, utcnow()):
        reason = f"LinkedIn says it was posted {job_facts.posted}; not one of today's jobs."
    if reason and not dry_run:
        return ApplyAttempt(ApplyResult.skipped, reason, facts=facts)
    if reason:
        facts["skip_reason"] = reason
    if not _open_form(browser):
        note = capture_failure(browser, kind)
        return ApplyAttempt(ApplyResult.no_button, "The Easy Apply button couldn't be clicked." + note, facts=facts)
    attempt = walk_form(browser, answers, dry_run=dry_run, kind=kind, facts=facts)
    if reason:
        attempt.detail += f" (A real run would skip this job: {reason})"
    return attempt


def remember_hiring_team(job: Job, team: list[dict] | None) -> None:
    """Keep the job page's hiring-team contact for the connection notes (``outreach.py``): the job
    poster first, never a 1st-degree connection. The caller commits."""
    if job.contact_profile_url or not team:
        return
    person = pick_contact(team)
    if person is not None:
        job.contact_name, job.contact_profile_url = person.name, person.profile_url
        job.contact_checked_at = utcnow()


# ---------------------------------------------------------------------------
# The scheduled applier


class LinkedInApplier:
    """Runs after the LinkedIn job search: Easy Apply to today's jobs while auto-apply is on."""

    kind = ScanKind.linkedin_apply
    platform = Platform.linkedin

    def __init__(self, wait_range: tuple[float, float] = APPLY_WAIT_SECONDS, rng=random.uniform):
        self.wait_range = wait_range
        self.rng = rng
        self.pauses: list[float] = []  # the pauses taken in the last run (seconds)

    def _pause(self, browser: BrowserSession, ctx: ScanContext) -> bool:
        """A random pause between applications; False when the run is being stopped."""
        seconds = self.rng(*self.wait_range)
        self.pauses.append(seconds)
        remaining = seconds
        while remaining > 0:
            if ctx.stop.is_set():
                return False
            chunk = min(remaining, 5.0)
            browser.wait(chunk)
            remaining -= chunk
        return not ctx.stop.is_set()

    def run(self, ctx: ScanContext) -> ScanOutcome:
        self.pauses = []
        with Session(ctx.engine) as session:
            if not get_flag(session, AUTO_APPLY_LINKEDIN):
                return ScanOutcome(status=ScanStatus.skipped, message="LinkedIn auto-apply is off.")
            cap = ctx.settings.easy_apply_daily_cap
            remaining = cap - applying.applied_today(session, ctx.now, Platform.linkedin, ApplyType.easy_apply)
            if remaining <= 0:
                return ScanOutcome(
                    status=ScanStatus.skipped, message=f"Today's Easy Apply limit of {cap} applications is reached."
                )
            jobs = applying.pending_jobs(
                session, ctx.now, min(remaining, APPLY_MAX_PER_RUN), Platform.linkedin, ApplyType.easy_apply
            )
            rules = load_rules(session)
            answers = AnswerBook.load(session)
        if not jobs:
            return ScanOutcome(status=ScanStatus.ok, message="No new Easy Apply jobs to apply to.")
        ctx.connections.sync_from_chrome(self.platform)
        if not ctx.connections.login_usable(self.platform):
            return ScanOutcome(status=ScanStatus.skipped, message="LinkedIn isn't connected, so nothing was applied.")

        profile_dir = ctx.browsers.profile_dir(self.platform)
        targets = [
            (job.id, StoredJob(job.url, job.company, job.short_description or "", job.experience or "", job.title))
            for job in jobs
        ]

        def task(driver):
            browser = driver.open(profile_dir=profile_dir, headless=ctx.settings.scan_headless)
            results: list[tuple[int, ApplyAttempt]] = []
            failure = login_lost = None
            try:
                if not LINKEDIN_LOGIN.has_auth_cookie(browser.cookies()):
                    raise LoginWall(LOGIN_EXPIRED)
                clicked = False
                failed_in_a_row = 0
                for job_id, stored in targets:
                    if ctx.stop.is_set():
                        break
                    if clicked and not self._pause(browser, ctx):  # pause after a form, not after a skip
                        break
                    attempt = apply_easy(browser, stored.url, answers, rules=rules, stored=stored, kind=self.kind.value)
                    clicked = attempt.clicked
                    logger.info("LinkedIn Easy Apply %s at %s: %s %s", stored.title, stored.company, attempt.result, attempt.detail)
                    results.append((job_id, attempt))
                    if attempt.result == ApplyResult.quota_reached:
                        break
                    failed = attempt.result in (ApplyResult.no_confirmation, ApplyResult.no_button)
                    failed_in_a_row = failed_in_a_row + 1 if failed else 0
                    if failed_in_a_row >= MAX_FAILED_IN_A_ROW:
                        failure = (
                            f"{MAX_FAILED_IN_A_ROW} forms in a row couldn't be completed; "
                            "check the screenshots in data/diagnostics."
                        )
                        break
            except LoginWall as error:
                failure = str(error)
                login_lost = SECURITY_CHECK if "security check" in failure.lower() else LOGIN_EXPIRED
            except (ScanBlocked, BrowserUnavailable) as error:
                failure = str(error) + capture_failure(browser, self.kind.value)
            except Exception as error:  # noqa: BLE001 - keep what was done so far
                logger.exception("LinkedIn Easy Apply stopped")
                text = str(error).strip()
                failure = f"Applying stopped: {text.splitlines()[0] if text else type(error).__name__}"
                failure += capture_failure(browser, self.kind.value)
            finally:
                browser.close()
            return results, failure, login_lost

        results, failure, login_lost = ctx.browsers.worker(self.platform.value).submit(task).result()
        if login_lost:
            ctx.connections.record_login_lost(self.platform, login_lost)
        counts: dict[ApplyResult, int] = {}
        with Session(ctx.engine) as session:
            for job_id, attempt in results:
                job = session.get(Job, job_id)
                if job is not None:
                    record_attempt(session, job, attempt)
                    remember_hiring_team(job, attempt.facts.get("hiring_team"))
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
            ApplyResult.questionnaire: "need your answers (questions without a template)",
            ApplyResult.company_site: "turned out to be company-site jobs",
            ApplyResult.no_button: "had no Easy Apply button",
            ApplyResult.no_confirmation: "couldn't be completed",
            ApplyResult.quota_reached: "hit LinkedIn's daily Easy Apply limit",
        }
        parts += [f"{counts[r]} {label}" for r, label in labels.items() if counts.get(r)]
        message = ", ".join(parts) + "."
        if failure:
            message += f" Stopped: {failure}"
        status = ScanStatus.failed if failure and not results else ScanStatus.ok
        return ScanOutcome(status=status, found=len(results), new=applied, message=message)
