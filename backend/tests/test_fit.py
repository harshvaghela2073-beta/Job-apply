"""Job-fit rules approved on 2026-10-15 (``app.scanner.fit``): another stack, senior titles, the
company skip list and the 2-skill minimum. The scanners and appliers that use them are tested in
their own files (search time, apply time, hiring posts)."""

import pytest

from app.scanner.fit import (
    LEFT_OUT,
    OFF_STACK,
    left_out_note,
    matching_skills,
    off_stack_term,
    search_left_out,
    senior_term,
    skip_list,
    skip_listed,
    title_reason,
    too_few_skills,
)
from app.scanner.matching import load_prefs
from app.sources.linkedin.screening import load_rules as linkedin_rules
from app.sources.naukri.screening import load_rules as naukri_rules
from tests.factories import make_field
from tests.scan_helpers import FIT_PREFS, PREFS, SKILLS, SKIP_COMPANIES

MINE = tuple(s.strip() for s in SKILLS.split(","))
SKIP = tuple(s.strip() for s in SKIP_COMPANIES.split(","))

# ---------------------------------------------------------------------------
# Another stack

# One title per entry of the vocabulary table (every entry must have one).
OFF_STACK_EXAMPLES = {
    "Android": "Android SDE 2",
    "iOS": "iOS Developer",
    "Swift": "Swift Engineer",
    "Flutter": "Flutter Developer",
    "React Native": "React Native Developer",
    "Kotlin": "Kotlin Developer",
    ".NET": "Full Stack Developer - .Net / React.js",
    "PHP": "PHP Laravel Developer",
    "Ruby": "Ruby on Rails Developer",
    "Salesforce": "Salesforce Developer",
    "SAP": "SAP ABAP Developer",
    "Oracle": "Oracle Software Developer",
    "Mainframe": "Mainframe Developer (COBOL)",
    "QA / testing": "Software Engineer in Test",
    "Support": "Software Support Engineer",
    "Embedded": "Embedded Software Engineer",
    "Games": "Games Backend Engineer",
    "Data annotation": "Data Annotation Specialist",
    "Frontend": "Frontend Developer",
}


def test_every_off_stack_entry_has_an_example():
    assert set(OFF_STACK_EXAMPLES) == {label for label, _, _ in OFF_STACK}


@pytest.mark.parametrize(("label", "title"), OFF_STACK_EXAMPLES.items())
def test_off_stack_titles(label, title):
    assert off_stack_term(title, MINE) is not None, label


@pytest.mark.parametrize(
    ("title", "term"),
    [
        ("Dotnet Full Stack Developer", "Dotnet"),
        (".NET and Angular Developer", ".NET"),
        ("Full Stack .Net / React.js", ".Net"),
        ("C# Backend Developer", "C#"),
        ("ASP.NET Developer", "ASP.NET"),
        ("SDET", "SDET"),
        ("QA Engineer", "QA"),
        ("Firmware Engineer", "Firmware"),
        ("UI Developer", "UI Developer"),
        ("Front End Engineer", "Front End"),
        # A database dialect of another stack isn't the candidate's SQL.
        ("Dot Net Developer (C#, SQL Server)", "Dot Net"),
        ("Oracle PL/SQL Developer", "Oracle"),
    ],
)
def test_off_stack_term_as_written(title, term):
    assert off_stack_term(title, MINE) == term


@pytest.mark.parametrize(
    "title",
    [
        "Java Full Stack Developer (Spring Boot + Angular)",  # Java in the title
        "Golang Developer - Gaming Platform",
        "Python Developer (Android SDK tooling)",
        "Backend Developer",
        "Software Development Engineer",
        "Full Stack Developer (Frontend heavy)",  # not frontend-only
        "Backend + Frontend Engineer",
        "SRE",
        "Business Analyst - Contest Platform",  # "Test" only as a whole word
    ],
)
def test_titles_that_are_not_another_stack(title):
    assert off_stack_term(title, MINE) is None


