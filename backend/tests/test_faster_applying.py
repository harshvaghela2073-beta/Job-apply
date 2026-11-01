"""Faster, more relevant applying (2026-10-21; docs/API_CONTRACT.md): default answers for questions
nobody saved, skipping instead of waiting, the salary target, the LinkedIn home feed and the company
links of company-site jobs. Synthetic pages only; people and companies are made up."""

from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.models import ApplyType, Job, JobStatus, Platform, ScanStatus, Template, TemplateKind, UnansweredQuestion
from app.scanner.applying import ApplyAttempt, ApplyResult, no_saved_answer, record_attempt
from app.scanner.fit import target_salary_lpa
from app.services.app_settings import AUTO_APPLY_LINKEDIN, set_flag
from app.sources.linkedin import selectors as S
from app.sources.linkedin.answers import DEFAULT_SOURCE, AnswerBook, FormField, save_learned
from app.sources.linkedin.easy_apply import (
    COMPANY_LINKS_PER_RUN,
    NO_COMPANY_LINK,
    LinkedInApplier,
    apply_easy,
)
from app.sources.linkedin.parse import parse_posts
from app.sources.linkedin.posts import LinkedInPostsScanner
from app.sources.linkedin.screening import ApplyRules, LinkedInJobFacts, LinkedInRules, load_rules, skip_reason
from app.timeutils import utcnow
from tests.factories import make_field, make_job
from tests.fakes import FakeField, FakeSession, FakeStep, page
from tests.scan_helpers import connect_linkedin, make_ctx, ten_am_today
from tests.test_linkedin_easy_apply import (
    BOOK,
    JOB_URL,
    MY_SKILLS,
    PROFILE,
    browser_for,
    contact_step,
    review_step,
    scripted,
    seed_profile,
)
from tests.test_naukri_chatbot import FakeNaukriJob, Q
from tests.test_naukri_chatbot import book as naukri_book
from tests.test_naukri_chatbot import run as naukri_run

NOW = ten_am_today()
SKILLS = {"skills": "Java, Spring Boot, Golang, SQL, Apache Kafka", "location": "Bengaluru"}


def answer(label: str, kind: str = "radio", options=("Yes", "No"), *, profile=SKILLS, templates=()):
    answers = AnswerBook(profile=dict(profile), templates=list(templates))
    result = answers.answer(FormField(id="1", label=label, type=kind, required=True, options=tuple(options)))
    return result, answers


# ---------------------------------------------------------------------------
# Default answers


