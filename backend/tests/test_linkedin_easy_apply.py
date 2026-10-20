"""LinkedIn Easy Apply: the skip rules, the form engine with a scripted fake form, the scheduled
applier and the apply endpoint. No real browser or network; nothing is ever really submitted."""

import re

import pytest
from sqlmodel import Session, select

from app.models import ApplyType, Job, JobStatus, Platform, ScanStatus, UnansweredQuestion
from app.scanner.applying import ApplyAttempt, ApplyResult, StoredJob, record_attempt
from app.scanner.base import LoginWall
from app.services.app_settings import AUTO_APPLY_LINKEDIN, set_flag
from app.sources.linkedin.answers import AnswerBook
from app.sources.linkedin.easy_apply import LinkedInApplier, apply_easy, read_job_page
from app.sources.linkedin.screening import (
    ApplyRules,
    LinkedInJobFacts,
    linkedin_salary_top_lpa,
    parse_job_facts,
    skip_reason,
)
from tests.factories import make_field, make_job, make_template
from tests.fakes import FakeEasyApply, FakeField, FakeSession, FakeStep, page
from tests.scan_helpers import connect_linkedin, make_ctx

MY_SKILLS = ("Java", "Spring Boot", "Golang", "Python", "SQL", "Redis", "Apache Kafka", "Kubernetes", "GKE")
RULES = ApplyRules(min_salary_lpa=12.0, skills=MY_SKILLS)
PROFILE = {
    "name": "Harsh Vaghela",
    "email": "candidate@example.com",
    "phone": "9000000001",
    "location": "Bengaluru",
    "current_ctc": "12 LPA",
    "form_expected_ctc": "18 LPA",
    "form_years_experience": "1.9 (2 when the form only accepts whole numbers)",
}
BOOK = AnswerBook(
    profile=PROFILE,
    templates=[("Notice period", "30 days"), ("Are you comfortable commuting to this job's location?", "Yes")],
)
JOB_URL = "https://www.linkedin.com/jobs/view/4012345678/"


def job_page(*, button="easy", applied=False, closed=False, company="Acme Software", salary=None,
             level="Entry level", description="Build Java and Spring Boot services. 1-3 years of experience.",
             industry=None, title="Backend Developer"):
    buttons = {
        "easy": '<button class="jobs-apply-button artdeco-button" aria-label="Easy Apply to Backend Developer at Acme">'
                "<span>Easy Apply</span></button>",
        "external": '<button class="jobs-apply-button artdeco-button" aria-label="Apply to Backend Developer on company website">'
                    "<span>Apply</span></button>",
        None: "",
    }
    pills = "".join(
        f'<li class="job-details-jobs-unified-top-card__job-insight"><span>{text}</span></li>' for text in (salary, level) if text
    )
    applied_html = '<div class="artdeco-inline-feedback artdeco-inline-feedback--success"><span>Applied 3 days ago</span></div>' if applied else ""
    closed_html = '<div class="jobs-details-top-card__apply-error"><span>No longer accepting applications</span></div>' if closed else ""
    company_box = (
        f'<section class="jobs-company"><div class="jobs-company__box"><div>{industry} · 51-200 employees</div></div></section>'
        if industry else ""
    )
    return f"""<div class="job-details-jobs-unified-top-card__container--two-pane">
  <div class="job-details-jobs-unified-top-card__job-title"><h1>{title}</h1></div>
  <div class="job-details-jobs-unified-top-card__company-name"><a href="/company/acme/">{company}</a></div>
  <ul>{pills}</ul>
  <div class="jobs-s-apply">{applied_html}{closed_html}{buttons[button]}</div>
</div>
<div id="job-details"><p>{description}</p></div>{company_box}"""


def whole_number(value: str) -> str:
    return "" if value.isdigit() else "Enter a whole number between 0 and 99"


def contact_step():
    return FakeStep([
        FakeField("Email address", "select", options=["Select an option", "candidate@example.com"],
                  value="candidate@example.com"),
        FakeField("Phone country code", "select", options=["India (+91)", "United States (+1)"], value="India (+91)"),
        FakeField("Mobile phone number", "text"),
    ], "next", "Contact info")


