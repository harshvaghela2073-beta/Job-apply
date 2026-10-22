import json
from datetime import timedelta

import pytest

from app.models import ApplyType, Platform
from app.scanner import base
from app.scanner.matching import SearchPrefs
from app.sources.naukri import selectors as S
from app.sources.naukri.parse import (
    account_name,
    block_reason,
    parse_job_page_apply,
    parse_recommended,
    parse_search,
    parse_search_api,
    parse_search_html,
)
from app.sources.naukri.scanner import (
    NaukriJobsScanner,
    Search,
    salary_bands,
    salary_range,
    search_plan,
    search_url,
)
from tests.factories import make_field
from tests.fakes import FakeSession, api, page
from tests.scan_helpers import FIT_PREFS, PREFS, epoch_ms, make_ctx, ten_am_today

NOW = ten_am_today()


def nk_job(job_id, title, *, posted=NOW - timedelta(hours=1), exp="1-4 Yrs", min_exp="1",
           location="Bengaluru", company_apply=False, redirect=None, label="Just Now", company="Acme"):
    return {
        "jobId": job_id,
        "title": title,
        "companyName": company,
        "jdURL": f"/job-listings-{title.lower().replace(' ', '-')}-{job_id}",
        "placeholders": [
            {"type": "experience", "label": exp},
            {"type": "salary", "label": "Not disclosed"},
            {"type": "location", "label": location},
        ],
        "footerPlaceholderLabel": label,
        "createdDate": epoch_ms(posted),
        "experienceText": exp,
        "minimumExperience": min_exp,
        "maximumExperience": "4",
        "companyApplyJob": company_apply,
        "applyRedirectUrl": redirect,
        "todaysJob": label == "Just Now",
        "tagsAndSkills": "Java,Spring Boot,Microservices,Kafka,AWS,Docker,Kubernetes",
        "jobDescription": "Build <b>payments</b> APIs.<br>Own services &amp; on-call.",
    }


def nk_json(*jobs, logged_in=False, total=None):
    return json.dumps({"noOfJobs": total if total is not None else len(jobs), "jobDetails": list(jobs),
                       "isLoggedIn": logged_in})


SEARCH_API_URL = "https://www.naukri.com/jobapi/v3/search?noOfResults=20&pageNo=1"


def search_page(url_fragment, body, html="<html><body>loading</body></html>"):
    return url_fragment, lambda url: page(url, html, captured=[api(SEARCH_API_URL, body)])


CARD_HTML = """
<div class="srp-jobtuple-wrapper" data-job-id="131026028764">
  <div class="cust-job-tuple">
    <h2><a class="title" title="Forward Deployed Software Engineer"
       href="https://www.naukri.com/job-listings-forward-deployed-software-engineer-hack-and-grow-bengaluru-1-to-3-years-131026028764?src=srp">Forward Deployed Software Engineer</a></h2>
    <div class="row2"><span class="comp-dtls-wrap"><a class="comp-name" href="/hack-and-grow-jobs-careers-1">Hack And Grow</a></span></div>
    <div class="row3"><div class="job-details">
      <span class="exp-wrap"><span class="expwdth">1-3 Yrs</span></span>
      <span class="loc-wrap"><span class="locWdth">Bengaluru</span></span>
    </div></div>
    <div class="row4"><span class="job-desc">Work with customers on Golang services.</span></div>
    <div class="row6"><span class="job-post-day">Just now</span></div>
  </div>
</div>
<div class="srp-jobtuple-wrapper" data-job-id="121026929072">
  <div class="cust-job-tuple">
    <h2><a class="title" href="/job-listings-software-development-engineer-accenture-bengaluru-1-to-4-years-121026929072">Software Development Engineer</a></h2>
    <a class="comp-name">Accenture</a>
    <span class="expwdth">1-4 Yrs</span><span class="locWdth">Bengaluru</span>
    <span class="job-post-day">1 Day Ago</span>
  </div>
</div>
"""


# ---------------------------------------------------------------------------
# Parsers