@pytest.mark.parametrize(
    ("label", "kind", "options", "expected"),
    [
        # Availability for interviews, any day or mode
        ("Are you available for a face-to-face interview on Saturday?", "radio", ("Yes", "No"), "Yes"),
        ("Are you comfortable with a virtual interview?", "select", ("Select an option", "Yes", "No"), "Yes"),
        ("Can you attend a weekend interview?", "text", (), "Yes"),
        ("Are you available for an F2F interview?", "radio", ("Yes, I am available", "No"), "Yes, I am available"),
        # Background verification, consent, office / hybrid in Bengaluru
        ("Are you okay with a BGV?", "radio", ("Yes", "No"), "Yes"),
        ("Do you consent to Acme Example creating a recruiter profile for you?", "checkbox", (), "true"),
        ("I agree to receive communications from Example Labs", "checkbox", ("I agree",), "I agree"),
        ("Do you agree to our privacy policy?", "radio", ("I agree", "I don't agree"), "I agree"),
        ("Are you comfortable working 5 days from office in Bengaluru?", "radio", ("Yes", "No"), "Yes"),
        ("Are you okay with WFO?", "radio", ("Yes", "No"), "Yes"),
        # Self-ratings: 8/10 or 4/5 for your skills, 4/10 or 2/5 otherwise
        ("Rate yourself out of 10 in Java", "numeric", (), "8"),
        ("Rate yourself out of 10 in Angular", "text", (), "4"),
        ("On a scale of 1-5, how would you rate your Kafka skills?", "select", ("1", "2", "3", "4", "5"), "4"),
        ("On a scale of 1 to 5, how would you rate your React skills?", "radio", ("1", "2", "3", "4", "5"), "2"),
        ("How would you rate your Golang skills?", "select", tuple(str(n) for n in range(1, 11)), "8"),  # scale from options
        ("Self-rating in Spring Boot (out of 10)", "select", ("Select an option", "1", "3", "5", "7", "9"), "7"),  # closest
        ("Rate your expertise in Rust out of 5", "radio", ("1 - Beginner", "3 - Good", "5 - Expert"), "1 - Beginner"),
        # Another city keeps the existing No; the rest isn't guessed
        ("Are you willing to relocate to Pune?", "radio", ("Yes", "No"), "No"),
        ("Are you willing to relocate to Jaipur?", "radio", ("Yes", "No"), "No"),
        ("Are you available for a face-to-face interview in Pune?", "radio", ("Yes", "No"), None),
        ("Are you comfortable working from our Hyderabad office?", "radio", ("Yes", "No"), None),
        # Not one of the defaults
        ("Are you willing to work night shifts?", "radio", ("Yes", "No"), None),
        ("Are you willing to sign a 2 year service bond?", "radio", ("Yes", "No"), None),
        ("Have you interviewed with Acme Example in the last 6 months?", "radio", ("Yes", "No"), None),
        ("Which days are you available for an interview?", "text", (), None),
        ("Why do you want to join Acme Example?", "textarea", (), None),
        ("On a scale of 1-10, how proficient are you in Java?", "text", (), "8"),
        ("Rate your Java skills", "text", (), None),  # no scale
        ("How would you rate your last appraisal (out of 5)?", "radio", ("1", "2", "3", "4", "5"), None),
        ("Performance rating in your current company", "select", ("1", "2", "3", "4", "5"), None),
        ("What is your hourly rate?", "text", (), None),
        ("Are you currently going to office?", "radio", ("Yes", "No"), None),  # what you do now
    ],
)
def test_defaults(label, kind, options, expected):
    result, answers = answer(label, kind, options)
    assert result.value == expected, result.reason
    if expected is not None and "relocate" not in label:
        assert result.reason == DEFAULT_SOURCE
        assert answers.learned == [(label, "Yes" if expected == "true" else expected)]


def test_a_saved_answer_wins_over_the_default():
    result, answers = answer(
        "Are you available for a Saturday interview?", templates=[("Are you available for a Saturday interview?", "No")]
    )
    assert result.value == "No" and result.reason.startswith("template") and answers.learned == []


def test_defaults_are_saved_as_templates_and_reused(session):
    _, answers = answer("Rate yourself out of 10 in Java", "numeric", ())
    assert save_learned(session, answers.learned) == 1
    session.commit()
    stored = session.exec(select(Template).where(Template.kind == TemplateKind.answer)).one()
    assert (stored.question, stored.answer) == ("Rate yourself out of 10 in Java", "8")
    again = AnswerBook.load(session).answer(
        FormField(id="1", label="Rate yourself out of 10 in Java", type="numeric", required=True)
    )
    assert again.value == "8" and again.reason.startswith('template "Rate yourself')


def test_easy_apply_finishes_with_default_answers(fake_browser):
    step = FakeStep([
        FakeField("Are you available for a virtual interview on Saturday?", "radio", options=["Yes", "No"]),
        FakeField("Rate yourself out of 10 in Java", "numeric"),
    ], "review", "Additional Questions")
    form = scripted(fake_browser, contact_step(), step, review_step())
    answers = AnswerBook(profile={**PROFILE, "skills": ", ".join(MY_SKILLS)}, templates=list(BOOK.templates))
    attempt = apply_easy(browser_for(fake_browser), JOB_URL, answers)
    assert attempt.result == ApplyResult.applied and form.submitted
    rows = {row["label"]: row for row in attempt.facts["fields"]}
    assert rows["Rate yourself out of 10 in Java"]["value"] == "8"
    assert {rows[label]["reason"] for label in rows if label in dict(answers.learned)} == {DEFAULT_SOURCE}