def questions_step(*extra, experience_validate=None):
    return FakeStep([
        FakeField("How many years of work experience do you have?", "numeric", validate=experience_validate),
        FakeField("Notice period (in days)", "numeric"),
        FakeField("Are you comfortable commuting to this job's location?", "radio", options=["Yes", "No"]),
        FakeField("Current CTC (in LPA)", "numeric"),
        *extra,
    ], "review", "Additional Questions")


def review_step():
    return FakeStep([
        FakeField("Follow Acme Software to stay up to date with their page.", "checkbox", required=False,
                  dom_id="follow-company-checkbox", checked=True),
    ], "submit", "Review your application")


def scripted(fake_browser, *steps, url=JOB_URL, **page_args) -> FakeEasyApply:
    form = FakeEasyApply(steps=list(steps) or [contact_step(), questions_step(), review_step()], job_html=job_page(**page_args))
    fake_browser.forms.append((url.removeprefix("https://www.linkedin.com"), form))
    return form


def browser_for(fake_browser) -> FakeSession:
    return FakeSession(fake_browser, None, True)


# ---------------------------------------------------------------------------
# Skip rules


@pytest.mark.parametrize(
    ("label", "top"),
    [
        ("₹12,00,000/yr - ₹18,00,000/yr", 18.0),
        ("₹15L/yr", 15.0),
        ("₹8L/yr - ₹10L/yr", 10.0),
        ("₹50K/mo", 6.0),
        ("12-18 LPA", 18.0),
        ("", None),
    ],
)
def test_linkedin_salary(label, top):
    assert linkedin_salary_top_lpa(label) == top


def facts(**values) -> LinkedInJobFacts:
    base = dict(title="Backend Developer", company="Acme Software", level="Entry level",
                description="Build Java services with Spring Boot.")
    base.update(values)
    return LinkedInJobFacts(**base)


@pytest.mark.parametrize(
    ("job", "reason_start"),
    [
        (facts(), None),
        (facts(salary="₹4,00,000/yr - ₹6,00,000/yr"), "Salary ₹4,00,000/yr - ₹6,00,000/yr is below your current 12 LPA"),
        (facts(salary="₹15L/yr"), None),
        (facts(description="Angular and .NET Core front ends; JavaScript a must."), "None of your skills"),
        # At least 2 of your skills (2026-10-15); the title counts too.
        (facts(title="Golang Developer", description="Build APIs on Kafka."), None),
        (facts(title="Golang Developer", description="Build APIs."), "Only 1 of your skills (Golang) is in the job's title or description; at least 2 are needed."),
        (facts(description="Build Java services."), "Only 1 of your skills (Java)"),
        (facts(description="We go live in May: Java services."), "Only 1 of your skills (Java)"),  # "go" isn't Golang
        (facts(description="Java and Go microservices."), None),
        (facts(description=""), None),  # no description read: not skipped on skills
        (facts(level="Internship"), "Internship posting."),
        (facts(description="Freshers welcome, 0-1 years, Java and Spring Boot training provided."), "Fresher / internship-style"),
        (facts(company="Seacross Recruitment Services"), "Posted by a consultancy (Seacross Recruitment Services)"),
        (facts(company="Confidential"), "The company is hidden"),
        (facts(company="Talentica", industry="Staffing and Recruiting"), "Posted by a consultancy (Talentica is in Staffing and Recruiting)"),
        # Job fit (2026-10-15): another stack, senior titles.
        (facts(title="Full Stack Developer - .Net / React.js"), 'Another stack: the title says ".Net" and names none of your skills.'),
        (facts(title="Android SDE 2"), 'Another stack: the title says "Android"'),
        (facts(title="Software Support Engineer"), 'Another stack: the title says "Support"'),
        (facts(title="Java Full Stack Developer (Spring Boot + Angular)"), None),
        (facts(title="Senior Java Developer"), 'Too senior: the title says "Senior".'),
        (facts(title="Technical Lead / Senior Full Stack"), 'Too senior: the title says "Lead".'),
        (facts(title="SDE III"), 'Too senior: the title says "SDE III".'),
        (facts(title="SDE 2"), None),
        (facts(title="Software Engineer II"), None),
    ],
)
def test_skip_reason(job, reason_start):
    reason = skip_reason(RULES, job)
    if reason_start is None:
        assert reason is None
    else:
        assert reason is not None and reason.startswith(reason_start)