def test_parse_search_api():
    body = nk_json(
        nk_job("101", "Backend Developer"),
        nk_job("102", "SDE -2", company_apply=True, redirect="https://careers.example.com/2", exp="0-5 Yrs", min_exp="0"),
        logged_in=True,
        total=580,
    )
    result = parse_search_api(body)
    assert result.total == 580 and result.logged_in is True and result.from_api
    first, second = result.jobs
    assert first.job_id == "101" and first.title == "Backend Developer" and first.company == "Acme"
    assert first.url == "https://www.naukri.com/job-listings-backend-developer-101"
    assert first.location == "Bengaluru" and first.experience == "1-4 Yrs" and first.min_experience == 1
    assert first.company_apply is False and first.posted_label == "Just Now"
    assert abs(first.created_at - (NOW - timedelta(hours=1))) < timedelta(seconds=1)
    assert first.description == "Build payments APIs. Own services & on-call."
    assert second.company_apply is True and second.apply_redirect_url == "https://careers.example.com/2"
    assert parse_search_api("not json") is None
    assert parse_search_api(json.dumps({"other": 1})) is None


def test_parse_search_html_card():
    result = parse_search_html(CARD_HTML)
    assert not result.from_api
    job, older = result.jobs
    assert job.job_id == "131026028764"
    assert job.title == "Forward Deployed Software Engineer"
    assert job.company == "Hack And Grow"
    assert job.url.endswith("-131026028764")  # query string dropped
    assert (job.experience, job.min_experience, job.location) == ("1-3 Yrs", 1, "Bengaluru")
    assert job.posted_label == "Just now" and job.company_apply is None
    assert job.description == "Work with customers on Golang services."
    assert older.url == "https://www.naukri.com/job-listings-software-development-engineer-accenture-bengaluru-1-to-4-years-121026929072"
    assert older.posted_label == "1 Day Ago"


def test_parse_search_prefers_captured_json():
    result = page("https://www.naukri.com/x", CARD_HTML, captured=[api(SEARCH_API_URL, nk_json(nk_job("7", "Java Developer")))])
    assert [j.job_id for j in parse_search(result).jobs] == ["7"]
    no_json = page("https://www.naukri.com/x", CARD_HTML)
    assert len(parse_search(no_json).jobs) == 2


def test_job_page_apply_buttons():
    company = '<div><button id="company-site-button" class="styles_company-site-button__a">Apply on company site</button></div>'
    company_text_only = '<div><button class="x">Apply on company site</button></div>'
    one_click = '<div><button id="apply-button" class="styles_apply-button__uJI3A apply-button">Apply</button></div>'
    applied = '<div><span id="already-applied" class="styles_already-applied__x">Applied</span></div>'
    logged_out = '<div><button id="login-apply-button">Login to apply</button></div>'
    assert parse_job_page_apply(company) == ApplyType.company_site
    assert parse_job_page_apply(company_text_only) == ApplyType.company_site
    assert parse_job_page_apply(one_click) == ApplyType.one_click
    assert parse_job_page_apply(applied) == ApplyType.one_click
    assert parse_job_page_apply(logged_out) is None


def test_block_reason():
    assert block_reason(page("https://www.naukri.com/x", "<h1>Access Denied</h1><p>You don't have permission</p>")) \
        == "Naukri showed a block page ('access denied')"
    assert block_reason(page("https://www.naukri.com/x", "", status=403)) == "Naukri returned HTTP 403"
    assert block_reason(page("https://www.naukri.com/x", "<script>captcha()</script><p>No jobs</p>")) is None


def test_account_name():
    assert account_name('<div class="info__heading" title="Harsh">Harsh Vaghela</div>') == "Harsh Vaghela"
    assert account_name("<div>nothing</div>") is None


# The candidate's targets of 2026-10-21 (synthetic profile values).
CANDIDATE_ROLES = "SDE, Software Development Engineer, Backend Engineer, Java Developer, Full Stack Engineer, SDE2, SDE1"
CANDIDATE_PREFS = SearchPrefs.from_values("1+ years, 2+ years", CANDIDATE_ROLES, "Bengaluru")


LIVE_FILTERS = (S.EXPERIENCE_PARAM, S.SALARY_PARAM, S.FRESHNESS_PARAM)  # before any test patches them


@pytest.fixture(autouse=True)
def _filters_off(monkeypatch):
    """These tests describe the search without Naukri's URL filters; ``naukri_filters`` turns them on."""
    monkeypatch.setattr(S, "EXPERIENCE_PARAM", None)
    monkeypatch.setattr(S, "SALARY_PARAM", None)
    monkeypatch.setattr(S, "FRESHNESS_PARAM", None)