def test_naukri_recruiter_questions_get_the_defaults_too():
    job = FakeNaukriJob("4001", [
        Q("Are you available for a face to face interview on Saturday?", "radio", ("Yes", "No")),
        Q("Rate yourself out of 10 in Java"),
    ])
    attempt = naukri_run(job, naukri_book())
    assert attempt.result == ApplyResult.applied, attempt.detail
    assert job.sent == ["Yes", "8"]
    assert [row["source"] for row in attempt.facts["questions"]] == [DEFAULT_SOURCE, DEFAULT_SOURCE]


# ---------------------------------------------------------------------------
# Skipped instead of waiting


def test_no_saved_answer_wording():
    assert no_saved_answer(["Notice?"]) == 'The form asks "Notice?", which has no saved answer.'
    assert no_saved_answer(["A?", "B?", "C?"]) == 'The form asks "A?", "B?" and "C?", which have no saved answer.'


def test_a_skipped_job_keeps_its_question_for_later(session):
    job = make_job(session, platform=Platform.linkedin, apply_type=ApplyType.easy_apply, status=JobStatus.found)
    record_attempt(session, job, ApplyAttempt(ApplyResult.skipped, no_saved_answer(["Shift?"]), ["Shift?"], clicked=True))
    rules_job = make_job(session, platform=Platform.linkedin, apply_type=ApplyType.easy_apply, status=JobStatus.found)
    record_attempt(session, rules_job, ApplyAttempt(ApplyResult.skipped, "Internship posting."))
    session.commit()
    assert (job.status, job.status_reason) == (
        JobStatus.skipped, 'Not auto-applied: The form asks "Shift?", which has no saved answer.'
    )
    (question,) = session.exec(select(UnansweredQuestion)).all()
    assert (question.question, question.job_id, question.platform) == ("Shift?", job.id, Platform.linkedin)


def test_naukri_skip_records_the_question(session):
    row = make_job(session, platform=Platform.naukri, apply_type=ApplyType.one_click, status=JobStatus.found)
    attempt = naukri_run(FakeNaukriJob("4002", [Q("Date of Birth (DD/MM/YYYY)")]), naukri_book())
    assert attempt.result == ApplyResult.skipped
    record_attempt(session, row, attempt)
    session.commit()
    assert row.status == JobStatus.skipped
    assert [q.question for q in session.exec(select(UnansweredQuestion)).all()] == ["Date of Birth (DD/MM/YYYY)"]


# ---------------------------------------------------------------------------
# Salary target


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("16-18 LPA", 16.0), ("16 LPA", 16.0), ("16+ LPA", 16.0), ("12.5 - 15 lakhs", 12.5),
        ("16,00,000 - 18,00,000", 16.0), ("", None), (None, None),
        ({"search_salary_lpa": "16-18 LPA", "current_ctc": "12 LPA"}, 16.0), ({"current_ctc": "12 LPA"}, None),
    ],
)
def test_target_salary(value, expected):
    assert target_salary_lpa(value) == expected


def job_facts(salary: str) -> LinkedInJobFacts:
    return LinkedInJobFacts(title="Backend Developer", company="Acme Example", salary=salary,
                            description="Java and Spring Boot services.")


def test_salary_below_the_target_is_skipped():
    rules = LinkedInRules(min_salary_lpa=12.0, skills=MY_SKILLS, target_salary_lpa=16.0)
    assert skip_reason(rules, job_facts("₹12L/yr - ₹15L/yr")) == "Salary ₹12L/yr - ₹15L/yr is below your target 16 LPA."
    assert skip_reason(rules, job_facts("₹14L/yr - ₹18L/yr")) is None  # tops out above the target
    assert skip_reason(rules, job_facts("")) is None  # no salary stated
    # Without a target: the current CTC, as before.
    plain = ApplyRules(min_salary_lpa=12.0, skills=MY_SKILLS)
    assert skip_reason(plain, job_facts("₹8L/yr - ₹10L/yr")) == "Salary ₹8L/yr - ₹10L/yr is below your current 12 LPA."
    assert skip_reason(plain, job_facts("₹12L/yr - ₹15L/yr")) is None