RULES_WITH_SKIP_LIST = ApplyRules(min_salary_lpa=12.0, skills=MY_SKILLS, skip_companies=("Jobgether", "Scoutit"))


def test_skip_list_and_the_stored_job():
    assert skip_reason(RULES_WITH_SKIP_LIST, facts(company="Jobgether")) == "Jobgether is on your skip list."
    stored = StoredJob(url=JOB_URL, company="Scoutit Technologies", title="Senior Java Developer")
    assert skip_reason(RULES_WITH_SKIP_LIST, facts(company=""), stored) == "Scoutit Technologies is on your skip list."
    # The page's title is missing: the stored one is used.
    assert skip_reason(RULES, facts(title=""), stored) == 'Too senior: the title says "Senior".'
    assert skip_reason(RULES, facts(company="Jobgether")) is None  # no skip list
    assert skip_reason(RULES_WITH_SKIP_LIST, facts(company="Acme Software")) is None


def test_parse_job_facts_and_page_state():
    html = job_page(salary="₹12L/yr - ₹18L/yr", industry="Staffing and Recruiting")
    found = parse_job_facts(html)
    assert (found.title, found.company, found.salary, found.level, found.industry) == (
        "Backend Developer", "Acme Software", "₹12L/yr - ₹18L/yr", "Entry level", "Staffing and Recruiting"
    )
    assert found.description.startswith("Build Java and Spring Boot services")
    assert read_job_page(html).easy_apply and not read_job_page(html).applied
    assert read_job_page(job_page(applied=True, button=None)).applied
    assert read_job_page(job_page(button="external")).external_apply
    assert read_job_page(job_page(button=None, closed=True)).closed


# ---------------------------------------------------------------------------
# The form engine


def test_multi_step_application_is_submitted(fake_browser):
    form = scripted(fake_browser)
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK, rules=RULES)

    assert attempt.result == ApplyResult.applied and attempt.clicked
    assert attempt.detail == "Applied with LinkedIn Easy Apply (3 step(s))."
    assert form.submitted and not form.discarded
    assert form.clicks == [
        "Easy Apply to Backend Developer at Acme",
        "Continue to next step",
        "Review your application",
        "Submit application",
        "Done",
    ]
    assert dict(form.fills) == {
        "Mobile phone number": "9000000001",
        "How many years of work experience do you have?": "1.9",
        "Notice period (in days)": "30",
        "Are you comfortable commuting to this job's location?": "Yes",
        "Current CTC (in LPA)": "12",
        "Follow Acme Software to stay up to date with their page.": "false",
    }
    # "Follow company" was unticked before Submit.
    assert form.checked_at_submit == {"Follow Acme Software to stay up to date with their page.": False}
    assert attempt.facts["follow_company"] == "unticked"
    rows = {row["label"]: row for row in attempt.facts["fields"]}
    assert rows["Email address"]["value"] == "candidate@example.com" and rows["Email address"]["step"] == 1
    assert rows["Current CTC (in LPA)"]["reason"] == "profile current_ctc (12 LPA) in lakhs"
    assert rows["Notice period (in days)"]["step"] == 2


def test_inline_error_gets_the_whole_number(fake_browser):
    form = scripted(fake_browser, contact_step(), questions_step(experience_validate=whole_number), review_step())
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK)
    assert attempt.result == ApplyResult.applied and form.submitted
    experience = [value for label, value in form.fills if label.startswith("How many years")]
    assert experience == ["1.9", "2"]
    assert form.clicks.count("Review your application") == 2
    row = next(r for r in attempt.facts["fields"] if r["label"].startswith("How many years"))
    assert row["value"] == "2" and "whole number" in row["reason"]


def plain_number(value: str) -> str:
    return "" if re.fullmatch(r"\d+(?:\.\d+)?", value) else "Invalid input"