@pytest.fixture
def naukri_filters(monkeypatch):
    """Naukri's experience, salary and freshness filters switched on (they need a live check first)."""
    monkeypatch.setattr(S, "EXPERIENCE_PARAM", "experience")
    monkeypatch.setattr(S, "SALARY_PARAM", "ctcFilter")
    monkeypatch.setattr(S, "FRESHNESS_PARAM", ("jobAge", "1"))


def test_live_filters():
    """Confirmed live on 2026-10-21 (14 matches instead of 1-3; no job asking 3+ years). The salary band
    stays off: it would hide every job without a stated salary."""
    assert LIVE_FILTERS == ("experience", None, ("jobAge", "1"))


def test_search_url_without_filters():
    """Without the filters only the role, city, sort and page are sent."""
    assert (S.EXPERIENCE_PARAM, S.SALARY_PARAM, S.FRESHNESS_PARAM) == (None, None, None)
    expected = "https://www.naukri.com/backend-engineer-jobs-in-bengaluru?k=backend%20engineer&l=bengaluru&sort=f"
    assert search_url("Backend Engineer", "Bengaluru") == expected
    assert search_url("Backend Engineer", "Bengaluru", experience=1, salary=(16.0, 18.0)) == expected
    assert search_url("SDE2", "Remote", page=2) == "https://www.naukri.com/sde2-jobs-2?k=sde2&wfhType=2&sort=f"


def test_search_url_with_filters(naukri_filters):
    assert search_url("SDE", "Bengaluru", experience=1, salary=(16.0, 18.0)) == (
        "https://www.naukri.com/sde-jobs-in-bengaluru?k=sde&l=bengaluru&experience=1&ctcFilter=15to25&jobAge=1&sort=f"
    )
    assert search_url("Full Stack Engineer", "Bengaluru", 3, experience=2, salary=(14.0, 18.0)) == (
        "https://www.naukri.com/full-stack-engineer-jobs-in-bengaluru-3?k=full%20stack%20engineer&l=bengaluru"
        "&experience=2&ctcFilter=10to15&ctcFilter=15to25&jobAge=1&sort=f"
    )
    assert search_url("Java Developer", "Remote") == "https://www.naukri.com/java-developer-jobs?k=java%20developer&wfhType=2&jobAge=1&sort=f"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("16-18 LPA", (16.0, 18.0)), ("16 LPA", (16.0, 16.0)), ("16 to 18 Lakhs", (16.0, 18.0)),
     ("18-16 LPA", (18.0, 18.0)), ("", None), (None, None), ("negotiable", None)],
)
def test_salary_range(value, expected):
    assert salary_range(value) == expected


def test_salary_bands():
    assert salary_bands((16.0, 18.0)) == ["15to25"]
    assert salary_bands((15.0, 15.0)) == ["15to25"]
    assert salary_bands((14.0, 30.0)) == ["10to15", "15to25", "25to50"]
    assert salary_bands((150.0, 200.0)) == []


def test_search_plan_one_search_per_role_in_priority_order():
    roles = CANDIDATE_PREFS.roles
    assert search_plan(CANDIDATE_PREFS) == [Search(role, "Bengaluru") for role in roles]
    two_cities = SearchPrefs.from_values("1+ years", "SDE, Java Developer", "Bengaluru, Remote")
    assert search_plan(two_cities) == [
        Search("SDE", "Bengaluru"), Search("SDE", "Remote"), Search("Java Developer", "Bengaluru"), Search("Java Developer", "Remote"),
    ]


def test_search_plan_per_experience_level_with_the_filter(naukri_filters):
    roles = CANDIDATE_PREFS.roles
    assert search_plan(CANDIDATE_PREFS) == (
        [Search(role, "Bengaluru", 1) for role in roles] + [Search(role, "Bengaluru", 2) for role in roles]
    )


# ---------------------------------------------------------------------------
# Scanner


def scan(client, fake_browser, *, known=(), max_loads=20, prefs=PREFS, reserve=0):
    ctx = make_ctx(client.app, NOW, max_loads=max_loads, prefs=prefs)
    session = FakeSession(fake_browser, None, True)
    return NaukriJobsScanner().scan(session, ctx, known=set(known), reserve=reserve)