def test_without_skills_any_off_stack_title_is_another_stack():
    assert off_stack_term("Java Android Developer", ()) == "Android"
    assert off_stack_term("Java Android Developer", ("Java",)) is None


# ---------------------------------------------------------------------------
# Senior


@pytest.mark.parametrize(
    ("title", "term"),
    [
        ("Senior Java Developer", "Senior"),
        ("Sr. Software Engineer", "Sr."),
        ("Sr Backend Developer", "Sr"),
        ("Technical Lead / Senior Full Stack", "Lead"),
        ("Team Lead - Backend", "Lead"),
        ("Tech Lead (Golang)", "Lead"),
        ("Lead Software Engineer", "Lead"),
        ("Principal Engineer", "Principal"),
        ("Staff Software Engineer", "Staff"),
        ("Solution Architect", "Architect"),
        ("Engineering Manager", "Manager"),
        ("Head of Engineering", "Head"),
        ("Director, Platform", "Director"),
        ("VP Engineering", "VP"),
        ("SDE 3", "SDE 3"),
        ("SDE III - Backend", "SDE III"),
        ("SDE-3", "SDE-3"),
        ("SDE3", "SDE3"),
        ("Software Engineer III", "Engineer III"),
        ("Level 3 Software Engineer", "Level 3"),
        ("Backend Engineer (L4)", "L4"),
    ],
)
def test_senior_titles(title, term):
    assert senior_term(title) == term


@pytest.mark.parametrize(
    "title",
    [
        "SDE 2",
        "SDE-II (Backend)",
        "Software Engineer II",
        "Software Engineer 2, API Ops",
        "Software Engineer 3+ years",
        "Software Engineer 3-5 Yrs",
        "Backend Developer - 3 years",
        "Software Developer 3.5 years",
        "Software Engineer- 2- 4years- AI Native",
        "Leading fintech: Backend Developer",
        "Headless CMS Developer",
        "SRE",
        "Member of Technical Staff",
        "Backend Developer",
    ],
)
def test_titles_that_are_not_senior(title):
    assert senior_term(title) is None


@pytest.mark.parametrize(
    "line",
    [
        "I'm the hiring manager for this SDE1 role",
        "Talent Acquisition Manager | hiring SDE2",
        "TA Lead hiring Backend Developers",
        "Head over to the form to apply for Java Developer",
        "Heads up: hiring SDE1",
        "You will lead the payments backend as a Java Developer",
        "Work with senior engineers as an SDE1",
    ],
)
def test_post_phrases_that_are_not_the_roles_level(line):
    assert senior_term(line) is None


# ---------------------------------------------------------------------------
# Skip list


def test_skip_list_matching():
    assert skip_list(None) == () and skip_list("") == ()
    assert skip_list(SKIP_COMPANIES) == ("Jobgether", "Crossing Hurdles", "Scoutit", "Applicantz", "CodeRound AI")
    assert skip_listed(["Jobgether"], SKIP) == "Jobgether"
    assert skip_listed(["SCOUTIT TECHNOLOGIES PVT LTD"], SKIP) == "SCOUTIT TECHNOLOGIES PVT LTD"  # contains, any case
    assert skip_listed(["coderound ai"], SKIP) == "coderound ai"
    assert skip_listed([None, "", "Acme", "Crossing Hurdles"], SKIP) == "Crossing Hurdles"
    assert skip_listed(["Acme Software"], SKIP) is None
    assert skip_listed(["Crossing"], SKIP) is None  # the company must contain the entry, not the reverse
    assert skip_listed(["Jobgether"], ()) is None
    assert skip_listed(["Jobgether"], ["", " "]) is None


# ---------------------------------------------------------------------------
# Skills