def test_repeated_questions_with_units_rejected_get_plain_numbers(fake_browser):
    """TCS, 2026-10-14: text boxes that take numbers only ("12 LPA" -> "Invalid input"), and the same
    questions asked twice on one step."""
    step = FakeStep([
        FakeField("Current ctc", "text", dom_id="_r_27_", validate=plain_number),
        FakeField("Notice period", "text", dom_id="_r_29_", validate=plain_number),
        FakeField("Current ctc", "text", dom_id="_r_2c_", validate=plain_number),
        FakeField("Notice period", "text", dom_id="_r_2e_", validate=plain_number),
    ], "review", "Additional Questions")
    form = scripted(fake_browser, contact_step(), step, review_step())
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK)
    assert attempt.result == ApplyResult.applied and form.submitted
    assert [v for label, v in form.fills if label == "Current ctc"] == ["12 LPA", "12 LPA", "12", "12"]
    assert [v for label, v in form.fills if label == "Notice period"] == ["30 days", "30 days", "30", "30"]


def test_value_rejected_again_is_unanswerable(fake_browser):
    always_wrong = lambda value: "Enter a decimal number larger than 5.0"  # noqa: E731
    form = scripted(fake_browser, contact_step(), questions_step(experience_validate=always_wrong), review_step())
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK)
    assert attempt.result == ApplyResult.questionnaire
    assert attempt.questions == ["How many years of work experience do you have?"]
    assert not form.submitted and form.discarded and "Submit application" not in form.clicks


def test_unanswerable_required_field_discards_and_never_submits(fake_browser):
    relocate = FakeField("Are you willing to work night shifts?", "radio", options=["Yes", "No"])
    sponsor = FakeField("Which shift do you prefer?", "select", options=["Select an option", "Morning", "Night"])
    optional = FakeField("LinkedIn profile URL", "text", required=False)
    form = scripted(fake_browser, contact_step(), questions_step(relocate, sponsor, optional), review_step())
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK)

    assert attempt.result == ApplyResult.questionnaire
    assert attempt.questions == ["Are you willing to work night shifts?", "Which shift do you prefer?"]
    assert "nothing was sent" in attempt.detail
    assert not form.submitted and form.discarded
    assert form.clicks[-2:] == ["Dismiss", "Discard"] and "Submit application" not in form.clicks
    assert relocate.value == "" and sponsor.value == ""  # never guessed
    rows = {row["label"]: row for row in attempt.facts["fields"]}
    assert rows["Are you willing to work night shifts?"]["value"] is None
    assert rows["LinkedIn profile URL"]["reason"] == "no answer: no matching template"


def test_new_layout_buttons_by_text_inside_the_dialog(fake_browser):
    """2026-10-14 layout: the footer buttons have only their text ("Next", "Review"), read and
    clicked by handle from one form read per step."""
    form = scripted(fake_browser)
    form.sdui = True
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK, rules=RULES)
    assert attempt.result == ApplyResult.applied and form.submitted
    assert form.clicks == ["Easy Apply to Backend Developer at Acme", "Next", "Review", "Submit application", "Done"]
    assert attempt.facts["follow_company"] == "unticked"


def test_new_layout_discard_through_the_confirmation_dialog(fake_browser):
    relocate = FakeField("Are you willing to work night shifts?", "radio", options=["Yes", "No"])
    form = scripted(fake_browser, contact_step(), questions_step(relocate), review_step())
    form.sdui = True
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK)
    assert attempt.result == ApplyResult.questionnaire and not form.submitted and form.discarded
    assert form.clicks[-2:] == ["Dismiss", "Discard"]


def test_a_page_that_answers_late_is_read_again(fake_browser):
    form = scripted(fake_browser)
    form.sdui = True
    form.silent_reads = 2  # the first two reads time out (a busy page)
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK)
    assert attempt.result == ApplyResult.applied and form.submitted


def test_a_page_that_never_answers_fails_without_hanging(fake_browser):
    form = scripted(fake_browser)
    form.silent_reads = 10_000
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK)
    assert attempt.result == ApplyResult.no_confirmation and not form.submitted
    assert attempt.detail.startswith("The Easy Apply form didn't open.")
    assert "LinkedIn's page stopped responding" in attempt.detail
    assert form.reads < 40