def test_scan_filters_and_ranks(client, fake_browser):
    yesterday = NOW - timedelta(hours=20)
    fake_browser.routes = [
        search_page("-jobs-in-bengaluru?", nk_json(
            nk_job("1", "Backend Developer"),
            nk_job("2", "SDE -2", company_apply=True, redirect="https://careers.example.com/2", exp="2-5 Yrs", min_exp="2"),
            nk_job("3", "Data Scientist"),
            nk_job("4", "Software Engineer Intern", exp="0-1 Yrs", min_exp="0"),
            nk_job("5", "Java Developer", exp="5-8 Yrs", min_exp="5"),
            nk_job("6", "Backend Developer", location="Hyderabad"),
            nk_job("7", "Staff Software Engineer", exp="0-3 Yrs", min_exp="0"),
            nk_job("8", "Golang Developer", posted=yesterday, label="1 Day Ago"),
            total=580,
        )),
        search_page("wfhType=2", nk_json(nk_job("9", "Golang Developer", location="Remote", exp="3-6 Yrs", min_exp="3"))),
    ]
    report = scan(client, fake_browser)
    # One page per search: a job from yesterday or a short page ends each one.
    assert report.failure is None and report.page_loads == len(search_plan(PREFS)) == 16
    by_id = {c.external_id: c for c in report.candidates}
    assert set(by_id) == {"1", "2", "9"} and len(report.candidates) == 3  # each job once

    backend = by_id["1"]
    assert backend.platform == Platform.naukri and backend.apply_type == ApplyType.one_click
    assert backend.apply_url is None
    assert (backend.matched_role, backend.experience, backend.priority) == ("Backend Developer", "1-4 Yrs", 40)
    assert backend.short_description.startswith("Java, Spring Boot, Microservices, Kafka, AWS, Docker · Build payments")
    assert backend.url == "https://www.naukri.com/job-listings-backend-developer-1"

    sde2 = by_id["2"]
    assert sde2.apply_type == ApplyType.company_site and sde2.apply_url == "https://careers.example.com/2"
    assert (sde2.matched_role, sde2.priority) == ("SDE2", 120)

    remote = by_id["9"]
    assert remote.location == "Remote" and remote.priority == 2 * 100 + 5 * 10 + 1
    assert all(c.posted_at is not None for c in report.candidates)
    assert report.summary() == [
        "Search: 9 jobs read; 3 matched.", "1 left out: 1 senior.",
        "Today's jobs not matched: 2 not one of your roles, 1 more experience than yours, 1 outside Bengaluru.",
    ]


def test_scan_runs_one_search_per_role_in_priority_order(client, fake_browser, naukri_filters):
    """With Naukri's filters on: every role at 1+ years first, then every role at 2+ years."""
    fake_browser.routes = [search_page("naukri.com", nk_json(nk_job("1", "Backend Engineer")))]
    report = scan(client, fake_browser, prefs=CANDIDATE_PREFS)
    slugs = [_slug_of(url) for url in fake_browser.visited]
    roles = ["sde", "software-development-engineer", "backend-engineer", "java-developer", "full-stack-engineer", "sde2", "sde1"]
    assert slugs == [(role, "1") for role in roles] + [(role, "2") for role in roles]
    assert all("&jobAge=1&sort=f" in url and "l=bengaluru" in url for url in fake_browser.visited)
    assert [c.external_id for c in report.candidates] == ["1"] and report.candidates[0].matched_role == "Backend Engineer"
    assert report.summary() == ["Search: 1 jobs read; 1 matched."]


def _slug_of(url: str) -> tuple[str, str]:
    path, _, query = url.removeprefix("https://www.naukri.com/").partition("?")
    experience = dict(part.split("=") for part in query.split("&")).get("experience", "")
    return path.removesuffix("-jobs-in-bengaluru"), experience


def test_scan_reads_the_salary_band_from_the_profile(client, fake_browser, naukri_filters):
    from sqlmodel import Session

    with Session(client.app.state.engine) as session:
        make_field(session, key="search_salary_lpa", label="Target salary", section="Job search", value="16-18 LPA")
    fake_browser.routes = [search_page("naukri.com", nk_json())]
    scan(client, fake_browser, prefs=SearchPrefs.from_values("1+ years", "SDE", "Bengaluru"))
    assert fake_browser.visited == [
        "https://www.naukri.com/sde-jobs-in-bengaluru?k=sde&l=bengaluru&experience=1&ctcFilter=15to25&jobAge=1&sort=f"
    ]