def test_rules_read_the_target(session):
    make_field(session, key="current_ctc", value="12 LPA", position=1)
    rules = load_rules(session)
    assert (rules.min_salary_lpa, rules.target_salary_lpa) == (12.0, None)
    make_field(session, key="search_salary_lpa", value="16-18 LPA", position=2)
    assert load_rules(session).target_salary_lpa == 16.0


# ---------------------------------------------------------------------------
# The home feed


def sdui_post(key: str, author: str, text: str, *, slug: str = "", ago: str = "2h") -> str:
    href = f"https://www.linkedin.com/in/{slug or author.lower().replace(' ', '-')}/"
    return (f'<div componentkey="{key}"><a href="{href}">{author} • 2nd</a><div>Recruiter at Example Labs</div>'
            f'<div>{ago} • </div><div data-testid="expandable-text-box">{text}</div></div>')


FEED_FORM = sdui_post(
    "urn:li:activity:7390000000000000001FeedType_MAIN_FEED", "Asha Example",
    "We're hiring Backend Developers (1-3 years) in Bengaluru! Google Form: https://forms.gle/FeedForm1",
)
FEED_EMAIL = sdui_post(
    "expandedFeEd02FeedType_MAIN_FEED_RELEVANCE", "Ravi Example",
    "Hiring Java Developer, 2+ years, Bengaluru. Mail your resume to talent@example-labs.com",
)
FEED_OLD = sdui_post(
    "expandedFeEd03FeedType_MAIN_FEED", "Mira Example", "Hiring Java Developer, Bengaluru. jobs@example.com", ago="3d",
)


def feed_page(*posts: str) -> str:
    return f'<html><body><main><div componentkey="mainFeedFeedType_LIST">{"".join(posts)}</div></main></body></html>'


def test_feed_posts_parse_with_any_feed_type():
    first, second = parse_posts(feed_page(FEED_FORM, FEED_EMAIL))
    # The list around the posts has a FeedType_ key too: each post is its own wrapper.
    assert (first.urn, second.urn) == ("sdui:urn:li:activity:7390000000000000001", "sdui:FeEd02")
    assert (first.author, first.author_url) == ("Asha Example", "https://www.linkedin.com/in/asha-example/")
    assert first.headline == "Recruiter at Example Labs" and first.time_label.startswith("2h")
    assert "forms.gle/FeedForm1" in first.text and "talent@example-labs.com" in second.text


def test_feed_post_whose_text_sits_in_an_inner_wrapper():
    html = ('<div componentkey="expandedOuter1FeedType_MAIN_FEED"><a href="https://www.linkedin.com/in/kiran-example/">'
            'Kiran Example</a><span>5h • </span><div componentkey="commentaryOuter1FeedType_MAIN_FEED_TEXT">'
            '<div data-testid="expandable-text-box">Hiring SDE1 in Bengaluru, jobs@example.com</div></div></div>')
    (post,) = parse_posts(html)
    assert (post.urn, post.author) == ("sdui:Outer1", "Kiran Example")


def test_unknown_feed_markup_falls_back_to_loose_text_boxes():
    article = ('<article><a href="https://www.linkedin.com/in/neha-example/">Neha Example</a><span>4h • </span>'
               '<div data-testid="expandable-text-box">{}</div></article>')
    html = "<main>" + article.format("Hiring SDE2 in Bengaluru, jobs@example.com") + article.format("Hello world") + "</main>"
    posts = parse_posts(html)
    assert [p.text for p in posts] == ["Hiring SDE2 in Bengaluru, jobs@example.com", "Hello world"]
    assert posts[0].urn.startswith("text:") and posts[0].author == "Neha Example"
    assert [p.urn for p in parse_posts(html)] == [p.urn for p in posts]  # the same id on every read


SEARCH_POST = sdui_post(
    "expandedSrch01FeedType_FLAGSHIP_SEARCH", "Dev Example",
    "Hiring Golang Developer (1-3 years) in Bengaluru. Apply: https://forms.gle/Search01",
)