def test_already_applied_external_closed_and_missing_button(fake_browser):
    form = scripted(fake_browser, applied=True, button=None)
    assert apply_easy(browser_for(fake_browser), JOB_URL, BOOK).result == ApplyResult.already_applied
    assert form.clicks == []

    fake_browser.forms.clear()
    form = scripted(fake_browser, button="external")
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK)
    assert attempt.result == ApplyResult.company_site and not attempt.clicked and form.clicks == []

    fake_browser.forms.clear()
    scripted(fake_browser, button=None, closed=True)
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK)
    assert attempt.result == ApplyResult.skipped and "no longer accepting" in attempt.detail

    fake_browser.forms.clear()
    scripted(fake_browser, button=None)
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK)
    assert attempt.result == ApplyResult.no_button and "Screenshot: data/diagnostics/linkedin_apply-" in attempt.detail


def test_skip_rules_stop_before_easy_apply(fake_browser):
    form = scripted(fake_browser, company="Seacross Recruitment Services")
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK, rules=RULES)
    assert attempt.result == ApplyResult.skipped and not attempt.clicked and form.clicks == []
    assert attempt.detail == "Posted by a consultancy (Seacross Recruitment Services)."


def test_dry_run_fills_every_step_but_never_submits(fake_browser):
    form = scripted(fake_browser)
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK, dry_run=True, rules=RULES)
    assert attempt.result == ApplyResult.would_apply
    assert attempt.detail == "Filled all 3 step(s); stopped before Submit (dry run) and discarded the draft."
    assert not form.submitted and form.discarded and "Submit application" not in form.clicks
    labels = [row["label"] for row in attempt.facts["fields"]]
    assert labels == [
        "Email address", "Phone country code", "Mobile phone number",
        "How many years of work experience do you have?", "Notice period (in days)",
        "Are you comfortable commuting to this job's location?", "Current CTC (in LPA)",
    ]
    assert all(set(row) >= {"label", "type", "required", "value", "reason"} for row in attempt.facts["fields"])


def test_dry_run_walks_the_form_even_when_a_skip_rule_matches(fake_browser):
    form = scripted(fake_browser, company="Seacross Recruitment Services")
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK, dry_run=True, rules=RULES)
    assert attempt.result == ApplyResult.would_apply and not form.submitted
    assert attempt.facts["skip_reason"].startswith("Posted by a consultancy")
    assert "A real run would skip this job" in attempt.detail


def test_daily_limit_and_unfillable_field(fake_browser):
    form = scripted(fake_browser)
    form.modal_message = "You've reached today's Easy Apply limit. We limit daily submissions to help."
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK)
    assert attempt.result == ApplyResult.quota_reached and not form.submitted

    fake_browser.forms.clear()
    broken = FakeStep([FakeField("Mobile phone number", "text", fill_ok=False)], "next", "Contact info")
    form = scripted(fake_browser, broken, review_step())
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK)
    assert attempt.result == ApplyResult.no_confirmation and "Couldn't fill in: Mobile phone number." in attempt.detail
    assert not form.submitted and form.discarded


def test_no_confirmation_after_submit(fake_browser):
    form = scripted(fake_browser)
    form.success_on_submit = False
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, BOOK)
    assert attempt.result == ApplyResult.no_confirmation
    assert attempt.detail.startswith("Clicked Submit but LinkedIn showed no confirmation.")


def test_login_wall_on_the_job_page(fake_browser):
    fake_browser.routes = [("linkedin.com", page("https://www.linkedin.com/uas/login?session_redirect=x"))]
    with pytest.raises(LoginWall):
        apply_easy(browser_for(fake_browser), JOB_URL, BOOK)


