import json
from datetime import timedelta

from app.models import ApplyType, Platform
from app.sources.naukri.parse import (
    account_name,
    block_reason,
    parse_job_page_apply,
    parse_search,
    parse_search_api,
    parse_search_html,
)
from app.sources.naukri.scanner import NaukriJobsScanner, search_url
from tests.fakes import FakeSession, api, page
from tests.scan_helpers import PREFS, epoch_ms, make_ctx, ten_am_today

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


def test_search_url():
    roles = ("Software Development Engineer", "Golang Developer")
    assert search_url(roles, "Bengaluru") == (
        "https://www.naukri.com/software-development-engineer-golang-developer-jobs-in-bengaluru"
        "?k=software%20development%20engineer%2C%20golang%20developer&l=bengaluru&sort=f"
    )
    assert search_url(roles, "Remote", page=2) == (
        "https://www.naukri.com/software-development-engineer-golang-developer-jobs-2"
        "?k=software%20development%20engineer%2C%20golang%20developer&wfhType=2&sort=f"
    )


# ---------------------------------------------------------------------------
# Scanner


def scan(client, fake_browser, *, known=(), max_loads=20):
    ctx = make_ctx(client.app, NOW, max_loads=max_loads)
    session = FakeSession(fake_browser, None, True)
    return NaukriJobsScanner().scan(session, ctx, known=set(known))


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
    assert report.failure is None and report.page_loads == 2  # a job from yesterday ends each search
    by_id = {c.external_id: c for c in report.candidates}
    assert set(by_id) == {"1", "2", "9"}

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


def test_scan_pages_until_known_or_older(client, fake_browser):
    first_page = [nk_job(str(100 + i), "Backend Developer") for i in range(20)]
    second_page = [nk_job(str(200 + i), "Java Developer") for i in range(20)]
    fake_browser.routes = [
        search_page("-jobs-in-bengaluru-2?", nk_json(*second_page)),
        search_page("-jobs-in-bengaluru-3?", nk_json(nk_job("300", "Java Developer"))),
        search_page("-jobs-in-bengaluru?", nk_json(*first_page)),
        search_page("wfhType=2", nk_json()),
    ]
    # Page 2 holds only known jobs, so page 3 isn't loaded.
    report = scan(client, fake_browser, known=[str(200 + i) for i in range(20)])
    visited = fake_browser.visited
    assert any("-jobs-in-bengaluru-2?" in url for url in visited)
    assert not any("-jobs-in-bengaluru-3?" in url for url in visited)
    assert len(report.candidates) == 40 and report.page_loads == 3


def test_scan_respects_page_budget(client, fake_browser):
    fake_browser.routes = [search_page("naukri.com", nk_json(*[nk_job(str(i), "Backend Developer") for i in range(20)]))]
    report = scan(client, fake_browser, max_loads=4)
    assert report.page_loads == 4
    assert report.notes == ["Stopped at the 4-page limit; some of today's jobs may be missed."]
    # The budget is split over the locations.
    assert sum("wfhType=2" in url for url in fake_browser.visited) == 2


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
    assert outcome.message == "Naukri isn't connected, so the search ran without a login; apply types come from the search results."
    assert all(profile is None for profile, _ in fake_browser.opened)


def test_run_when_connected_still_searches_as_guest(client, fake_browser, settings):
    connect_naukri(client, settings)
    fake_browser.routes = [search_page("naukri.com", nk_json(nk_job("1", "Backend Developer")))]
    outcome = NaukriJobsScanner().run(make_ctx(client.app, NOW))
    assert (outcome.status, outcome.found, outcome.message) == ("ok", 1, None)
    assert fake_browser.opened == [(None, True)]  # the results said the apply type: no login needed


def test_html_fallback_confirms_apply_type_with_the_login(client, fake_browser, settings):
    connect_naukri(client, settings)
    fake_browser.routes = [
        ("job-listings-forward-deployed", page("https://www.naukri.com/job", '<button id="company-site-button">Apply on company site</button>')),
        ("-jobs-in-bengaluru?", page("https://www.naukri.com/srp", CARD_HTML)),
        ("wfhType=2", page("https://www.naukri.com/srp", "<p>No jobs</p>")),
    ]
    outcome = NaukriJobsScanner().run(make_ctx(client.app, NOW))
    assert (outcome.status, outcome.found, outcome.message) == ("ok", 1, None)
    assert fake_browser.opened == [(None, True), (settings.browser_profiles_dir / "naukri", True)]
    assert client.get("/api/jobs").json()["items"][0]["apply_type"] == "company_site"
    assert client.get("/api/connections").json()["items"][1]["status"] == "connected"


def test_html_fallback_login_wall_disconnects(client, fake_browser, settings):
    connect_naukri(client, settings)
    fake_browser.routes = [
        ("job-listings-forward-deployed", page("https://www.naukri.com/nlogin/login?URL=job")),
        ("-jobs-in-bengaluru?", page("https://www.naukri.com/srp", CARD_HTML)),
        ("wfhType=2", page("https://www.naukri.com/srp", "<p>No jobs</p>")),
    ]
    outcome = NaukriJobsScanner().run(make_ctx(client.app, NOW))
    assert outcome.status == "ok" and outcome.found == 1
    assert outcome.message == "1 job(s) are listed as company-site jobs without confirming the apply button."
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
    assert "-jobs-in-bengaluru?" in fake_browser.visited[0] and "wfhType=2" in fake_browser.visited[1]