def posts_routes(fake_browser, feed_html: str | None) -> None:
    search = f"<html><body><main>{SEARCH_POST}{FEED_EMAIL}</main></body></html>"  # one post also on the feed
    routes = [("search/results/content", page("https://www.linkedin.com/search/results/content/?keywords=x", search))]
    if feed_html is not None:
        routes.insert(0, ("/feed/", page(S.FEED_URL, feed_html)))
    fake_browser.routes = routes


def test_feed_is_read_first_and_counted_apart(client, fake_browser):
    posts_routes(fake_browser, feed_page(FEED_FORM, FEED_EMAIL, FEED_OLD))
    session = FakeSession(fake_browser, None, True)
    report = LinkedInPostsScanner().scan(session, make_ctx(client.app, NOW))
    assert report.failure is None and report.login_ok
    assert fake_browser.visited[0] == S.FEED_URL
    assert session.scrolls >= S.FEED_SCROLLS
    by_id = {c.external_id: c for c in report.candidates}
    assert set(by_id) == {"sdui:urn:li:activity:7390000000000000001", "sdui:FeEd02", "sdui:Srch01"}
    assert by_id["sdui:urn:li:activity:7390000000000000001"].form_url == "https://forms.gle/FeedForm1"
    assert by_id["sdui:FeEd02"].contact_email == "talent@example-labs.com"
    (note,) = [n for n in report.notes if n.startswith("Read ")]
    assert note.startswith("Read 3 feed posts and 1 search posts;")
    assert "1 too old" in note  # the same rules: a 3-day-old email post


def test_an_unreadable_feed_saves_a_diagnostic_page_and_the_searches_still_run(client, fake_browser):
    posts_routes(fake_browser, "<html><body><main><div>Something new</div></main></body></html>")
    session = FakeSession(fake_browser, None, True)
    report = LinkedInPostsScanner().scan(session, make_ctx(client.app, NOW))
    assert report.failure is None
    assert report.notes[0].startswith("The home feed showed no readable posts. Screenshot: data/diagnostics/linkedin_posts-")
    assert "Page: data/diagnostics/" in report.notes[0]
    assert any("linkedin_posts-" in shot for shot in fake_browser.screenshots)
    assert {c.external_id for c in report.candidates} == {"sdui:Srch01", "sdui:FeEd02"}
    assert any(n.startswith("Read 0 feed posts and 2 search posts;") for n in report.notes)


def test_a_feed_that_fails_to_open_doesnt_stop_the_searches(client, fake_browser):
    posts_routes(fake_browser, None)

    def broken(url):
        raise RuntimeError("Timeout 60000ms exceeded")

    fake_browser.routes.insert(0, ("/feed/", broken))
    report = LinkedInPostsScanner().scan(FakeSession(fake_browser, None, True), make_ctx(client.app, NOW))
    assert report.failure is None
    assert report.notes[0].startswith("The home feed couldn't be opened (RuntimeError).")
    assert {c.external_id for c in report.candidates} == {"sdui:Srch01", "sdui:FeEd02"}


def test_a_login_wall_on_the_feed_stops_the_run(client, fake_browser):
    fake_browser.routes = [("/feed/", page("https://www.linkedin.com/uas/login?session_redirect=feed"))]
    report = LinkedInPostsScanner().scan(FakeSession(fake_browser, None, True), make_ctx(client.app, NOW))
    assert report.failure == "LinkedIn redirected to its login page; stopped."
    assert report.login_lost and fake_browser.visited == [S.FEED_URL]


def test_the_feed_stays_within_the_page_budget():
    queries = 5 * (1 + S.POST_FORM_SCROLLS) + 8 * (1 + S.POST_SCROLLS)
    assert 1 + S.FEED_SCROLLS + queries <= S.POST_MAX_PAGE_LOADS


# ---------------------------------------------------------------------------
# Company links


