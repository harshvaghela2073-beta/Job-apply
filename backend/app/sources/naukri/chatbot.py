"""Answering Naukri's recruiter questions: the "chatbot" drawer some jobs open after Apply ("Kindly
answer all the recruiter's questions to successfully apply for the job").

The candidate asked on 2026-10-14 for these to be answered from their details, the same way as
LinkedIn Easy Apply: each question goes to ``AnswerBook`` (profile fields, matching Template Library
answers and the profile-based rules in ``AnswerBook._infer``). Answers worked out from the profile
collect in ``answers.learned``; the caller saves them to the Template Library after the run.

The drawer asks one question at a time (saved pages of 2026-10-13/14):

1. a bot bubble with the question, after a typing indicator;
2. an input under it: a text box, radio buttons (Yes / No), checkboxes or chips;
3. "Save" sends the answer, which shows up as the candidate's bubble; then the next question comes.

After the last answer Naukri applies (the page goes to saveApply, or the button says "Applied").

Nothing is guessed. A question without an answer (the candidate's defaults of 2026-10-21 included)
stops the flow: the application isn't completed (Naukri applies only after the last answer), the job
is skipped ("The form asks <question>, which has no saved answer."; 2026-10-21, instead of waiting
for the candidate) and the question goes to the unanswered questions. A dry run never clicks Apply
(on a job without questions that click applies), so it previews the questions the job asked on its
last try.

Selectors live in ``selectors.py`` (``CHATBOT_*``).
"""

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from bs4 import Tag
from sqlmodel import Session, col, select

from app.browser import BrowserSession
from app.models import UnansweredQuestion
from app.scanner.applying import ApplyAttempt, ApplyResult, no_saved_answer
from app.scanner.base import capture_failure
from app.sources.html import first, soup, text_of
from app.sources.linkedin.answers import Answer, AnswerBook, FormField, clean_label
from app.sources.naukri import selectors as S
from app.sources.naukri.parse import apply_outcome

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 20
QUESTION_WAIT_SECONDS = 15  # for the next question (typing indicator) or the confirmation
SAVE_WAIT_SECONDS = 4  # for "Save" to turn on after an answer is typed or chosen
POLL_SECONDS = 0.5

# The input kind -> the FormField type AnswerBook understands.
FIELD_TYPES = {"text": "text", "date": "date", "radio": "radio", "chips": "radio", "checkbox": "checkbox"}
_ERROR = re.compile(S.CHATBOT_ERROR, re.I)
_SAFE_ID = re.compile(r"^[A-Za-z_][\w-]*$")
_DATE = re.compile(r"^\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{4})\s*$")


@dataclass
class Option:
    text: str
    selector: str  # what to click: the radio / checkbox label, or the chip


@dataclass
class Stalled:
    """Nothing changed before the wait ran out; ``view`` is the last thing the drawer showed."""

    view: "DrawerView"


@dataclass
class DrawerView:
    """What the drawer shows right now."""

    open: bool = False
    typing: bool = False
    question: str = ""  # the question waiting for an answer ("" while none is)
    error: str = ""  # Naukri rejected the last answer with this message
    answers_given: int = 0  # the candidate's bubbles so far
    bubbles: int = 0  # all bubbles (bot and candidate)
    kind: str = "none"  # text | date | radio | checkbox | chips | unsupported | none
    options: list[Option] = field(default_factory=list)
    input_selector: str = ""  # the text box
    date_selectors: tuple[str, ...] = ()  # day, month, year boxes
    unsupported: str = ""  # what kind of input the tool can't fill
    save_enabled: bool = False
    questions: list[str] = field(default_factory=list)  # every question bubble in the drawer

    @property
    def ready(self) -> bool:
        return self.open and not self.typing and bool(self.question) and self.kind != "none"

    @property
    def signature(self) -> tuple:
        """Changes when Naukri reacts to an answer (a new bubble, question or error)."""
        return (self.answers_given, self.bubbles, self.question, self.error)


# ---------------------------------------------------------------------------
# Reading the drawer (pure: HTML in, DrawerView out)


def _classes(node: Tag) -> list[str]:
    return list(node.get("class") or [])