def test_scan_leaves_out_other_stacks_senior_titles_and_skip_listed_companies(client, fake_browser):
    """Job fit (2026-10-15): such jobs aren't stored, and the run message counts them (each job once)."""
    dotnet = nk_job("11", "Dotnet Full Stack Developer", location="Bengaluru, Remote")
    fake_browser.routes = [
        search_page("-jobs-in-bengaluru?", nk_json(
            dotnet,
            nk_job("12", "Java Full Stack Developer (Spring Boot + Angular)"),
            nk_job("13", "Senior Java Developer", exp="2-6 Yrs", min_exp="2"),
            nk_job("14", "SDE 2"),
            nk_job("15", "Backend Developer", company="Scoutit"),
            nk_job("16", "Software Support Engineer"),
            nk_job("17", "Senior Backend Developer", exp="6-10 Yrs", min_exp="6"),  # too much experience first
        )),
        search_page("wfhType=2", nk_json(dotnet)),
    ]
    report = scan(client, fake_browser, prefs=FIT_PREFS)
    assert {c.external_id for c in report.candidates} == {"12", "14"}
    assert report.notes == []
    assert report.summary() == [
        "Search: 7 jobs read; 2 matched.", "4 left out: 2 another stack, 1 senior, 1 on your skip list.",
        "Today's jobs not matched: 1 more experience than yours.",
    ]


def test_scan_pages_until_known_or_older(client, fake_browser):
    first_page = [nk_job(str(100 + i), "Backend Developer") for i in range(20)]
    second_page = [nk_job(str(200 + i), "Backend Developer") for i in range(20)]
    fake_browser.routes = [
        search_page("-jobs-in-bengaluru-2?", nk_json(*second_page)),
        search_page("-jobs-in-bengaluru-3?", nk_json(nk_job("300", "Backend Developer"))),
        search_page("-jobs-in-bengaluru?", nk_json(*first_page)),
    ]
    # Page 2 holds only known jobs, so page 3 isn't loaded.
    prefs = SearchPrefs.from_values("1+ years", "Backend Developer", "Bengaluru")
    report = scan(client, fake_browser, known=[str(200 + i) for i in range(20)], prefs=prefs)
    visited = fake_browser.visited
    assert any("-jobs-in-bengaluru-2?" in url for url in visited)
    assert not any("-jobs-in-bengaluru-3?" in url for url in visited)
    assert len(report.candidates) == 40 and report.page_loads == 2


def test_scan_splits_the_page_budget_over_the_searches(client, fake_browser):
    pages = iter(range(1000))

    def full_page(url):
        return page(url, "", captured=[api(SEARCH_API_URL, nk_json(*[nk_job(str(next(pages)), "Backend Developer") for _ in range(20)]))])

    fake_browser.routes = [("naukri.com", full_page)]
    prefs = SearchPrefs.from_values("1+ years", "Backend Developer, Java Developer", "Bengaluru")
    report = scan(client, fake_browser, max_loads=6, prefs=prefs)
    assert report.page_loads == 6
    assert report.notes == ["Stopped at the 6-page limit; some of today's jobs may be missed."]
    assert [url.split("?")[0].rsplit("/", 1)[1] for url in fake_browser.visited] == [
        "backend-developer-jobs-in-bengaluru", "backend-developer-jobs-in-bengaluru-2", "backend-developer-jobs-in-bengaluru-3",
        "java-developer-jobs-in-bengaluru", "java-developer-jobs-in-bengaluru-2", "java-developer-jobs-in-bengaluru-3",
    ]

    # A load kept for the recommended list comes off the searches' share.
    fake_browser.visited.clear()
    report = scan(client, fake_browser, max_loads=6, prefs=prefs, reserve=1)
    assert report.page_loads == 5 and sum("backend-developer" in url for url in fake_browser.visited) == 3


def test_scan_stops_on_block(client, fake_browser):
    fake_browser.routes = [("naukri.com", page("https://www.naukri.com/x", "<h1>Access Denied</h1>"))]
    report = scan(client, fake_browser)
    assert report.failure == "Naukri showed a block page ('access denied'); stopped."
    assert report.page_loads == 1 and report.candidates == []