def company_page(kind: str) -> str:
    button = {
        "link": '<a aria-label="Apply on company website" href="https://www.linkedin.com/safety/go/?url='
                'https%3A%2F%2Fcareers.acme.example%2Fjobs%2F{n}&amp;trk=x">Apply</a>',
        "easy": '<button class="jobs-apply-button" aria-label="Easy Apply to Backend Developer at Acme Example">'
                "<span>Easy Apply</span></button>",
        "applied": '<div class="artdeco-inline-feedback artdeco-inline-feedback--success"><span>Applied 2 days ago</span></div>',
        "closed": '<div class="jobs-details-top-card__apply-error"><span>No longer accepting applications</span></div>',
        "none": "",
    }[kind]
    team = (
        '<div class="job-details-people-who-can-help__section--two-pane"><h2>Meet the hiring team</h2>'
        '<div class="hirer-card__hirer-information"><a href="https://www.linkedin.com/in/tara-example/">'
        '<span class="jobs-poster__name"><strong>Tara Example</strong></span></a>'
        '<span class="hirer-card__connection-degree">· 2nd</span>'
        '<div class="linked-area"><div class="text-body-small">Talent Acquisition at Acme Example</div></div>'
        '<div class="hirer-card__job-poster">Job poster</div></div></div>'
    )
    return (f'<div class="job-details-jobs-unified-top-card__container--two-pane"><h1>Backend Developer</h1>'
            f"{button}</div>{team}")


def company_job(session, n: int, *, minutes_ago: int = 0, **extra) -> Job:
    values = dict(
        platform=Platform.linkedin, external_id=f"40200000{n:02d}", url=f"https://www.linkedin.com/jobs/view/40200000{n:02d}/",
        apply_type=ApplyType.company_site, status=JobStatus.found, company="Acme Example", title="Backend Developer",
        found_at=utcnow() - timedelta(minutes=minutes_ago),
    )
    values.update(extra)
    return make_job(session, **values)


def route_company_pages(fake_browser, kinds: dict[int, str]) -> None:
    fake_browser.routes = [
        (f"/jobs/view/40200000{n:02d}", page(f"https://www.linkedin.com/jobs/view/40200000{n:02d}/",
                                              company_page(kind).replace("{n}", str(n))))
        for n, kind in kinds.items()
    ]