def test_record_attempt_adds_linkedin_questions(session):
    job = make_job(session, platform=Platform.linkedin, apply_type=ApplyType.easy_apply, status=JobStatus.found)
    record_attempt(session, job, ApplyAttempt(ApplyResult.questionnaire, "Needs answers.", ["Relocate to Pune?"]))
    session.commit()
    assert job.status == JobStatus.needs_manual and job.status_reason == "Needs answers. Questions: Relocate to Pune?"
    (question,) = session.exec(select(UnansweredQuestion)).all()
    assert (question.question, question.platform, question.job_id) == ("Relocate to Pune?", Platform.linkedin, job.id)


# ---------------------------------------------------------------------------
# The scheduled applier


def easy_job(session, n: int, priority: int, **extra) -> Job:
    values = dict(
        platform=Platform.linkedin, external_id=f"40100000{n:02d}", url=f"https://www.linkedin.com/jobs/view/40100000{n:02d}/",
        apply_type=ApplyType.easy_apply, status=JobStatus.found, priority=priority, company="Acme Software",
        title="Backend Developer",
    )
    values.update(extra)
    return make_job(session, **values)


def seed_profile(session):
    for position, (key, val) in enumerate({**PROFILE, "skills": ", ".join(MY_SKILLS)}.items(), start=1):
        make_field(session, key=key, label=key, section="Profile", value=val, position=position)
    make_template(session, question="Notice period", answer="30 days")
    make_template(session, question="Are you comfortable commuting to this job's location?", answer="Yes")


def test_applier_is_off_by_default(client):
    outcome = LinkedInApplier().run(make_ctx(client.app))
    assert (outcome.status, outcome.message) == (ScanStatus.skipped, "LinkedIn auto-apply is off.")


def test_applier_needs_the_login(client):
    with Session(client.app.state.engine) as session:
        set_flag(session, AUTO_APPLY_LINKEDIN, True)
        easy_job(session, 1, priority=0)
    outcome = LinkedInApplier().run(make_ctx(client.app))
    assert (outcome.status, outcome.message) == (ScanStatus.skipped, "LinkedIn isn't connected, so nothing was applied.")