def connect_naukri(client, settings):
    from sqlmodel import Session

    from app.models import Connection, ConnectionStatus

    (settings.browser_profiles_dir / "naukri").mkdir(parents=True)
    with Session(client.app.state.engine) as session:
        row = session.get(Connection, Platform.naukri)
        row.status = ConnectionStatus.connected
        session.add(row)
        session.commit()


def test_run_searches_as_guest_and_says_when_not_connected(client, fake_browser):
    fake_browser.routes = [search_page("naukri.com", nk_json(nk_job("1", "Backend Developer")))]
    outcome = NaukriJobsScanner().run(make_ctx(client.app, NOW))
    assert (outcome.status, outcome.found, outcome.new) == ("ok", 1, 1)
    assert outcome.message == (
        "Search: 1 jobs read; 1 matched. "
        "Naukri isn't connected, so the search ran without a login; apply types come from the search results."
    )
    assert all(profile is None for profile, _ in fake_browser.opened)
    assert not any("recommendedjobs" in url for url in fake_browser.visited)


def test_run_when_connected_searches_as_guest_and_reads_recommended_jobs(client, fake_browser, settings):
    connect_naukri(client, settings)
    fake_browser.routes = [search_page("naukri.com", nk_json(nk_job("1", "Backend Developer")))]
    outcome = NaukriJobsScanner().run(make_ctx(client.app, NOW))
    assert (outcome.status, outcome.found, outcome.message) == ("ok", 1, "Search: 1 jobs read; Recommended: 1; 1 matched.")
    # The search as a guest, then the recommended list with the saved login.
    assert fake_browser.opened == [(None, True), (settings.browser_profiles_dir / "naukri", True)]
    assert fake_browser.visited[-1] == "https://www.naukri.com/mnjuser/recommendedjobs"


def test_html_fallback_confirms_apply_type_with_the_login(client, fake_browser, settings):
    connect_naukri(client, settings)
    fake_browser.routes = [
        ("job-listings-forward-deployed", page("https://www.naukri.com/job", '<button id="company-site-button">Apply on company site</button>')),
        ("-jobs-in-bengaluru?", page("https://www.naukri.com/srp", CARD_HTML)),
        ("wfhType=2", page("https://www.naukri.com/srp", "<p>No jobs</p>")),
        ("recommendedjobs", page(S.RECOMMENDED_URL, CARD_HTML)),
    ]
    outcome = NaukriJobsScanner().run(make_ctx(client.app, NOW))
    assert (outcome.status, outcome.found, outcome.message) == ("ok", 1, "Search: 2 jobs read; Recommended: 2; 1 matched.")
    assert fake_browser.opened == [(None, True), (settings.browser_profiles_dir / "naukri", True)]
    assert client.get("/api/jobs").json()["items"][0]["apply_type"] == "company_site"
    assert client.get("/api/connections").json()["items"][1]["status"] == "connected"


def test_html_fallback_login_wall_disconnects(client, fake_browser, settings):
    connect_naukri(client, settings)
    fake_browser.routes = [
        ("job-listings-forward-deployed", page("https://www.naukri.com/nlogin/login?URL=job")),
        ("-jobs-in-bengaluru?", page("https://www.naukri.com/srp", CARD_HTML)),
        ("wfhType=2", page("https://www.naukri.com/srp", "<p>No jobs</p>")),
        ("recommendedjobs", page(S.RECOMMENDED_URL, CARD_HTML)),
    ]
    outcome = NaukriJobsScanner().run(make_ctx(client.app, NOW))
    assert outcome.status == "ok" and outcome.found == 1
    assert outcome.message == (
        "Search: 2 jobs read; Recommended: 2; 1 matched. "
        "1 job(s) are listed as company-site jobs without confirming the apply button."
    )
    naukri = client.get("/api/connections").json()["items"][1]
    assert naukri["status"] == "disconnected" and "expired" in naukri["message"]


def test_html_fallback_without_login(client, fake_browser):
    fake_browser.routes = [
        ("-jobs-in-bengaluru?", page("https://www.naukri.com/srp", CARD_HTML)),
        ("wfhType=2", page("https://www.naukri.com/srp", "<p>No jobs</p>")),
    ]
    report = scan(client, fake_browser)
    (candidate,) = report.candidates
    assert candidate.apply_type == ApplyType.company_site and report.unconfirmed == [candidate]


