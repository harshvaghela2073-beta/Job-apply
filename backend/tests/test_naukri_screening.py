import pytest
from sqlmodel import Session

from app.models import ApplyType, Job, JobStatus, Platform
from app.services.app_settings import AUTO_APPLY_NAUKRI, set_flag
from app.sources.naukri.apply import NaukriApplier
from app.sources.naukri.screening import (
    ApplyRules,
    JobFacts,
    looks_like_person,
    matching_skills,
    parse_job_facts,
    salary_top_lpa,
    skip_reason,
)
from tests.factories import make_field, make_job
from tests.fakes import page
from tests.scan_helpers import make_ctx
from tests.test_naukri import connect_naukri

MY_SKILLS = ("Java", "Spring Boot", "Golang", "Python", "SQL", "Redis", "Apache Kafka", "Kubernetes", "GKE")
RULES = ApplyRules(min_salary_lpa=12.0, skills=MY_SKILLS)


@pytest.mark.parametrize(
    ("label", "top"),
    [
        ("2.5-4.5 Lacs P.A.", 4.5),
        ("12-18 Lacs P.A.", 18.0),
        ("₹ 6,00,000 - 9,00,000 P.A.", 9.0),
        ("15 Lakhs", 15.0),
        ("Not Disclosed", None),
        ("", None),
    ],
)
def test_salary_top(label, top):
    assert salary_top_lpa(label) == top


def test_skill_matching_uses_word_boundaries_and_aliases():
    assert matching_skills(MY_SKILLS, ["JavaScript", "React"]) == []
    assert matching_skills(MY_SKILLS, ["Go", "Microservices"]) == ["Golang"]
    assert matching_skills(MY_SKILLS, ["Java Spring Boot", "AWS"]) == ["Java", "Spring Boot"]
    assert matching_skills(MY_SKILLS, ["MySQL", "Kafka"]) == ["SQL", "Apache Kafka"]
    assert matching_skills(MY_SKILLS, [".NET Core", "Angular", "SQL Server"]) == ["SQL"]


def test_person_names():
    assert looks_like_person("Surender Singh")
    assert not looks_like_person("Great Media Technologies")
    assert not looks_like_person("Codewave")
    assert not looks_like_person("Aviato Consulting")


def facts(**values) -> JobFacts:
    base = dict(company="Acme Software", salary="Not Disclosed", experience="2-5 Yrs", skills=["Java", "Spring Boot"])
    base.update(values)
    return JobFacts(**base)


@pytest.mark.parametrize(
    ("job", "reason_start"),
    [
        (facts(), None),
        (facts(salary="2.5-4.5 Lacs P.A."), "Salary 2.5-4.5 Lacs P.A. is below your current 12 LPA"),
        (facts(salary="12-18 Lacs P.A."), None),
        (facts(skills=[".NET Core", "Angular", "C#"]), "None of your skills"),
        # At least 2 of your skills in the key skills, title and description (2026-10-15).
        (facts(skills=["Java", "AWS"]), "Only 1 of your skills (Java) is in the job's key skills (Java, AWS), title or description; at least 2 are needed."),
        (facts(skills=["Java", "AWS"], description="Kafka pipelines on GKE"), None),
        (facts(skills=["Java", "AWS"], description="We go live soon"), "Only 1 of your skills (Java)"),
        (facts(skills=["Java", "Go"]), None),  # a key-skill tag "Go" is Golang
        (facts(experience="0-1 Yrs", description="Internship or training experience is fine"), "Fresher / internship-style"),
        (facts(experience="0-3 Yrs", description="Design and build Java services"), None),
        (facts(company="MNC's", posted_by="Siri Tech Solutions"), "The company is hidden"),
        (facts(company="Seacross Recruitment Services"), "Posted by a consultancy"),
        (facts(company="Surender Singh"), "Posted by an individual recruiter"),
        (facts(company="Aviato Consulting"), None),
    ],
)
def test_skip_reason(job, reason_start):
    reason = skip_reason(RULES, job)
    if reason_start is None:
        assert reason is None
    else:
        assert reason is not None and reason.startswith(reason_start)


def test_stored_values_fill_in_for_the_page():
    empty_page = JobFacts()
    assert skip_reason(RULES, empty_page, stored_text="Angular, .NET · Build apps") is not None
    assert skip_reason(RULES, empty_page, stored_text="Java, Spring Boot · Build apps") is None
    assert skip_reason(RULES, empty_page, stored_text="Java · Build apps on Kafka") is None  # the stored description counts
    assert skip_reason(RULES, empty_page, stored_text="Java · Build apps").startswith("Only 1 of your skills (Java)")
    assert skip_reason(RULES, empty_page, stored_company="Surender Singh", stored_text="Java, Redis · x").startswith(
        "Posted by an individual recruiter"
    )


RULES_WITH_SKIP_LIST = ApplyRules(min_salary_lpa=12.0, skills=MY_SKILLS, skip_companies=("Jobgether", "Scoutit"))