def _hidden(node: Tag, root: Tag | None = None) -> bool:
    current = node
    while isinstance(current, Tag):
        style = (current.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or any(c in S.CHATBOT_HIDDEN_ANCESTOR_CLASSES for c in _classes(current)):
            return True
        if current is root:
            break
        current = current.parent
    return False


def css_path(node: Tag) -> str:
    """A CSS selector for exactly this element: from its nearest ancestor with a plain id (the drawer
    has one), then ``tag:nth-child(n)`` steps. Never holds the option's text, so commas or quotes in
    an answer can't break it."""
    parts: list[str] = []
    current: Tag | None = node
    while isinstance(current, Tag) and current.name != "[document]":
        node_id = current.get("id")
        if isinstance(node_id, str) and _SAFE_ID.match(node_id):
            parts.append(f"[id='{node_id}']")
            break
        parent = current.parent
        if current.name == "html" or parent is None:
            parts.append(current.name)
            break
        siblings = [child for child in parent.children if isinstance(child, Tag)]
        index = next(i for i, child in enumerate(siblings, 1) if child is current)
        parts.append(f"{current.name}:nth-child({index})")
        current = parent
    return " > ".join(reversed(parts))


def _is_question(text: str) -> bool:
    lowered = text.lower()
    return bool(text) and not any(marker in lowered for marker in S.CHATBOT_NOT_A_QUESTION) and not _ERROR.search(text)


def _last_visible(root: Tag, selectors: Iterable[str]) -> Tag | None:
    found = [node for selector in selectors for node in root.select(selector) if not _hidden(node, root)]
    return found[-1] if found else None


def _label_text(node: Tag) -> str:
    return " ".join(text_of(node).split())


def _choice_options(group: Tag, input_type: str, boxes: Iterable[str] = ()) -> list[Option]:
    """Radio buttons or checkboxes: each input's label text, and the label to click (the inputs
    themselves are hidden off-screen)."""
    labels = group.find_all("label")
    options = []
    for control in group.select(f"input[type='{input_type}']"):
        control_id = control.get("id")
        label = next((lb for lb in labels if control_id and lb.get("for") == control_id), None)
        label = label or control.find_next_sibling("label") or control.find_parent("label")
        text = (_label_text(label) if label is not None else "") or str(control.get("value") or "").strip()
        if text:
            options.append(Option(text, css_path(label if label is not None else control)))
    if not options:  # styled options without inputs
        for selector in boxes:
            for box in group.select(selector):
                target = box.select_one("label") or box
                if _label_text(target):
                    options.append(Option(_label_text(target), css_path(target)))
    return options


def _chip_options(root: Tag) -> list[Option]:
    chips = [
        chip for selector in S.CHATBOT_CHIP for chip in root.select(selector)
        if not _hidden(chip, root) and S.CHATBOT_CHIP_DISABLED not in _classes(chip)
    ]
    if not chips:
        return []
    group = chips[-1].parent  # only the latest chip group: older ones belong to answered questions
    return [Option(_label_text(chip), css_path(chip)) for chip in chips if chip.parent is group and _label_text(chip)]


def read_drawer(html: str) -> DrawerView:
    doc = soup(html)
    root = first(doc, S.CHATBOT_ROOT)
    if root is None or _hidden(root) or any(c in S.CHATBOT_HIDDEN_CLASSES for c in _classes(root)):
        return DrawerView()
    view = DrawerView(open=True)
    items = [li for li in root.find_all("li") if {S.CHATBOT_BOT_ITEM, S.CHATBOT_USER_ITEM} & set(_classes(li))]
    user_positions = [i for i, li in enumerate(items) if S.CHATBOT_USER_ITEM in _classes(li)]
    last_user = user_positions[-1] if user_positions else -1
    view.answers_given = len(user_positions)
    view.bubbles = len(items)

    def bot_text(li: Tag) -> str:
        return _label_text(first(li, S.CHATBOT_BOT_TEXT) or li)

    bots = [(i, bot_text(li)) for i, li in enumerate(items) if S.CHATBOT_BOT_ITEM in _classes(li)]
    view.questions = list(dict.fromkeys(text for _, text in bots if _is_question(text)))
    since = [text for i, text in bots if i > last_user]
    asked = [text for text in since if _is_question(text)]
    errors = [text for text in since if _ERROR.search(text)]
    inline_error = root.select_one(S.CHATBOT_INLINE_ERROR)
    if inline_error is not None and _label_text(inline_error) and not _hidden(inline_error, root):
        errors.append(_label_text(inline_error))
    view.error = errors[-1] if errors else ""
    if asked:
        view.question = asked[-1]
    elif errors:  # rejected: the question is the one before the candidate's last bubble
        earlier = [text for i, text in bots if i < last_user and _is_question(text)]
        view.question = earlier[-1] if earlier else ""
    view.typing = first(root, S.CHATBOT_TYPING) is not None

    wrapper = root.select_one(S.CHATBOT_SAVE_WRAPPER)
    view.save_enabled = wrapper is not None and "disabled" not in _classes(wrapper)

    radio = _last_visible(root, S.CHATBOT_RADIO_GROUP)
    checkbox = _last_visible(root, S.CHATBOT_CHECKBOX_GROUP)
    date = _last_visible(root, S.CHATBOT_DATE)
    unsupported = next(
        (name for selector, name in S.CHATBOT_UNSUPPORTED.items() if _last_visible(root, (selector,)) is not None), ""
    )
    text_box = _last_visible(root, S.CHATBOT_TEXT_INPUT)
    chips = _chip_options(root)
    if radio is not None and (options := _choice_options(radio, "radio", S.CHATBOT_RADIO_OPTION)):
        view.kind, view.options = "radio", options
    elif checkbox is not None and (options := _choice_options(checkbox, "checkbox")):
        view.kind, view.options = "checkbox", options
    elif date is not None:
        parts = [date.select_one(selector) for selector in S.CHATBOT_DATE_PARTS]
        if all(part is not None for part in parts):
            view.kind, view.date_selectors = "date", tuple(css_path(part) for part in parts)
        else:
            view.kind, view.unsupported = "unsupported", "a date box"
    elif unsupported:
        view.kind, view.unsupported = "unsupported", unsupported
    elif text_box is not None:
        view.kind, view.input_selector = "text", css_path(text_box)  # chips next to a text box are suggestions
    elif chips:
        view.kind, view.options = "chips", chips
    return view


# ---------------------------------------------------------------------------
# Answering


def question_row(field: FormField, answer: Answer, kind: str = "") -> dict:
    """One row of ``facts["questions"]``."""
    row = {"question": field.label, "type": kind or field.type, "answer": answer.value, "source": answer.reason}
    if field.options:
        row["options"] = list(field.options[:12])
    if field.error:
        row["error"] = field.error
    return row


def preview_answers(answers: AnswerBook, questions: Iterable[str]) -> list[dict]:
    """The answer each question would get (asked as a text question: the input kind is only known
    once the drawer shows it)."""
    rows = []
    for index, question in enumerate(dict.fromkeys(q for q in questions if q.strip())):
        field_ = FormField(id=f"q{index}", label=clean_label(question), type="text", required=True)
        rows.append(question_row(field_, answers.answer(field_)))
    return rows


def recorded_questions(session: Session, job_id: int | None, status_reason: str | None) -> list[str]:
    """The recruiter questions a job asked on its last try: its unanswered-question rows, then the
    "Questions: ..." part of its status reason (a question already waiting for another job isn't
    stored twice)."""
    stored = []
    if job_id is not None:
        stored = list(
            session.exec(
                select(UnansweredQuestion.question)
                .where(UnansweredQuestion.job_id == job_id)
                .order_by(col(UnansweredQuestion.id))
            ).all()
        )
    reason = status_reason or ""
    from_reason = reason.split(" Questions: ", 1)[1].split("; ") if " Questions: " in reason else []
    return list(dict.fromkeys(q.strip() for q in stored + from_reason if q.strip()))


def _wait_for_change(browser: BrowserSession, previous: tuple | None) -> ApplyAttempt | DrawerView | Stalled:
    """Poll until Naukri confirms, or the drawer shows a question to answer that differs from what it
    showed when the last answer was sent (``previous``, a ``DrawerView.signature``): the next
    question, or the same one with an error. ``Stalled`` when the wait runs out (its view is closed
    when the drawer went away without a confirmation)."""
    view = DrawerView()
    waited = 0.0
    while waited <= QUESTION_WAIT_SECONDS:
        browser.close_extra_pages()
        html = browser.html()
        outcome = apply_outcome(html, browser.current_url())
        if outcome is not None:
            return outcome
        view = read_drawer(html)
        if view.ready and view.signature != previous:
            return view
        browser.wait(POLL_SECONDS)
        waited += POLL_SECONDS
    return Stalled(view)


def _date_parts(value: str) -> tuple[str, str, str] | None:
    match = _DATE.match(value)
    return (match.group(1).zfill(2), match.group(2).zfill(2), match.group(3)) if match else None


def _give(browser: BrowserSession, view: DrawerView, value: str) -> str | None:
    """Put the answer in and send it. Returns what went wrong, or None."""
    if view.kind in ("radio", "chips"):
        option = next((o for o in view.options if o.text == value), None)
        if option is None or not browser.click(option.selector):
            return f"the option {value!r} couldn't be chosen"
    elif view.kind == "checkbox":
        wanted = [v.strip() for v in value.split("\n") if v.strip()]
        for option in view.options:
            if option.text in wanted and not browser.click(option.selector):
                return f"the box {option.text!r} couldn't be ticked"
    elif view.kind == "text":
        if not browser.type_text(view.input_selector, value):
            return "the answer couldn't be typed in"
    elif view.kind == "date":
        parts = _date_parts(value)
        if parts is None:
            return f"{value!r} isn't a DD/MM/YYYY date"
        for selector, part in zip(view.date_selectors, parts, strict=True):
            if not browser.type_text(selector, part):
                return "the date couldn't be typed in"
    else:
        return "the tool can't fill this kind of input"
    # Chips may send themselves; otherwise "Save" turns on once there is an answer.
    waited = 0.0
    while waited <= SAVE_WAIT_SECONDS:
        now = read_drawer(browser.html())
        if not now.open or now.typing or now.answers_given > view.answers_given:
            return None  # already sent
        if now.save_enabled:
            return None if browser.click(S.CHATBOT_SAVE) else "the Save button couldn't be clicked"
        browser.wait(POLL_SECONDS)
        waited += POLL_SECONDS
    return "the Save button stayed off"


def answer_questions(browser: BrowserSession, answers: AnswerBook, *, kind: str, facts: dict) -> ApplyAttempt:
    """Answer the open drawer's questions one by one until Naukri confirms the application.

    ``facts["questions"]`` gets one row per question: the question, the input kind, its options, the
    answer and where it came from (or why there is none).
    """
    rows: list[dict] = []
    facts["questions"] = rows
    sent_view: DrawerView | None = None
    sent_question = sent_value = None
    retried: set[str] = set()

    def stop(result: ApplyResult, detail: str, questions: list[str] | None = None) -> ApplyAttempt:
        detail += capture_failure(browser, kind)
        return ApplyAttempt(result, detail, questions or [], facts, clicked=True)

    for _ in range(MAX_QUESTIONS + 2):
        state = _wait_for_change(browser, sent_view.signature if sent_view else None)
        if isinstance(state, ApplyAttempt):
            if state.result == ApplyResult.applied:
                state.detail = (
                    f"Applied with Naukri's one-click Apply after answering {len(rows)} recruiter question(s)."
                    if rows else state.detail
                )
            return ApplyAttempt(state.result, state.detail, [], facts, clicked=True)
        if isinstance(state, Stalled):
            view = state.view
            if not view.open:
                return stop(ApplyResult.no_confirmation, "The recruiter-questions drawer closed but Naukri showed no confirmation.")
            if sent_question is None and view.questions:
                # Questions with an input the tool doesn't recognise: leave the job for you.
                return stop(
                    ApplyResult.questionnaire,
                    "Naukri asked the recruiter's questions in a form the tool couldn't read.",
                    view.questions,
                )
            where = f" after the answer to {sent_question!r}" if sent_question else ""
            return stop(ApplyResult.no_confirmation, f"Naukri's recruiter questions stopped{where}.")
        view = state

        form_field = FormField(
            id=f"q{len(rows)}",
            label=clean_label(view.question),
            type=FIELD_TYPES.get(view.kind, "text"),
            required=True,
            options=tuple(o.text for o in view.options),
            error=view.error,
        )
        if view.kind == "unsupported":
            answer = Answer(None, f"no answer: Naukri shows {view.unsupported}, which the tool can't fill yet")
        else:
            answer = answers.answer(form_field)
        rejected = bool(view.error) and view.question == sent_question
        if rejected:
            rows.pop()  # the new row replaces the rejected one
            if answer.value is None or answer.value == sent_value or view.question in retried:
                rows.append(question_row(form_field, Answer(None, f"no answer: Naukri didn't accept {sent_value!r}"), view.kind))
                return stop(
                    ApplyResult.no_confirmation,
                    f"Naukri didn't accept the answer {sent_value!r} to {view.question!r} ({view.error}); "
                    "the application wasn't completed.",
                )
            retried.add(view.question)
        rows.append(question_row(form_field, answer, view.kind))
        if answer.value is None:
            answered = len(rows) - 1
            start = f"Answered {answered} of Naukri's recruiter questions, but" if answered else "Naukri's recruiter question"
            if view.kind == "unsupported":  # an answer in the Template Library wouldn't help: not "unanswered"
                return stop(
                    ApplyResult.questionnaire,
                    f"{start} {view.question!r} uses {view.unsupported}, which the tool can't fill yet; "
                    "the application wasn't completed.",
                )
            # Skipped, not left waiting (2026-10-21): the application isn't completed.
            return ApplyAttempt(ApplyResult.skipped, no_saved_answer([form_field.label]), [form_field.label], facts, clicked=True)
        problem = _give(browser, view, answer.value)
        if problem:
            return stop(ApplyResult.no_confirmation, f"Couldn't answer {view.question!r}: {problem}; the application wasn't completed.")
        logger.info("Naukri question %r answered from %s", view.question, answer.reason)
        sent_view, sent_question, sent_value = view, view.question, answer.value
    return stop(ApplyResult.no_confirmation, f"Naukri asked more than {MAX_QUESTIONS} questions; stopped.")