def test_prefs_order_is_used(client, fake_browser):
    assert PREFS.locations == ("Bengaluru", "Remote")
    fake_browser.routes = [search_page("naukri.com", nk_json())]
    scan(client, fake_browser)
    assert "software-development-engineer-jobs-in-bengaluru?" in fake_browser.visited[0]
    assert "software-development-engineer-jobs?" in fake_browser.visited[1] and "wfhType=2" in fake_browser.visited[1]
    assert "sde1-jobs-in-bengaluru?" in fake_browser.visited[2]


# ---------------------------------------------------------------------------
# Recommended jobs (2026-10-21)

RECOMMENDED_API_URL = "https://www.naukri.com/jobapi/v2/search/recom-jobs?src=jobsearchDesk"


def recommended_page(*jobs, html="<html><body>loading</body></html>"):
    body = json.dumps({"recommendedJobs": {"jobDetails": list(jobs)}, "noOfJobs": len(jobs)})
    return page(S.RECOMMENDED_URL, html, captured=[api(RECOMMENDED_API_URL, body)])


def test_parse_recommended_reads_job_lists_in_any_captured_json():
    result = recommended_page(nk_job("31", "Backend Engineer"), nk_job("32", "Java Developer"))
    result.captured.insert(0, api("https://www.naukri.com/jobapi/v2/search/recom-jobs/count", "not json"))
    result.captured.append(api(RECOMMENDED_API_URL, json.dumps([{"jobDetails": [nk_job("33", "SDE 2"), nk_job("31", "Backend Engineer")]}])))
    jobs = parse_recommended(result).jobs
    assert [job.job_id for job in jobs] == ["31", "32", "33"]
    assert jobs[0].company == "Acme" and jobs[0].company_apply is False and jobs[0].created_at is not None


def test_parse_recommended_falls_back_to_the_job_cards():
    assert [job.job_id for job in parse_recommended(page(S.RECOMMENDED_URL, CARD_HTML)).jobs] == ["131026028764", "121026929072"]
    assert parse_recommended(page(S.RECOMMENDED_URL, "<p>Nothing here</p>")).jobs == []


def candidate_prefs_ctx(client, **values):
    return make_ctx(client.app, NOW, prefs=SearchPrefs.from_values(
        "1+ years, 2+ years", CANDIDATE_ROLES, "Bengaluru",
        skills="Java, Spring Boot, Golang, Kafka", skip_companies="Scoutit",
    ), **values)


def test_recommended_jobs_use_the_same_rules(client, fake_browser, settings):
    connect_naukri(client, settings)
    yesterday = NOW - timedelta(hours=20)
    fake_browser.routes = [
        ("recommendedjobs", recommended_page(
            nk_job("41", "Back End Engineer"),
            nk_job("42", "Java Developer", posted=yesterday, label="1 Day Ago"),  # not posted today
            nk_job("43", "Senior Backend Engineer", exp="2-5 Yrs", min_exp="2"),  # senior
            nk_job("44", "Full Stack Developer", location="Hyderabad"),  # another city
            nk_job("45", "Data Scientist"),  # not a role
            nk_job("46", "SDE II", company="Scoutit"),  # skip list
            nk_job("47", "Fullstack Engineer", company_apply=True, redirect="https://careers.example.com/47"),
            nk_job("48", "Java Engineer", exp="3-6 Yrs", min_exp="3"),  # more experience than 2+ years
        )),
        search_page("naukri.com", nk_json(nk_job("40", "SDE-1"), nk_job("41", "Back End Engineer"))),
    ]
    outcome = NaukriJobsScanner().run(candidate_prefs_ctx(client))
    assert outcome.status == "ok" and (outcome.found, outcome.new) == (3, 3)
    assert outcome.message == (
        "Search: 2 jobs read; Recommended: 8; 3 matched. 2 left out: 1 senior, 1 on your skip list. "
        "Today's jobs not matched: 1 not one of your roles, 1 more experience than yours, 1 outside Bengaluru."
    )
    jobs = {job["external_id"]: job for job in client.get("/api/jobs").json()["items"]}
    assert set(jobs) == {"40", "41", "47"}
    assert jobs["47"]["apply_type"] == "company_site" and jobs["47"]["matched_role"] == "Full Stack Engineer"
    assert jobs["41"]["matched_role"] == "Backend Engineer" and jobs["40"]["matched_role"] == "SDE1"