def test_two_skills_with_aliases():
    assert matching_skills(MINE, ["Go", "Microservices"]) == ["Golang"]  # a key-skill tag counts as written
    assert matching_skills(MINE, [], "Build services in Go and Java.") == ["Java", "Golang"]
    assert matching_skills(MINE, [], "Experience with Java, Go, Python") == ["Java", "Golang", "Python"]
    assert matching_skills(MINE, [], "Backend (Go) with MySQL and K8s") == ["Golang", "SQL", "Kubernetes"]
    assert matching_skills(MINE, ["Java"], "Kafka pipelines") == ["Java", "Apache Kafka"]
    assert matching_skills(MINE, [], "Go Developer") == ["Golang"]


@pytest.mark.parametrize(
    "text",
    ["We go live in May with Java.", "Own the go-to-market for Java tools.", "Go to our careers page. Java.",
     "Go/No-Go decisions on Java releases.", "Let's go! Java."],
)
def test_go_in_running_text_is_not_golang(text):
    assert matching_skills(MINE, [], text) == ["Java"]


def test_too_few_skills_reasons():
    where = "the job's title or description"
    assert too_few_skills(["Java", "Redis"], where, MINE) is None
    assert too_few_skills(["Java"], where, MINE) == (
        "Only 1 of your skills (Java) is in the job's title or description; at least 2 are needed."
    )
    assert too_few_skills([], where, MINE) == "None of your skills are in the job's title or description."
    assert too_few_skills(["Java"], where, ("Java",)) is None  # a one-skill profile needs one


# ---------------------------------------------------------------------------
# Together


def test_title_reason_order_and_wording():
    assert title_reason("Senior .NET Developer", ["Scoutit"], skills=MINE, skip_companies=SKIP) == (
        "company", "Scoutit is on your skip list."
    )
    assert title_reason("Senior .NET Developer", ["Acme"], skills=MINE, skip_companies=SKIP) == (
        "stack", 'Another stack: the title says ".NET" and names none of your skills.'
    )
    assert title_reason("Senior Java Developer", ["Acme"], skills=MINE, skip_companies=SKIP) == (
        "senior", 'Too senior: the title says "Senior".'
    )
    assert title_reason("SDE 2", ["Acme"], skills=MINE, skip_companies=SKIP) is None


def test_search_left_out_uses_the_prefs():
    assert search_left_out(FIT_PREFS, "Backend Developer", "Jobgether") == "company"
    assert search_left_out(PREFS, "Backend Developer", "Jobgether") is None  # no skip list
    assert search_left_out(FIT_PREFS, "Android SDE 2", "Acme") == "stack"
    assert search_left_out(FIT_PREFS, "Senior Backend Engineer", "Acme") == "senior"
    assert search_left_out(FIT_PREFS, "Java Full Stack Developer (Spring Boot + Angular)", "Acme") is None


def test_left_out_note():
    assert left_out_note({}) is None and left_out_note({key: 0 for key in LEFT_OUT}) is None
    assert left_out_note({"stack": 7, "senior": 5}) == "12 left out: 7 another stack, 5 senior."
    assert left_out_note({"company": 1, "senior": 2}) == "3 left out: 2 senior, 1 on your skip list."


def test_profile_fields_are_loaded(session):
    make_field(session, key="search_experience_order", value="1+ years", position=1)
    make_field(session, key="search_roles", value="Backend Developer", position=2)
    make_field(session, key="search_locations", value="Remote", position=3)
    make_field(session, key="skills", value="Java, Golang", position=4)
    assert load_prefs(session).skip_companies == ()  # no skip list field: nothing skipped
    assert naukri_rules(session).skip_companies == () and linkedin_rules(session).skip_companies == ()

    make_field(session, key="search_skip_companies", value="Jobgether, Scoutit", position=5)
    prefs = load_prefs(session)
    assert (prefs.skills, prefs.skip_companies) == (("Java", "Golang"), ("Jobgether", "Scoutit"))
    rules = naukri_rules(session)
    assert (rules.skills, rules.skip_companies) == (("Java", "Golang"), ("Jobgether", "Scoutit"))
    assert linkedin_rules(session) == rules