@pytest.mark.parametrize(
    ("job", "stored", "reason"),
    [
        (facts(company="Scoutit"), {}, "Scoutit is on your skip list."),
        (facts(company=""), {"stored_company": "Jobgether Inc"}, "Jobgether Inc is on your skip list."),
        (facts(), {"stored_title": "Dotnet Full Stack Developer"}, 'Another stack: the title says "Dotnet" and names none of your skills.'),
        (facts(), {"stored_title": "Oracle Software Developer"}, 'Another stack: the title says "Oracle" and names none of your skills.'),
        (facts(), {"stored_title": "Java Full Stack Developer (Spring Boot + Angular)"}, None),
        (facts(), {"stored_title": "Senior Java Developer"}, 'Too senior: the title says "Senior".'),
        (facts(), {"stored_title": "Team Lead - Backend"}, 'Too senior: the title says "Lead".'),
        (facts(), {"stored_title": "SDE-3"}, 'Too senior: the title says "SDE-3".'),
        (facts(), {"stored_title": "SDE 2"}, None),
        (facts(company="Acme Software"), {"stored_title": "Software Engineer II"}, None),
    ],
)
def test_job_fit_rules(job, stored, reason):
    assert skip_reason(RULES_WITH_SKIP_LIST, job, **stored) == reason
    if reason and "skip list" in reason:
        assert skip_reason(RULES, job, **stored) is None  # without a skip list nothing is skipped for it


JOB_PAGE = """<html><body>
<section class="styles_job-header-container___0wLZ">
  <h1>Java Developer</h1>
  <div class="styles_jhc__comp-name__abc"><a href="/x-jobs-careers-1">MNC's</a></div>
  <div>Posted by Siri Tech Solutions</div>
  <div class="styles_jhc__exp__k_giM"><span>0 - 3 years</span></div>
  <div class="styles_jhc__salary__jdfEC"><span>2.5-4.5 Lacs P.A.</span></div>
</section>
<div class="styles_key-skill__GIPn_"><a href="#"><span>Java</span></a><a href="#"><span>AWS</span></a></div>
<section class="styles_job-desc-container"><div class="styles_JDC__dang-inner-html__h0K4t">Design Java apps.</div></section>
</body></html>"""


def test_parse_job_facts():
    found = parse_job_facts(JOB_PAGE)
    assert (found.company, found.posted_by, found.salary, found.experience) == (
        "MNC's", "Siri Tech Solutions", "2.5-4.5 Lacs P.A.", "0 - 3 years"
    )
    assert found.skills == ["Java", "AWS"] and found.description == "Design Java apps."


def test_applier_records_skipped_jobs_without_clicking(client, settings, fake_browser):
    connect_naukri(client, settings)
    with Session(client.app.state.engine) as session:
        set_flag(session, AUTO_APPLY_NAUKRI, True)
        make_field(session, key="current_ctc", label="CTC", section="Current job", value="12 LPA")
        make_field(session, key="skills", label="Skills", section="Skills", value=", ".join(MY_SKILLS))
        job = make_job(
            session, platform=Platform.naukri, external_id="nk-1", url="https://www.naukri.com/job-listings-java-1",
            apply_type=ApplyType.one_click, status=JobStatus.found, priority=0, title="Java Developer",
        )
        job_id = job.id
    page_html = JOB_PAGE.replace("</section>\n<div", '<button id="apply-button">Apply</button></section>\n<div', 1)
    fake_browser.routes = [("job-listings", page("https://www.naukri.com/job-listings-java-1", page_html))]
    fake_browser.click_routes = [("job-listings", page("https://www.naukri.com/job-listings-java-1", "<p>successfully applied</p>"))]

    outcome = NaukriApplier().run(make_ctx(client.app))

    assert fake_browser.clicks == []
    assert outcome.new == 0 and outcome.message == "Applied to 0, 1 skipped by your rules."
    with Session(client.app.state.engine) as session:
        stored = session.get(Job, job_id)
        assert stored.status == JobStatus.skipped
        assert stored.status_reason.startswith("Not auto-applied: Salary 2.5-4.5 Lacs P.A. is below")


def test_applier_skips_jobs_that_dont_fit(client, settings, fake_browser):
    """Jobs stored before the job-fit rules (2026-10-15): the stored title and the skip list count."""
    connect_naukri(client, settings)
    with Session(client.app.state.engine) as session:
        set_flag(session, AUTO_APPLY_NAUKRI, True)
        make_field(session, key="current_ctc", label="CTC", section="Current job", value="2 LPA")
        make_field(session, key="skills", label="Skills", section="Skills", value=", ".join(MY_SKILLS), position=2)
        make_field(session, key="search_skip_companies", label="Skip companies", section="Job search priority",
                   value="Jobgether, Crossing Hurdles, Scoutit, Applicantz, CodeRound AI", position=3)
        senior = make_job(
            session, platform=Platform.naukri, external_id="nk-2", url="https://www.naukri.com/job-listings-senior-2",
            apply_type=ApplyType.one_click, status=JobStatus.found, priority=0, title="Senior Java Developer",
            company="Acme Software",
        )
        aggregator = make_job(
            session, platform=Platform.naukri, external_id="nk-3", url="https://www.naukri.com/job-listings-java-3",
            apply_type=ApplyType.one_click, status=JobStatus.found, priority=1, title="Java Developer",
            company="Scoutit",
        )
        ids = (senior.id, aggregator.id)
    page_html = JOB_PAGE.replace("</section>\n<div", '<button id="apply-button">Apply</button></section>\n<div', 1)
    fake_browser.routes = [
        ("job-listings-senior-2", page("https://www.naukri.com/job-listings-senior-2", page_html.replace("MNC's", "Acme Software"))),
        ("job-listings-java-3", page("https://www.naukri.com/job-listings-java-3", page_html.replace("MNC's", "Scoutit"))),
    ]

    outcome = NaukriApplier().run(make_ctx(client.app))

    assert fake_browser.clicks == [] and outcome.message == "Applied to 0, 2 skipped by your rules."
    with Session(client.app.state.engine) as session:
        reasons = [session.get(Job, job_id).status_reason for job_id in ids]
    assert reasons == ['Not auto-applied: Too senior: the title says "Senior".', "Not auto-applied: Scoutit is on your skip list."]