def test_recommended_list_gets_a_page_load_when_the_searches_use_the_budget(client, fake_browser, settings):
    connect_naukri(client, settings)
    full = [nk_job(str(500 + i), "Backend Engineer") for i in range(20)]
    fake_browser.routes = [
        ("recommendedjobs", recommended_page(nk_job("61", "Java Developer"))),
        search_page("naukri.com", nk_json(*full)),
    ]
    outcome = NaukriJobsScanner().run(candidate_prefs_ctx(client, max_loads=4))
    assert outcome.page_loads == 4 and fake_browser.visited[-1] == S.RECOMMENDED_URL
    assert outcome.message.startswith("Search: 20 jobs read; Recommended: 1; 21 matched. Stopped at the 4-page limit")


def test_unreadable_recommended_page_is_saved_and_doesnt_fail_the_run(client, fake_browser, settings):
    connect_naukri(client, settings)
    fake_browser.routes = [
        ("recommendedjobs", page(S.RECOMMENDED_URL, "<html><body><div class='newLayout'>Jobs for you</div></body></html>")),
        search_page("naukri.com", nk_json(nk_job("1", "Backend Engineer"))),
    ]
    outcome = NaukriJobsScanner().run(candidate_prefs_ctx(client))
    assert (outcome.status, outcome.found) == ("ok", 1)
    assert outcome.message.startswith("Search: 1 jobs read; Recommended: 0; 1 matched. No recommended jobs could be read.")
    (saved,) = base.DIAGNOSTICS_DIR.glob("naukri_recommended-*.html")
    assert "Jobs for you" in saved.read_text() and f"Page: data/diagnostics/{saved.name}." in outcome.message
    assert any("naukri_recommended-" in shot for shot in fake_browser.screenshots)


def test_recommended_page_errors_dont_fail_the_run(client, fake_browser, settings):
    connect_naukri(client, settings)

    def broken(url):
        raise RuntimeError("net::ERR_CONNECTION_RESET at https://www.naukri.com/mnjuser/recommendedjobs")

    fake_browser.routes = [("recommendedjobs", broken), search_page("naukri.com", nk_json(nk_job("1", "Backend Engineer")))]
    outcome = NaukriJobsScanner().run(candidate_prefs_ctx(client))
    assert (outcome.status, outcome.found) == ("ok", 1)
    assert outcome.message == (
        "Search: 1 jobs read; 1 matched. The recommended jobs didn't load: "
        "net::ERR_CONNECTION_RESET at https://www.naukri.com/mnjuser/recommendedjobs"
    )


def test_recommended_login_wall_disconnects_and_stops(client, fake_browser, settings):
    connect_naukri(client, settings)
    fake_browser.routes = [
        ("recommendedjobs", page("https://www.naukri.com/nlogin/login?URL=/mnjuser/recommendedjobs")),
        ("-jobs-in-bengaluru?", page("https://www.naukri.com/srp", CARD_HTML)),
    ]
    outcome = NaukriJobsScanner().run(candidate_prefs_ctx(client))
    assert outcome.status == "ok" and outcome.found == 1
    assert not any("job-listings" in url for url in fake_browser.visited)  # no job page opened after the wall
    naukri = client.get("/api/connections").json()["items"][1]
    assert naukri["status"] == "disconnected" and "expired" in naukri["message"]


def test_recommended_block_page_stops_the_logged_in_pages(client, fake_browser, settings):
    connect_naukri(client, settings)
    fake_browser.routes = [
        ("recommendedjobs", page(S.RECOMMENDED_URL, "<h1>Access Denied</h1>")),
        ("-jobs-in-bengaluru?", page("https://www.naukri.com/srp", CARD_HTML)),
    ]
    outcome = NaukriJobsScanner().run(candidate_prefs_ctx(client))
    assert outcome.status == "ok" and outcome.found == 1
    assert "Recommended jobs: Naukri showed a block page ('access denied'); stopped." in outcome.message
    assert not any("job-listings" in url for url in fake_browser.visited)
    assert not list(base.DIAGNOSTICS_DIR.glob("naukri_recommended-*"))