def test_company_links_are_stored(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    engine = client.app.state.engine
    kinds = {1: "link", 2: "easy", 3: "applied", 4: "closed", 5: "none"}
    with Session(engine) as session:
        ids = {n: company_job(session, n, minutes_ago=n).id for n in kinds}
        known = company_job(session, 6, apply_url="https://careers.acme.example/jobs/6").id  # has its link
        old = company_job(session, 7, found_at=utcnow() - timedelta(days=3)).id  # not new
        manual = company_job(session, 8, status=JobStatus.needs_manual).id
        easy = company_job(session, 9, apply_type=ApplyType.easy_apply).id  # Easy Apply is off
    route_company_pages(fake_browser, kinds)

    outcome = LinkedInApplier().run(make_ctx(client.app))  # auto-apply is off: the links still run

    assert outcome.status == ScanStatus.ok
    assert outcome.message == (
        "LinkedIn auto-apply is off. Found company links for 1 jobs. "
        "1 turned out to be Easy Apply, 1 already applied, 1 no longer accepting applications."
    )
    opened = [url for url in fake_browser.visited if "/jobs/view/" in url]
    assert opened == [f"https://www.linkedin.com/jobs/view/40200000{n:02d}/" for n in kinds]  # newest first
    with Session(engine) as session:
        jobs = {n: session.get(Job, i) for n, i in ids.items()}
        assert (jobs[1].apply_url, jobs[1].status) == ("https://careers.acme.example/jobs/1", JobStatus.found)
        assert (jobs[2].apply_type, jobs[2].status) == (ApplyType.easy_apply, JobStatus.found)
        assert jobs[3].status == JobStatus.applied and jobs[3].applied_at is not None
        assert jobs[4].status == JobStatus.skipped
        assert (jobs[5].status, jobs[5].status_reason) == (JobStatus.found, NO_COMPANY_LINK)
        assert jobs[1].contact_profile_url == "https://www.linkedin.com/in/tara-example/"  # for the notes later
        assert all(session.get(Job, i).apply_url in (None, "https://careers.acme.example/jobs/6")
                   for i in (known, old, manual, easy))

    # The next run opens none of them again (a page without a link isn't reopened).
    fake_browser.visited.clear()
    again = LinkedInApplier().run(make_ctx(client.app))
    assert (again.status, again.message) == (ScanStatus.skipped, "LinkedIn auto-apply is off.")
    assert not [url for url in fake_browser.visited if "/jobs/view/" in url]


def test_company_links_keep_to_the_page_budget(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    with Session(client.app.state.engine) as session:
        for n in range(1, COMPANY_LINKS_PER_RUN + 3):
            company_job(session, n, minutes_ago=n)
    route_company_pages(fake_browser, {n: "link" for n in range(1, COMPANY_LINKS_PER_RUN + 3)})

    outcome = LinkedInApplier().run(make_ctx(client.app))
    opened = [url for url in fake_browser.visited if "/jobs/view/" in url]
    assert len(opened) == COMPANY_LINKS_PER_RUN and outcome.message.endswith(f"Found company links for {COMPANY_LINKS_PER_RUN} jobs.")
    assert opened[0].endswith("/4020000001/")  # the newest

    fake_browser.visited.clear()
    LinkedInApplier().run(make_ctx(client.app, max_loads=1))  # SCAN_MAX_PAGE_LOADS caps it too
    assert len([url for url in fake_browser.visited if "/jobs/view/" in url]) == 1


def test_company_links_come_after_easy_apply(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    engine = client.app.state.engine
    with Session(engine) as session:
        set_flag(session, AUTO_APPLY_LINKEDIN, True)
        seed_profile(session)
        easy = make_job(session, platform=Platform.linkedin, external_id="4010000001", url=JOB_URL,
                        apply_type=ApplyType.easy_apply, status=JobStatus.found, priority=0, company="Acme Software",
                        title="Backend Developer")
        company_job(session, 1)
        easy_id = easy.id
    route_company_pages(fake_browser, {1: "link"})
    form = scripted(fake_browser)

    outcome = LinkedInApplier(rng=lambda low, high: 0).run(make_ctx(client.app))

    opened = [url for url in fake_browser.visited if "/jobs/view/" in url]
    assert opened == [JOB_URL, "https://www.linkedin.com/jobs/view/4020000001/"]
    assert form.submitted and outcome.message == "Applied to 1. Found company links for 1 jobs."
    with Session(engine) as session:
        assert session.get(Job, easy_id).status == JobStatus.applied


def test_a_login_wall_on_a_company_page_stops_the_run(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    with Session(client.app.state.engine) as session:
        job_id = company_job(session, 1).id
    fake_browser.routes = [("/jobs/view/", page("https://www.linkedin.com/uas/login?session_redirect=x"))]

    outcome = LinkedInApplier().run(make_ctx(client.app))

    assert outcome.status == ScanStatus.failed
    assert outcome.message.endswith("Stopped: LinkedIn redirected to its login page; the saved login has expired.")
    assert client.get("/api/connections").json()["items"][0]["status"] == "disconnected"
    with Session(client.app.state.engine) as session:
        job = session.get(Job, job_id)
        assert (job.status, job.status_reason, job.apply_url) == (JobStatus.found, None, None)


@pytest.mark.parametrize(
    "label",
    [
        "Only apply if you can go to the office for the interview, Bangalore",
        "You can come to our Bengaluru office for a face to face interview?",
        "If selected, can you visit the office for the final round of interview?",
    ],
)
def test_in_person_interview_wordings_get_yes(label):
    """Live on 2026-10-21 (Recro): "if you can go to the office for the interview" had no answer."""
    from app.sources.linkedin.answers import AnswerBook, DEFAULT_SOURCE, FormField

    book = AnswerBook(profile={"skills": "Java", "location": "Bengaluru"}, templates=[])
    answer = book.answer(FormField(id="1", label=label, type="radio", required=True, options=("Yes", "No")))
    assert (answer.value, answer.reason) == ("Yes", DEFAULT_SOURCE)