def test_applier_order_caps_and_pauses(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    engine = client.app.state.engine
    with Session(engine) as session:
        set_flag(session, AUTO_APPLY_LINKEDIN, True)
        seed_profile(session)
        jobs = {
            "consultancy": easy_job(session, 1, priority=0, company="Seacross Recruitment Services"),
            "first": easy_job(session, 2, priority=10),
            "questions": easy_job(session, 3, priority=20),
            "second": easy_job(session, 4, priority=30),
            "third": easy_job(session, 5, priority=40),
            "sixth": easy_job(session, 6, priority=50),  # over the 5-per-run limit
            "company_site": easy_job(session, 7, priority=0, apply_type=ApplyType.company_site),
        }
        ids = {name: job.id for name, job in jobs.items()}
        urls = {name: job.url for name, job in jobs.items()}
    relocate = FakeField("Are you willing to work night shifts?", "radio", options=["Yes", "No"])
    forms = {
        "consultancy": scripted(fake_browser, url=urls["consultancy"], company="Seacross Recruitment Services"),
        "first": scripted(fake_browser, url=urls["first"]),
        "questions": scripted(fake_browser, contact_step(), questions_step(relocate), review_step(), url=urls["questions"]),
        "second": scripted(fake_browser, url=urls["second"]),
        "third": scripted(fake_browser, url=urls["third"]),
        "sixth": scripted(fake_browser, url=urls["sixth"]),
    }
    applier = LinkedInApplier(rng=lambda low, high: low)

    outcome = applier.run(make_ctx(client.app))

    opened = [url for url in fake_browser.visited if "/jobs/view/" in url]
    assert opened == [urls[n] for n in ("consultancy", "first", "questions", "second", "third")]
    assert (outcome.status, outcome.found, outcome.new) == (ScanStatus.ok, 5, 3)
    assert outcome.message == "Applied to 3, 1 skipped by your rules, 1 need your answers (questions without a template)."
    # A pause after each application that opened the form, none after the skipped first job.
    assert applier.pauses == [45.0, 45.0, 45.0]
    assert not forms["consultancy"].clicks and not forms["sixth"].clicks
    assert not forms["questions"].submitted and forms["questions"].discarded
    with Session(engine) as session:
        status = {name: session.get(Job, ids[name]).status for name in ids}
        assert status == {
            "consultancy": JobStatus.skipped, "first": JobStatus.applied, "questions": JobStatus.needs_manual,
            "second": JobStatus.applied, "third": JobStatus.applied, "sixth": JobStatus.found,
            "company_site": JobStatus.found,
        }
        (question,) = session.exec(select(UnansweredQuestion)).all()
        assert (question.question, question.platform) == ("Are you willing to work night shifts?", Platform.linkedin)


def test_applier_skips_jobs_that_dont_fit(client, fake_browser):
    """Jobs stored before the job-fit rules (2026-10-15) are skipped at apply time, never opened."""
    connect_linkedin(client.app, fake_browser)
    engine = client.app.state.engine
    with Session(engine) as session:
        set_flag(session, AUTO_APPLY_LINKEDIN, True)
        seed_profile(session)
        make_field(session, key="search_skip_companies", label="Skip companies", section="Job search priority",
                   value="Jobgether, Crossing Hurdles, Scoutit, Applicantz, CodeRound AI", position=50)
        jobs = {
            "aggregator": easy_job(session, 1, priority=0, company="Jobgether"),
            "dotnet": easy_job(session, 2, priority=10, title="Full Stack Developer - .Net / React.js"),
            "senior": easy_job(session, 3, priority=20, title="Senior Backend Engineer"),
            "fits": easy_job(session, 4, priority=30, title="SDE 2"),
        }
        ids = {name: job.id for name, job in jobs.items()}
    forms = {
        name: scripted(fake_browser, url=jobs[name].url, company=jobs[name].company, title=jobs[name].title)
        for name in jobs
    }

    outcome = LinkedInApplier(rng=lambda low, high: low).run(make_ctx(client.app))

    assert outcome.message == "Applied to 1, 3 skipped by your rules."
    assert all(not forms[name].clicks for name in ("aggregator", "dotnet", "senior")) and forms["fits"].submitted
    with Session(engine) as session:
        reasons = {name: session.get(Job, ids[name]).status_reason for name in ids}
    assert reasons["aggregator"] == "Not auto-applied: Jobgether is on your skip list."
    assert reasons["dotnet"] == 'Not auto-applied: Another stack: the title says ".Net" and names none of your skills.'
    assert reasons["senior"] == 'Not auto-applied: Too senior: the title says "Senior".'


def test_applier_respects_the_daily_cap(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    ctx = make_ctx(client.app)
    ctx.settings = ctx.settings.model_copy(update={"easy_apply_daily_cap": 1})
    with Session(client.app.state.engine) as session:
        set_flag(session, AUTO_APPLY_LINKEDIN, True)
        seed_profile(session)
        first, second = easy_job(session, 1, priority=0), easy_job(session, 2, priority=10)
        urls = (first.url, second.url)
    for url in urls:
        scripted(fake_browser, url=url)
    assert LinkedInApplier(rng=lambda low, high: 0).run(ctx).new == 1
    again = LinkedInApplier().run(ctx)
    assert again.status == ScanStatus.skipped and again.message == "Today's Easy Apply limit of 1 applications is reached."


def test_applier_stops_at_a_login_wall(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    with Session(client.app.state.engine) as session:
        set_flag(session, AUTO_APPLY_LINKEDIN, True)
        easy_job(session, 1, priority=0)
    fake_browser.routes = [("linkedin.com", page("https://www.linkedin.com/checkpoint/challenge/x"))]
    outcome = LinkedInApplier().run(make_ctx(client.app))
    assert outcome.status == ScanStatus.failed and outcome.message.endswith("Stopped: LinkedIn showed a security check; stopped.")
    linkedin = client.get("/api/connections").json()["items"][0]
    assert linkedin["status"] == "disconnected" and "security check" in linkedin["message"]


# ---------------------------------------------------------------------------
# Settings and the apply endpoint


def test_settings_flag(client):
    off = {
        "auto_send_email": False, "auto_send_linkedin_notes": False, "pause_naukri": False,
        "pause_linkedin": False, "pause_linkedin_jobs": False, "pause_linkedin_posts": False, "pause_naukri_jobs": False,
        "linkedin_scan_interval_minutes": 30, "naukri_scan_interval_minutes": 30,
        "linkedin_search_interval_minutes": 60,
    }
    assert client.get("/api/settings").json() == {"auto_apply_naukri": False, "auto_apply_linkedin": False, **off}
    assert client.put("/api/settings", json={"auto_apply_linkedin": True}).json() == {
        "auto_apply_naukri": False, "auto_apply_linkedin": True, **off,
    }
    assert client.put("/api/settings", json={"auto_apply_naukri": True}).json() == {
        "auto_apply_naukri": True, "auto_apply_linkedin": True, **off,
    }


def test_apply_endpoint_dispatches_linkedin_jobs(client, fake_browser):
    with Session(client.app.state.engine) as session:
        seed_profile(session)
        job = easy_job(session, 1, priority=0)
        company_site = easy_job(session, 2, priority=0, apply_type=ApplyType.company_site)
        job_id, url, other_id = job.id, job.url, company_site.id
    form = scripted(fake_browser, url=url)
    assert client.post(f"/api/jobs/{other_id}/apply").status_code == 422
    response = client.post(f"/api/jobs/{job_id}/apply", params={"dry_run": True})
    assert response.status_code == 409 and response.json()["detail"] == "LinkedIn isn't connected. Connect it first."

    connect_linkedin(client.app, fake_browser)
    dry = client.post(f"/api/jobs/{job_id}/apply", params={"dry_run": True}).json()
    assert dry["result"] == "would_apply" and dry["job"]["status"] == "found"
    assert [row["label"] for row in dry["facts"]["fields"]][:3] == ["Email address", "Phone country code", "Mobile phone number"]
    assert dry["facts"]["company"] == "Acme Software"
    assert not form.submitted and form.discarded

    form.discarded = False
    done = client.post(f"/api/jobs/{job_id}/apply").json()
    assert done["result"] == "applied" and done["job"]["status"] == "applied" and form.submitted
    assert client.post(f"/api/jobs/{job_id}/apply").status_code == 409  # already applied


def test_apply_endpoint_records_unanswered_questions(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    with Session(client.app.state.engine) as session:
        seed_profile(session)
        job = easy_job(session, 1, priority=0)
        job_id, url = job.id, job.url
    relocate = FakeField("Are you willing to work night shifts?", "radio", options=["Yes", "No"])
    scripted(fake_browser, contact_step(), questions_step(relocate), review_step(), url=url)
    body = client.post(f"/api/jobs/{job_id}/apply").json()
    assert body["result"] == "questionnaire" and body["questions"] == ["Are you willing to work night shifts?"]
    assert body["job"]["status"] == "needs_manual"
    (question,) = client.get("/api/questions/unanswered").json()["items"]
    assert (question["platform"], question["job_id"]) == ("linkedin", job_id)


def test_apply_endpoint_respects_the_easy_apply_cap(client, fake_browser, settings):
    connect_linkedin(client.app, fake_browser)
    client.app.state.settings = settings.model_copy(update={"easy_apply_daily_cap": 0})
    with Session(client.app.state.engine) as session:
        job_id = easy_job(session, 1, priority=0).id
    response = client.post(f"/api/jobs/{job_id}/apply")
    assert response.status_code == 409 and "limit of 0" in response.json()["detail"]


def test_stored_job_has_a_title():
    assert StoredJob("u", title="Backend Developer").title == "Backend Developer"


def test_a_company_site_job_keeps_the_company_apply_link(session):
    """2026-10-15: the job page's "Apply on company website" link is saved, so the dashboard opens the
    company's own page."""
    job = make_job(session, platform=Platform.linkedin, apply_type=ApplyType.easy_apply, status=JobStatus.found)
    attempt = ApplyAttempt(ApplyResult.company_site, "Company site.", facts={"apply_url": "https://careers.acme.com/1"})
    record_attempt(session, job, attempt)
    session.commit()
    assert (job.apply_type, job.status, job.apply_url) == (ApplyType.company_site, JobStatus.found, "https://careers.acme.com/1")
