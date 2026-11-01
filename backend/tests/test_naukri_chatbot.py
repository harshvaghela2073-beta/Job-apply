"""Answering Naukri's recruiter questions (the chatbot drawer), the pause switch and the cases that
used to end as "no Apply button" (2026-10-14).

The fixtures are synthetic. They copy the structure of saved Naukri pages (class names, nesting,
one question at a time), never their content.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlmodel import Session, select

from app.models import (
    ApplyType,
    Job,
    JobStatus,
    Platform,
    ProfileField,
    ScanStatus,
    Template,
    TemplateKind,
    UnansweredQuestion,
)
from app.scanner.applying import ApplyResult, record_attempt
from app.scanner.base import LoginWall
from app.services.app_settings import AUTO_APPLY_NAUKRI, PAUSE_NAUKRI, set_flag
from app.sources.html import soup
from app.sources.linkedin.answers import AnswerBook
from app.sources.naukri.apply import NaukriApplier, apply_on_page
from app.sources.naukri.chatbot import read_drawer
from app.sources.naukri.scanner import NaukriJobsScanner
from tests.factories import make_job
from tests.fakes import FakeBrowser, FakeDriver, FakeSession, page
from tests.scan_helpers import make_ctx
from tests.test_naukri import connect_naukri

PROFILE = {
    "name": "Test Candidate",
    "skills": "Java, Spring Boot, Golang, SQL, Apache Kafka",
    "form_years_experience": "1.9 (2 when the form only accepts whole numbers)",
    "location": "Bengaluru",
    "current_ctc": "12 LPA",
    "form_expected_ctc": "18 LPA",
}
TEMPLATES = [("Notice period", "30 days"), ("Which of these tools have you used?", "Java, Kafka")]

RELOCATE = "Are you currently living in or ready to relocate to Bengaluru ?"
JAVA_YEARS = "How many years of experience do you have in Java?"
NOTICE = "What is your notice period?"
TOOLS = "Which of these tools have you used?"
CTC = "What is your current CTC?"
DOB = "Date of Birth (DD/MM/YYYY)"


def book(**extra_templates: str) -> AnswerBook:
    return AnswerBook(profile=dict(PROFILE), templates=TEMPLATES + list(extra_templates.items()))


# ---------------------------------------------------------------------------
# A scripted Naukri job page with the recruiter-questions drawer

GREETING = (
    "Hi Test Candidate, thank you for showing interest. Kindly answer all the recruiter's questions "
    "to successfully apply for the job."
)
LOGO = '<li class="botLogo chatbot_ListItem"><div class="chipMsg"><span><img class="platformlogo"></span></div></li>'
TYPING = (
    '<li class="botItem chatbot_ListItem"><div class="botMsg msg"><div>'
    '<span class="_chatBotdot"></span><span class="_chatBotdot"></span><span class="_chatBotdot"></span>'
    "</div></div></li>"
)


def bot(text: str) -> str:
    return f'<li class="botItem chatbot_ListItem"><div class="botMsg msg"><div><span>{text}</span></div></div></li>'


def user(text: str) -> str:
    return (
        '<li class="userItem chatbot_ListItem"><div class="userMsg"><div class="chatbot_msgWrapper">'
        f"<span>{text}</span></div></div></li>"
    )


def save_apply_url(job_id: str, code: int = 200) -> str:
    response = quote(json.dumps({job_id: code}, separators=(",", ":")))
    return (
        f"https://www.naukri.com/myapply/saveApply?strJobsarr=[{job_id}]&applytype=single"
        f"&multiApplyResp={response}&jobTitle=Backend%20Developer"
    )


SPLASH = '<html><head><title>Save Apply | NaukriRecruiter</title></head><body><div id="root"></div></body></html>'


@dataclass
class Q:
    text: str
    kind: str = "text"  # text | radio | checkbox | chips | date | search
    options: tuple[str, ...] = ()
    reject: str = ""  # Naukri's error for the first answer


class FakeNaukriJob:
    """A logged-in job page. Apply opens the drawer, which asks ``questions`` one at a time like
    Naukri: a typing indicator, then the question and its input; Save sends the answer (chips send
    themselves). After the last answer the page goes to saveApply."""

    def __init__(self, job_id: str, questions: list[Q], *, typing_reads: int = 2):
        self.job_id = job_id
        self.page_url = f"https://www.naukri.com/job-listings-backend-developer-acme-bengaluru-1-to-3-years-{job_id}"
        self.questions = questions
        self.typing_reads = typing_reads
        self.apply_clicks = 0
        self.saves = 0
        self.sent: list[str] = []
        self.applied = False
        self.load()

    def load(self) -> None:
        self.url = self.page_url
        self.state = "page"
        self.items: list[str] = []
        self.index = 0
        self.asking = False
        self.typing_left = 0
        self.pending: str | None = None
        self.checked: list[str] = []
        self.date: dict[str, str] = {}
        self.rejected: set[str] = set()

    # -- rendering ---------------------------------------------------------------------------

    def _widget(self, q: Q) -> str:
        if q.kind == "radio":
            boxes = "".join(
                f'<div class="ssrc__radio-btn-container"><input type="radio" class="ssrc__radio" id="{o}" '
                f'name="radio-button" value="{o}"><label class="ssrc__label" for="{o}">{o}</label></div>'
                for o in q.options
            )
            return f'<div class="singleselect-radiobutton-container">{boxes}</div>'
        if q.kind == "checkbox":
            boxes = "".join(
                f'<input type="checkbox" class="mcc__checkbox" id="cb{i}"><label class="mcc__label" for="cb{i}">{o}</label>'
                for i, o in enumerate(q.options)
            )
            return f'<div class="multicheckboxes-container">{boxes}</div>'
        if q.kind == "chips":
            chips = "".join(f'<div class="chatbot_Chip chipInRow chipItem"><span>{o}</span></div>' for o in q.options)
            return f'<div class="chatBot chatbot_Chips chatbot_ChipsInRow">{chips}</div>'
        if q.kind == "date":
            return (
                '<div class="dob__container"><div class="dob__input-container"><span>'
                '<input class="dob__input day" placeholder="DD"><span class="slash">/</span>'
                '<input class="dob__input month" placeholder="MM"><span class="slash">/</span>'
                '<input class="dob__input year" placeholder="YYYY"></span></div></div>'
            )
        if q.kind == "search":
            return '<div class="singleselect-sugg-container"><div class="ssc__input-container"><div class="ssc__textarea"></div></div></div>'
        return (
            '<div class="chatbot_SendMessageContainer" id="_t1InputBox"><div class="chatbot_InputContainer">'
            '<div class="textAreaWrapper"><div class="textArea" contenteditable="true" '
            f'data-placeholder="Type message here..." id="userInput__t1InputBox">{self.pending or ""}</div>'
            "</div></div></div>"
        )

    def _drawer(self, typing: bool) -> str:
        items = self.items + ([TYPING] if typing else [])
        footer = "" if typing or not self.asking else self._widget(self.questions[self.index])
        send = "send" if self.pending else "send disabled"
        return (
            '<div id="_t1ChatbotContainer" class="_chatBotContainer"><div class="chatbot_Drawer chatbot_right" id="_t1Drawer">'
            '<div class="chatbot_DrawerContentWrapper"><div class="chatbot_Nav" id="_t1Navbar">'
            '<div class="crossIcon chatBot chatBot-ic-cross"></div></div>'
            '<div class="chatbot_MessageContainer" id="_t1Messages">'
            f'<ul class="list" id="chatList__t1Messages">{"".join(items)}</ul>'
            f'<div class="footerWrapper" id="_t1Footer"><div class="footerInputBoxWrapper">{footer}</div></div></div>'
            f'<div class="sendMsgbtn_container" id="sendMsgbtn_container__t1InputBox"><div class="{send}" '
            'id="sendMsg__t1InputBox"><div class="sendMsg" tabindex="0">Save</div></div></div>'
            '<form><input class="chatbot_Uploader" id="_t1Uploader" type="file"></form></div>'
            '<div class="chatbot_Overlay show"></div></div></div>'
        )

    def _render(self, typing: bool = False) -> str:
        if self.state == "done":
            return SPLASH
        button = (
            '<span id="already-applied" class="styles_already-applied">Applied</span>' if self.applied
            else '<button id="apply-button" class="styles_apply-button__uJI3A apply-button">Apply</button>'
        )
        drawer = self._drawer(typing) if self.state == "drawer" else ""
        return (
            "<html><head><title>Backend Developer - Acme</title></head><body>"
            '<div class="nI-gNb-header"><div class="nI-gNb-drawer"><div class="nI-gNb-drawer__icon"></div></div></div>'
            '<section class="styles_job-header-container___0wLZ" id="job_header">'
            '<div class="styles_jd-header-comp-name__MvqAI"><a href="/acme-jobs-careers-1">Acme Technologies</a></div>'
            '<div class="styles_jhc__exp__k_giM"><span>1 - 3 years</span></div>'
            '<div class="styles_jhc__salary__jdfEC"><span>Not Disclosed</span></div>'
            f'<div class="styles_jhc__apply-button-container__5Bqnb"><button id="save-job-button">Save</button>{button}</div>'
            "</section>"
            '<section class="styles_job-desc-container"><div class="styles_key-skill__GIPn_"><a>Java</a><a>Kafka</a></div>'
            '<div class="styles_JDC__dang-inner-html__h0K4t">Build Java services.</div></section>'
            f"{drawer}</body></html>"
        )

    def html(self) -> str:
        typing = False
        if self.state == "drawer" and not self.asking:
            if self.typing_left > 0:
                self.typing_left -= 1
                typing = True
            else:
                self.items.append(bot(self.questions[self.index].text))
                self.asking = True
        return self._render(typing)

    # -- actions -----------------------------------------------------------------------------

    def _find(self, selector: str):
        doc = soup(self._render())
        for part in selector.split(","):
            found = doc.select_one(part.strip())
            if found is not None:
                return found
        return None

    def _finish(self) -> None:
        self.state, self.applied, self.url = "done", True, save_apply_url(self.job_id)

    def _send(self, value: str) -> None:
        self.items.append(user(value))
        self.sent.append(value)
        self.pending, self.checked, self.date = None, [], {}
        q = self.questions[self.index]
        if q.reject and q.text not in self.rejected:
            self.rejected.add(q.text)
            self.items.append(bot(q.reject))
            return
        self.index += 1
        self.asking = False
        self.typing_left = self.typing_reads
        if self.index == len(self.questions):
            self._finish()

    def click(self, selector: str) -> bool:
        element = self._find(selector)
        if element is None:
            return False
        classes = element.get("class") or []
        if element.get("id") == "apply-button":
            self.apply_clicks += 1
            if not self.questions:
                self._finish()
            else:
                self.state, self.items, self.asking, self.typing_left = "drawer", [LOGO, bot(GREETING)], False, self.typing_reads
            return True
        if "sendMsg" in classes:
            self.saves += 1
            if self.pending:  # a disabled Save does nothing
                self._send(self.pending)
            return True
        if "ssrc__label" in classes:
            self.pending = element.get_text(strip=True)
            return True
        if "mcc__label" in classes:
            text = element.get_text(strip=True)
            self.checked = [c for c in self.checked if c != text] if text in self.checked else self.checked + [text]
            self.pending = ", ".join(self.checked) or None
            return True
        if "chatbot_Chip" in classes:
            self._send(element.get_text(strip=True))
            return True
        return False

    def type_text(self, selector: str, text: str) -> bool:
        element = self._find(selector)
        if element is None:
            return False
        classes = element.get("class") or []
        if "textArea" in classes:
            self.pending = text
            return True
        if "dob__input" in classes:
            self.date[classes[-1]] = text
            if len(self.date) == 3:
                self.pending = f"{self.date['day']}/{self.date['month']}/{self.date['year']}"
            return True
        return False


class DrawerSession(FakeSession):
    """FakeSession that serves the scripted jobs of a ``DrawerBrowser``."""

    def __init__(self, browser, profile_dir=None, headless=True):
        super().__init__(browser, profile_dir, headless)
        self.job: FakeNaukriJob | None = None

    def goto(self, url, *, wait_for=None, capture=None, timeout=45.0):
        self.job = next((job for job in getattr(self.browser, "jobs", []) if job.page_url == url), None)
        if self.job is None:
            return super().goto(url, wait_for=wait_for, capture=capture, timeout=timeout)
        self.browser.visited.append(url)
        self.job.load()
        self.url = self.job.url
        return page(self.job.url, self.job.html())

    def html(self) -> str:
        return self.job.html() if self.job else super().html()

    def current_url(self) -> str:
        return self.job.url if self.job else super().current_url()

    def click(self, selector: str) -> bool:
        if self.job is None:
            return super().click(selector)
        self.browser.clicks.append((self.job.page_url, selector))
        return self.job.click(selector)

    def type_text(self, selector: str, text: str) -> bool:
        return self.job.type_text(selector, text) if self.job else False


class DrawerDriver(FakeDriver):
    def open(self, *, profile_dir, headless):
        if profile_dir is not None:
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
        self.browser.opened.append((profile_dir, headless))
        session = DrawerSession(self.browser, profile_dir, headless)
        self.sessions.append(session)
        return session


@dataclass
class DrawerBrowser(FakeBrowser):
    jobs: list[FakeNaukriJob] = field(default_factory=list)

    def factory(self):
        return lambda: DrawerDriver(self)


@pytest.fixture
def fake_browser() -> DrawerBrowser:
    return DrawerBrowser()


def run(job: FakeNaukriJob, answers: AnswerBook, **kwargs):
    browser = DrawerSession(DrawerBrowser(jobs=[job]))
    return apply_on_page(browser, job.page_url, answers=answers, **kwargs)


# ---------------------------------------------------------------------------
# Reading the drawer


def test_read_drawer_text_question_and_typing_indicator():
    job = FakeNaukriJob("1001", [Q(JAVA_YEARS)], typing_reads=1)
    job.click("#apply-button")
    waiting = read_drawer(job.html())
    assert waiting.open and waiting.typing and not waiting.ready and waiting.question == ""  # greeting only
    view = read_drawer(job.html())
    assert view.ready and (view.question, view.kind, view.save_enabled) == (JAVA_YEARS, "text", False)
    assert soup(job.html()).select_one(view.input_selector).get("id") == "userInput__t1InputBox"


def test_read_drawer_radio_options_point_at_their_labels():
    job = FakeNaukriJob("1002", [Q(RELOCATE, "radio", ("Yes", "No"))], typing_reads=0)
    job.click("#apply-button")
    html = job.html()
    view = read_drawer(html)
    assert (view.question, view.kind) == (RELOCATE, "radio")
    assert [o.text for o in view.options] == ["Yes", "No"]
    assert [soup(html).select_one(o.selector).get("for") for o in view.options] == ["Yes", "No"]


def test_read_drawer_after_an_answer_waits_for_the_next_question():
    html = (
        '<div class="chatbot_Drawer chatbot_right" id="_x1Drawer"><ul class="list">'
        f"{LOGO}{bot(GREETING)}{bot(NOTICE)}{user('1 month')}</ul></div>"
    )
    view = read_drawer(html)
    assert view.open and view.answers_given == 1 and view.question == "" and not view.ready
    assert view.questions == [NOTICE]


def test_read_drawer_rejected_answer():
    html = (
        '<div class="chatbot_Drawer" id="_x1Drawer"><ul class="list">'
        f"{bot('Total years of experience')}{user('1.9')}{bot('Please enter a whole number')}</ul>"
        '<div class="footerWrapper"><div class="textArea" contenteditable="true"></div></div></div>'
    )
    view = read_drawer(html)
    assert (view.question, view.error, view.kind) == ("Total years of experience", "Please enter a whole number", "text")


def test_no_drawer():
    assert not read_drawer("<html><body><button id='apply-button'>Apply</button></body></html>").open


# ---------------------------------------------------------------------------
# Answering


def test_answers_every_kind_of_question_then_applies():
    job = FakeNaukriJob(
        "2001",
        [
            Q(RELOCATE, "radio", ("Yes", "No")),
            Q(JAVA_YEARS),
            Q(NOTICE, "chips", ("15 days or less", "1 month", "2 months", "3 months or more")),
            Q(TOOLS, "checkbox", ("Java", "Kafka", "React")),
            Q(CTC),
        ],
    )
    answers = book()
    attempt = run(job, answers)

    assert attempt.result == ApplyResult.applied, attempt.detail
    assert attempt.detail == "Applied with Naukri's one-click Apply after answering 5 recruiter question(s)."
    assert job.sent == ["Yes", "1.9", "1 month", "Java, Kafka", "12 LPA"]
    assert job.apply_clicks == 1
    rows = attempt.facts["questions"]
    assert [(r["question"], r["type"], r["answer"]) for r in rows] == [
        (RELOCATE, "radio", "Yes"),
        (JAVA_YEARS, "text", "1.9"),
        (NOTICE, "chips", "1 month"),
        (TOOLS, "checkbox", "Java\nKafka"),
        (CTC, "text", "12 LPA"),
    ]
    assert rows[0]["options"] == ["Yes", "No"]
    # Worked out from the profile (not from a template): saved to the Template Library afterwards.
    assert answers.learned == [(RELOCATE, "Yes"), (JAVA_YEARS, "1.9")]


def test_a_question_without_an_answer_stops_and_skips_the_job():
    """2026-10-21: skipped instead of left waiting for the candidate."""
    job = FakeNaukriJob("2002", [Q(RELOCATE, "radio", ("Yes", "No")), Q(DOB), Q(CTC)])
    attempt = run(job, book())

    assert attempt.result == ApplyResult.skipped and attempt.clicked
    assert attempt.questions == [DOB]
    assert job.sent == ["Yes"] and not job.applied  # never guessed, never completed
    assert attempt.detail == f'The form asks "{DOB}", which has no saved answer.'
    assert [r["answer"] for r in attempt.facts["questions"]] == ["Yes", None]


def test_unanswered_question_is_recorded_on_the_job(session):
    job_row = make_job(session, platform=Platform.naukri, apply_type=ApplyType.one_click, status=JobStatus.found)
    attempt = run(FakeNaukriJob("2003", [Q(DOB)]), book())
    record_attempt(session, job_row, attempt)
    session.commit()
    assert job_row.status == JobStatus.skipped
    assert job_row.status_reason == f'Not auto-applied: The form asks "{DOB}", which has no saved answer.'
    assert [q.question for q in session.exec(select(UnansweredQuestion)).all()] == [DOB]


def test_a_rejected_decimal_is_sent_again_as_a_whole_number():
    job = FakeNaukriJob("2004", [Q("Total years of experience", reject="Please enter a whole number")])
    attempt = run(job, book())
    assert attempt.result == ApplyResult.applied, attempt.detail
    assert job.sent == ["1.9", "2"]
    assert attempt.facts["questions"][0]["error"] == "Please enter a whole number"


def test_a_rejected_answer_with_a_unit_is_sent_again_as_a_plain_number():
    job = FakeNaukriJob("2005", [Q(CTC, reject="Please enter a valid number")])
    attempt = run(job, book())
    assert attempt.result == ApplyResult.applied and job.sent == ["12 LPA", "12"]


def test_a_rejected_answer_is_not_sent_twice():
    projects = "How many projects have you led?"
    job = FakeNaukriJob("2010", [Q(projects, reject="Please enter a valid number")])
    attempt = run(job, book(**{projects: "3"}))
    assert attempt.result == ApplyResult.no_confirmation and job.sent == ["3"]
    assert "didn't accept" in attempt.detail
    # A decimal is tried once more as the nearest whole number, whatever the error says (2026-10-28).
    job = FakeNaukriJob("2011", [Q("Total years of experience", reject="Please enter a valid number")])
    attempt = run(job, book())
    assert attempt.result == ApplyResult.applied and job.sent == ["1.9", "2"]


def test_date_boxes():
    job = FakeNaukriJob("2006", [Q("Date of birth", "date")])
    attempt = run(job, book(**{"Date of birth": "5/6/2000"}))
    assert attempt.result == ApplyResult.applied and job.sent == ["05/06/2000"]


def test_an_input_the_tool_cannot_fill_is_left_for_you_without_an_unanswered_question():
    job = FakeNaukriJob("2007", [Q("Select your current city", "search")])
    attempt = run(job, book())
    assert attempt.result == ApplyResult.questionnaire and attempt.questions == []
    assert "a searchable list" in attempt.detail and job.sent == []


def test_a_job_without_questions_still_applies_directly():
    job = FakeNaukriJob("2008", [])
    attempt = run(job, book())
    assert attempt.result == ApplyResult.applied and attempt.detail == "Applied with Naukri's one-click Apply."


def test_dry_run_never_clicks_and_previews_the_last_questions():
    job = FakeNaukriJob("2009", [Q(RELOCATE, "radio", ("Yes", "No"))])
    answers = book()
    attempt = run(job, answers, dry_run=True, known_questions=[RELOCATE, "What is your favourite colour?"])
    assert attempt.result == ApplyResult.would_apply and job.apply_clicks == 0 and job.sent == []
    assert [(r["question"], r["answer"]) for r in attempt.facts["questions"]] == [
        (RELOCATE, "Yes"), ("What is your favourite colour?", None),
    ]
    assert attempt.questions == ["What is your favourite colour?"]
    assert "1 still have no answer" in attempt.detail


# ---------------------------------------------------------------------------
# The applier and the endpoint


def add_profile(engine, templates=TEMPLATES) -> None:
    with Session(engine) as session:
        for position, (key, value) in enumerate(PROFILE.items()):
            session.add(ProfileField(key=key, label=key, section="Test", value=value, position=position))
        for question, answer in templates:
            session.add(Template(kind=TemplateKind.answer, question=question, answer=answer))
        session.commit()


def naukri_job(session, fake: FakeNaukriJob, **overrides) -> Job:
    values = dict(
        platform=Platform.naukri,
        external_id=fake.job_id,
        url=fake.page_url,
        company="Acme Technologies",
        short_description="Java, Kafka · Build Java services.",
        experience="1-3 Yrs",
        apply_type=ApplyType.one_click,
        status=JobStatus.found,
        priority=0,
    )
    values.update(overrides)
    return make_job(session, **values)


def test_applier_answers_questions_and_saves_learned_answers_once(client, settings, fake_browser, profile_path):
    connect_naukri(client, settings)
    engine = client.app.state.engine
    add_profile(engine)
    first = FakeNaukriJob("3001", [Q(RELOCATE, "radio", ("Yes", "No")), Q(JAVA_YEARS)])
    second = FakeNaukriJob("3002", [Q(JAVA_YEARS), Q(RELOCATE, "radio", ("Yes", "No")), Q(NOTICE, "text")])
    fake_browser.jobs = [first, second]
    with Session(engine) as session:
        set_flag(session, AUTO_APPLY_NAUKRI, True)
        ids = [naukri_job(session, first).id, naukri_job(session, second, priority=10).id]

    outcome = NaukriApplier().run(make_ctx(client.app))

    assert (outcome.status, outcome.new) == (ScanStatus.ok, 2), outcome.message
    assert outcome.message == (
        "Applied to 2. Saved 2 answer(s) worked out from your profile to the Template Library."
    )
    assert first.sent == ["Yes", "1.9"] and second.sent == ["1.9", "Yes", "30 days"]
    with Session(engine) as session:
        assert all(session.get(Job, i).status == JobStatus.applied for i in ids)
        learned = session.exec(select(Template).where(Template.question.in_([RELOCATE, JAVA_YEARS]))).all()
        assert sorted((t.question, t.answer) for t in learned) == [(RELOCATE, "Yes"), (JAVA_YEARS, "1.9")]
    assert RELOCATE in profile_path.read_text()  # the profile mirror was refreshed


def test_applier_skips_unanswerable_jobs(client, settings, fake_browser):
    connect_naukri(client, settings)
    engine = client.app.state.engine
    add_profile(engine)
    fake = FakeNaukriJob("3003", [Q(RELOCATE, "radio", ("Yes", "No")), Q(DOB)])
    fake_browser.jobs = [fake]
    with Session(engine) as session:
        set_flag(session, AUTO_APPLY_NAUKRI, True)
        job_id = naukri_job(session, fake).id

    outcome = NaukriApplier().run(make_ctx(client.app))

    assert outcome.message.startswith("Applied to 0, 1 skipped for a recruiter question without a saved answer")
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job.status == JobStatus.skipped and DOB in job.status_reason
        assert [q.question for q in session.exec(select(UnansweredQuestion)).all()] == [DOB]
        # The answer worked out on the way is kept for next time.
        assert session.exec(select(Template).where(Template.question == RELOCATE)).one().answer == "Yes"


def test_endpoint_dry_run_previews_recorded_questions_then_applies(client, settings, fake_browser):
    connect_naukri(client, settings)
    engine = client.app.state.engine
    add_profile(engine)
    fake = FakeNaukriJob("3004", [Q(RELOCATE, "radio", ("Yes", "No")), Q(CTC)])
    fake_browser.jobs = [fake]
    with Session(engine) as session:
        job = naukri_job(
            session, fake, status=JobStatus.needs_manual,
            status_reason=f"Naukri asked the recruiter's questions. Questions: {RELOCATE}; {CTC}",
        )
        job_id = job.id
        session.add(UnansweredQuestion(question=RELOCATE, platform=Platform.naukri, job_id=job_id))
        session.commit()

    dry = client.post(f"/api/jobs/{job_id}/apply", params={"dry_run": True}).json()
    assert dry["result"] == "would_apply" and fake.apply_clicks == 0 and dry["job"]["status"] == "needs_manual"
    assert [(q["question"], q["answer"]) for q in dry["facts"]["questions"]] == [(RELOCATE, "Yes"), (CTC, "12 LPA")]
    assert dry["questions"] == []

    done = client.post(f"/api/jobs/{job_id}/apply").json()
    assert done["result"] == "applied" and done["job"]["status"] == "applied"
    assert fake.sent == ["Yes", "12 LPA"]
    with Session(engine) as session:
        saved = session.exec(select(Template).where(Template.question == RELOCATE)).all()
        assert [t.answer for t in saved] == ["Yes"]  # learned in the dry run, saved once


# ---------------------------------------------------------------------------
# Pausing Naukri


def test_pause_switch_in_settings(client):
    assert client.get("/api/settings").json()["pause_naukri"] is False
    assert client.put("/api/settings", json={"pause_naukri": True}).json()["pause_naukri"] is True
    assert client.get("/api/settings").json()["pause_naukri"] is True
    assert client.put("/api/settings", json={"pause_naukri": False}).json()["pause_naukri"] is False


def test_paused_naukri_opens_no_browser(client, settings, fake_browser):
    connect_naukri(client, settings)
    engine = client.app.state.engine
    fake = FakeNaukriJob("4001", [])
    fake_browser.jobs = [fake]
    with Session(engine) as session:
        set_flag(session, AUTO_APPLY_NAUKRI, True)
        set_flag(session, PAUSE_NAUKRI, True)
        naukri_job(session, fake)
    ctx = make_ctx(client.app)
    for scanner in (NaukriJobsScanner(), NaukriApplier()):
        outcome = scanner.run(ctx)
        assert (outcome.status, outcome.message) == (ScanStatus.skipped, "Naukri is paused.")
    assert fake_browser.opened == [] and fake_browser.visited == [] and fake.apply_clicks == 0

    with Session(engine) as session:
        set_flag(session, PAUSE_NAUKRI, False)
    assert NaukriApplier().run(ctx).new == 1


# ---------------------------------------------------------------------------
# What used to end as "no Apply button"

JOB_URL = "https://www.naukri.com/job-listings-backend-developer-acme-bengaluru-1-to-3-years-020926034338"
APPLY_PAGE = '<html><body><button id="apply-button" class="apply-button">Apply</button></body></html>'


class NavigatesMidClick(FakeSession):
    """The live case of 2026-10-14: the click applied and the page went to saveApply so fast that
    Playwright reported the click as failed ("Execution context was destroyed")."""

    code = 200

    def click(self, selector: str) -> bool:
        self.browser.clicks.append((self.url, selector))
        self.url, self._html = save_apply_url("020926034338", self.code), SPLASH
        self.last_click_problem = "Locator.evaluate: Execution context was destroyed, most likely because of a navigation."
        return False


def test_a_click_that_navigated_to_save_apply_counts_as_applied(fake_browser):
    fake_browser.routes = [("job-listings", page(JOB_URL, APPLY_PAGE))]
    attempt = apply_on_page(NavigatesMidClick(fake_browser, None, True), JOB_URL)
    assert attempt.result == ApplyResult.applied and not fake_browser.screenshots


def test_save_apply_with_an_error_code_is_not_applied(fake_browser):
    fake_browser.routes = [("job-listings", page(JOB_URL, APPLY_PAGE))]
    browser = NavigatesMidClick(fake_browser, None, True)
    browser.code = 409
    attempt = apply_on_page(browser, JOB_URL)
    assert attempt.result == ApplyResult.no_confirmation and "409" in attempt.detail


def test_expired_job_is_skipped_with_the_reason(fake_browser):
    closed = "<html><body><p>This job is no longer accepting applications.</p></body></html>"
    fake_browser.routes = [("job-listings", page(JOB_URL, closed))]
    attempt = apply_on_page(FakeSession(fake_browser, None, True), JOB_URL)
    assert attempt.result == ApplyResult.skipped and "no longer accepting applications" in attempt.detail


def test_logged_out_job_page_is_a_login_wall(fake_browser):
    logged_out = (
        '<html><body><div class="nI-gNb-header"><a id="login_Layer" title="Jobseeker Login">Login</a>'
        f'<a id="register_Layer">Register</a></div>{APPLY_PAGE}</body></html>'
    )
    fake_browser.routes = [("job-listings", page(JOB_URL, logged_out))]
    with pytest.raises(LoginWall):
        apply_on_page(FakeSession(fake_browser, None, True), JOB_URL)


class LateButton(FakeSession):
    """The header renders a moment after the page reports loaded."""

    reads = 0

    def html(self) -> str:
        self.reads += 1
        return self._html if self.reads < 3 else APPLY_PAGE


def test_a_late_apply_button_is_waited_for(fake_browser):
    fake_browser.routes = [("job-listings", page(JOB_URL, "<html><body>Loading</body></html>"))]
    attempt = apply_on_page(LateButton(fake_browser, None, True), JOB_URL, dry_run=True)
    assert attempt.result == ApplyResult.would_apply
