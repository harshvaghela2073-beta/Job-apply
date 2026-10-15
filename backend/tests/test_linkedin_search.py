"""The logged-in LinkedIn job search (``linkedin_jobs``): parsers on small hand-written pages, the
scan with a fake browser, and the hourly interval. No real browser or network."""

import json
from datetime import timedelta
from html import escape
from urllib.parse import parse_qs, urlparse

from sqlmodel import Session

from app.models import ApplyType, Platform, ScanKind, ScanRun, ScanStatus
from app.scanner.matching import SearchPrefs
from app.sources.linkedin.jobs import (
    LinkedInJobsScanner,
    experience_levels,
    keyword_chunks,
    keywords,
    next_search_after,
    page_url,
    search_params,
)
from app.sources.linkedin.parse import parse_search_page, search_block_reason
from tests.fakes import FakeSession, api, page
from tests.scan_helpers import PREFS, connect_linkedin, epoch_ms, make_ctx, ten_am_today

NOW = ten_am_today()
SEARCH = "https://www.linkedin.com/jobs/search/"


# ---------------------------------------------------------------------------
# Fixtures: a logged-in results page


def dom_card(job_id, title, *, company="Acme", location="Bengaluru, Karnataka, India (Hybrid)",
             ago="2 hours ago", date="2026-10-13", easy=True, insight=None):
    footer = f'<li class="job-card-container__footer-item"><time datetime="{date}">{ago}</time></li>' if ago else ""
    if easy:
        footer += '<li class="job-card-container__footer-item"><span>Easy Apply</span></li>'
    insight_html = f'<div class="job-card-container__job-insight-text">{insight}</div>' if insight else ""
    return f"""
<li class="scaffold-layout__list-item" data-occludable-job-id="{job_id}">
  <div class="job-card-container job-card-container--clickable" data-job-id="{job_id}">
    <div class="artdeco-entity-lockup">
      <div class="artdeco-entity-lockup__title">
        <a class="job-card-list__title--link" href="/jobs/view/{job_id}/?eBP=x&amp;trk=flagship3">
          <span aria-hidden="true"><strong>{title}</strong></span>
          <span class="visually-hidden">{title} with verification</span>
        </a>
      </div>
      <div class="artdeco-entity-lockup__subtitle"><span>{company}</span></div>
      <div class="artdeco-entity-lockup__caption"><ul><li><span>{location}</span></li></ul></div>
    </div>
    {insight_html}
    <ul class="job-card-list__footer-wrapper">{footer}</ul>
  </div>
</li>"""


def placeholder(job_id):
    """A card the lazy list hasn't rendered yet."""
    return f'<li class="scaffold-layout__list-item" data-occludable-job-id="{job_id}"></li>'


def json_card(job_id, title, *, company="Acme", location="Bengaluru, Karnataka, India (On-site)",
              listed=None, easy=True):
    footer = [{"$type": "com.linkedin.voyager.dash.jobs.JobPostingFooterItem", "type": "LISTED_DATE",
               "timeAt": epoch_ms(listed or NOW - timedelta(hours=1))}]
    if easy:
        footer.append({"type": "EASY_APPLY_TEXT", "text": {"text": "Easy Apply"}})
    return {
        "$type": "com.linkedin.voyager.dash.jobs.JobPostingCard",
        "entityUrn": f"urn:li:fsd_jobPostingCard:({job_id},JOBS_SEARCH)",
        "jobPostingUrn": f"urn:li:fsd_jobPosting:{job_id}",
        "jobPostingTitle": title,
        "title": {"text": title},
        "primaryDescription": {"text": company},
        "secondaryDescription": {"text": location},
        "footerItems": footer,
        "*jobPosting": f"urn:li:fsd_jobPosting:{job_id}",
    }


def voyager(*cards, total=None, extra=()):
    """A voyagerJobsDashJobCards-style document: a collection with paging, the cards in included."""
    ids = [card["entityUrn"] for card in cards]
    collection = {"$type": "com.linkedin.restli.common.CollectionResponse", "*elements": ids}
    if total is not None:
        collection["paging"] = {"total": total, "count": 25, "start": 0}
    return {"data": {"data": {"jobsDashJobCardsByJobSearch": collection}}, "included": [*cards, *extra]}


def results_html(*cards_html, blocks=(), count=None, no_results=False):
    codes = "".join(
        f'<code id="bpr-guid-{i}" style="display: none">{escape(json.dumps(block))}</code>'
        for i, block in enumerate(blocks)
    )
    subtitle = f'<div class="jobs-search-results-list__subtitle"><span>{count:,} results</span></div>' if count else ""
    banner = '<div class="jobs-search-no-results-banner"><h2>No matching jobs found.</h2></div>' if no_results else ""
    return f"""<html><head><title>Jobs</title></head><body>{codes}
<div class="scaffold-layout__list">{subtitle}{banner}<ul>{''.join(cards_html)}</ul></div></body></html>"""


# ---------------------------------------------------------------------------
# URLs


def test_search_url_matches_the_logged_in_search():
    codes = [code for code, _ in experience_levels(PREFS)]
    assert codes == ["2", "3", "4"]
    roles = ("Backend Developer", "SDE1")
    url = page_url(search_params(roles, "Bengaluru", codes))
    assert url == (
        "https://www.linkedin.com/jobs/search/?keywords=%22Backend%20Developer%22%20OR%20SDE1"
        "&location=Bengaluru%2C%20Karnataka%2C%20India&f_TPR=r86400&sortBy=DD&f_E=2%2C3%2C4"
    )
    remote = parse_qs(urlparse(page_url(search_params(roles, "Remote", codes), 25)).query)
    assert remote["location"] == ["India"] and remote["f_WT"] == ["2"] and remote["start"] == ["25"]
    assert "f_AL" not in remote  # Easy Apply comes from each card, not a separate search


def test_experience_levels_and_keywords():
    reordered = SearchPrefs.from_values("3+ years, 1+ years", "SDE1", "Remote")
    assert experience_levels(reordered) == [("3", 0), ("4", 0), ("2", 1)]
    assert keywords(("Software Development Engineer", "SDE1")) == '"Software Development Engineer" OR SDE1'
    assert keyword_chunks(PREFS.roles) == [
        ("Software Development Engineer", "SDE1", "SDE2", "Java Developer"),
        ("Backend Developer", "Golang Developer", "Full Stack Developer", "Forward Deployed Engineer"),
    ]
    assert keyword_chunks(("A" * 60, "B" * 60)) == [("A" * 60,), ("B" * 60,)]


# ---------------------------------------------------------------------------
# Parsers


def test_parse_embedded_json():
    html = results_html(blocks=[
        {"request": "/voyager/api/voyagerJobsDashJobCards", "status": 200, "body": "bpr-guid-1"},
        voyager(
            json_card("4012345678", "Backend Developer", company="Navi"),
            json_card("4012345679", "Java Developer", easy=False, location="India (Remote)"),
            total=61,
            extra=[
                # An entity that only points at a job: its "title" must not become the job title.
                {"$type": "com.linkedin.voyager.dash.jobs.JobPostingRelevanceReason",
                 "entityUrn": "urn:li:fsd_jobPostingRelevanceReason:4012345678", "title": "Top applicant"},
                # The older JobPosting shape: applyMethod says company site.
                {"$type": "com.linkedin.voyager.jobs.JobPosting", "entityUrn": "urn:li:fs_normalized_jobPosting:4012345680",
                 "title": "Golang Developer", "formattedLocation": "Bengaluru, Karnataka, India",
                 "listedAt": epoch_ms(NOW - timedelta(minutes=30)),
                 "applyMethod": {"$type": "com.linkedin.voyager.jobs.OffsiteApply", "companyApplyUrl": "https://x.example"},
                 "companyDetails": {"com.linkedin.voyager.deco.jobs.web.shared.WebCompactJobPostingCompany": {
                     "companyResolutionResult": {"name": "Zeta"}}}},
            ],
        ),
    ])
    result = parse_search_page(html)
    by_id = {job.job_id: job for job in result.jobs}
    assert list(by_id) == ["4012345678", "4012345679", "4012345680"]
    backend = by_id["4012345678"]
    assert (backend.title, backend.company, backend.location) == ("Backend Developer", "Navi", "Bengaluru, Karnataka, India (On-site)")
    assert backend.easy_apply is True and backend.listed_at == NOW - timedelta(hours=1)
    assert backend.url == "https://www.linkedin.com/jobs/view/4012345678/"
    assert by_id["4012345679"].easy_apply is False
    golang = by_id["4012345680"]
    assert (golang.title, golang.company, golang.easy_apply) == ("Golang Developer", "Zeta", False)
    assert result.total == 61 and result.json_jobs == 3 and result.dom_jobs == 0


def test_parse_captured_api_response():
    body = json.dumps(voyager(json_card("4012345690", "SDE II", easy=True), total=1))
    result = parse_search_page("<html><body></body></html>", [body, "not json"])
    assert [(j.job_id, j.title, j.easy_apply) for j in result.jobs] == [("4012345690", "SDE II", True)]


def test_parse_rendered_cards():
    html = results_html(
        dom_card("4011111111", "Backend Developer", insight="Actively reviewing applicants"),
        dom_card("4011111112", "Golang Developer", company="Zeta", easy=False, ago="23 hours ago", date="2026-10-12"),
        placeholder("4011111113"),
        count=1234,
    )
    result = parse_search_page(html)
    by_id = {job.job_id: job for job in result.jobs}
    backend = by_id["4011111111"]
    assert (backend.title, backend.company, backend.location) == (
        "Backend Developer", "Acme", "Bengaluru, Karnataka, India (Hybrid)"
    )
    assert (backend.easy_apply, backend.posted_label, backend.posted_attr) == (True, "2 hours ago", "2026-10-13")
    assert backend.insight == "Actively reviewing applicants"
    assert (by_id["4011111112"].easy_apply, by_id["4011111112"].company) == (False, "Zeta")
    assert by_id["4011111113"].title == "" and by_id["4011111113"].easy_apply is None  # not rendered yet
    assert result.total == 1234 and result.dom_jobs == 2 and not result.no_results


def test_json_and_cards_are_merged():
    html = results_html(
        dom_card("4012345678", "Backend Developer", easy=True),
        placeholder("4012345681"),
        blocks=[voyager(json_card("4012345681", "Java Developer", easy=False))],
    )
    by_id = {job.job_id: job for job in parse_search_page(html).jobs}
    assert by_id["4012345681"].title == "Java Developer"  # the JSON fills the unrendered card
    assert by_id["4012345678"].easy_apply is True


def test_no_results_and_blocks():
    empty = parse_search_page(results_html(no_results=True))
    assert empty.jobs == [] and empty.no_results
    assert search_block_reason(page(SEARCH, status=429)) == "LinkedIn returned HTTP 429 to the job search"
    assert search_block_reason(page(SEARCH)) is None


# ---------------------------------------------------------------------------
# The scan


def search_routes(url: str):
    query = parse_qs(urlparse(url).query)
    first_group = query["keywords"][0].startswith('"Software Development Engineer"')
    remote = query.get("f_WT") == ["2"]
    start = int(query.get("start", ["0"])[0])
    assert query["f_TPR"] == ["r86400"] and query["sortBy"] == ["DD"] and query["f_E"] == ["2,3,4"]
    if not first_group:
        return page(url, results_html(no_results=True))
    if remote:
        return page(url, results_html(dom_card("500", "Full Stack Engineer", location="India (Remote)", ago="1 hour ago", easy=False)))
    if start == 0:
        return page(url, results_html(
            dom_card("100", "Backend Developer", insight="Actively reviewing applicants"),
            dom_card("101", "Senior Software Engineer"),
            dom_card("102", "Golang Developer", ago="23 hours ago", date="2026-10-12"),
            dom_card("103", "Python Developer"),
            dom_card("104", "Software Engineer Intern"),
            dom_card("105", "Backend Developer", location="Pune, Maharashtra, India"),
            dom_card("106", "Java Developer", easy=False),
            placeholder("4000000107"),
            blocks=[voyager(json_card("4000000107", "SDE II (Backend)", easy=True), total=30)],
        ))
    if start == 25:
        return page(url, results_html(dom_card("108", "Backend Developer | 2-4 years", ago="30 minutes ago")))
    raise AssertionError(url)


def test_search_scan_rendering_only(client, fake_browser):
    """No job-data request seen on the pages: every page is rendered, at most MAX_RENDERS (12).
    Every search runs with the Easy Apply filter first, then without (2026-10-15)."""
    fake_browser.routes = [("linkedin.com/jobs/search", search_routes)]
    report = LinkedInJobsScanner().scan(FakeSession(fake_browser, None, True), make_ctx(client.app, NOW))
    assert report.failure is None and report.login_ok
    # 8 first pages (Easy Apply only / all x 2 locations x 2 role groups), then the second page of
    # the search with 30 results, in both passes.
    assert report.page_loads == 10 and fake_browser.list_scrolls == 10
    searched = [url for url in fake_browser.visited if "jobs/search" in url]
    assert all("f_AL=true" in url for url in searched[:4]) and not any("f_AL" in url for url in searched[4:8])
    by_id = {c.external_id: c for c in report.candidates}
    assert set(by_id) == {"100", "106", "4000000107", "500", "108"}

    backend = by_id["100"]
    assert backend.platform == Platform.linkedin and backend.apply_type == ApplyType.easy_apply
    assert backend.url == "https://www.linkedin.com/jobs/view/100/"
    assert (backend.matched_role, backend.priority) == ("Backend Developer", 240)
    assert backend.short_description == "Easy Apply · Actively reviewing applicants"
    assert backend.posted_at == NOW - timedelta(hours=2)
    assert by_id["106"].apply_type == ApplyType.company_site
    assert (by_id["4000000107"].matched_role, by_id["4000000107"].apply_type) == ("SDE2", ApplyType.easy_apply)  # from the JSON
    remote = by_id["500"]
    assert (remote.location, remote.priority, remote.apply_type) == ("India (Remote)", 261, ApplyType.company_site)
    assert any(note.startswith("Read ") for note in report.notes)
    assert report.notes[0] == (
        "LinkedIn's job-data request wasn't seen on the Bengaluru (Easy Apply only), Remote (Easy Apply only), "
        "Bengaluru and Remote results pages; their pages are rendered instead."
    )
    assert "Read 10 jobs (0 through LinkedIn's job-data request in 0 call(s), 10 from 10 rendered page(s)); 5 matched your roles, experience and locations." in report.notes
    assert not any(note.startswith("Stopped at this run's limit") for note in report.notes)


def test_scan_page_budget(client, fake_browser):
    def endless(url):
        start = int(parse_qs(urlparse(url).query).get("start", ["0"])[0])
        cards = [dom_card(str(1000 + start + i), "Backend Developer") for i in range(25)]
        return page(url, results_html(*cards, count=500))

    fake_browser.routes = [("linkedin.com/jobs/search", endless)]
    report = LinkedInJobsScanner().scan(FakeSession(fake_browser, None, True), make_ctx(client.app, NOW))
    assert report.page_loads == 12
    assert (
        "Stopped at this run's limit (12 rendered page(s), 0 job-data request(s)); some of today's jobs may be missed."
        in report.notes
    )


def test_scan_login_wall_and_security_check(client, fake_browser):
    fake_browser.routes = [("linkedin.com", page("https://www.linkedin.com/uas/login?session_redirect=x"))]
    report = LinkedInJobsScanner().scan(FakeSession(fake_browser, None, True), make_ctx(client.app, NOW))
    assert report.failure == "LinkedIn redirected to its login page; stopped."
    assert report.login_lost == "The saved LinkedIn login has expired. Connect again."

    fake_browser.routes = [("linkedin.com", page("https://www.linkedin.com/checkpoint/challenge/x"))]
    report = LinkedInJobsScanner().scan(FakeSession(fake_browser, None, True), make_ctx(client.app, NOW))
    assert report.failure == "LinkedIn showed a security check; stopped." and "security check" in report.login_lost


def test_scan_stops_on_429_and_unreadable_pages(client, fake_browser):
    fake_browser.routes = [("linkedin.com/jobs/search", lambda url: page(url, "<html></html>", status=429))]
    report = LinkedInJobsScanner().scan(FakeSession(fake_browser, None, True), make_ctx(client.app, NOW))
    assert report.failure.startswith("LinkedIn returned HTTP 429 to the job search; stopped.")  # the backoff reads "429"
    assert report.page_loads == 1

    fake_browser.routes = [("linkedin.com/jobs/search", lambda url: page(url, "<html><body><div>?</div></body></html>"))]
    report = LinkedInJobsScanner().scan(FakeSession(fake_browser, None, True), make_ctx(client.app, NOW))
    assert report.failure.startswith("LinkedIn's job search showed no readable jobs")
    assert "Screenshot: data/diagnostics/linkedin_jobs-" in report.failure


# ---------------------------------------------------------------------------
# Running it: login and the hourly interval


def test_run_is_skipped_when_not_connected(client, fake_browser):
    outcome = LinkedInJobsScanner().run(make_ctx(client.app, NOW))
    assert outcome.status == ScanStatus.skipped and outcome.message.startswith("LinkedIn isn't connected")
    assert fake_browser.opened == []


def test_run_uses_the_saved_login(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    fake_browser.routes = [("linkedin.com/jobs/search", lambda url: page(url, results_html(dom_card("100", "Backend Developer"))))]
    outcome = LinkedInJobsScanner().run(make_ctx(client.app, NOW))
    assert (outcome.status, outcome.found, outcome.new) == (ScanStatus.ok, 1, 1)
    profile = client.app.state.browsers.profile_dir(Platform.linkedin)
    assert fake_browser.opened == [(profile, True)]


def test_run_without_the_login_cookie_marks_linkedin_disconnected(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    fake_browser.cookies = []
    outcome = LinkedInJobsScanner().run(make_ctx(client.app, NOW))
    assert outcome.status == ScanStatus.skipped and "expired" in outcome.message
    assert client.get("/api/connections").json()["items"][0]["status"] == "disconnected"


def add_run(engine, status, minutes_ago):
    with Session(engine) as session:
        started = NOW - timedelta(minutes=minutes_ago)
        session.add(ScanRun(kind=ScanKind.linkedin_jobs, status=status, started_at=started,
                            finished_at=started + timedelta(minutes=1)))
        session.commit()


def test_search_runs_at_most_hourly(client, fake_browser):
    engine = client.app.state.engine
    assert next_search_after(engine, NOW, 60) is None  # no search yet
    add_run(engine, ScanStatus.skipped, 5)  # skipped runs don't count
    assert next_search_after(engine, NOW, 60) is None
    add_run(engine, ScanStatus.ok, 59)  # within the 2-minute slack: due
    assert next_search_after(engine, NOW, 60) is None
    add_run(engine, ScanStatus.ok, 20)
    assert next_search_after(engine, NOW, 60) == NOW + timedelta(minutes=40)

    connect_linkedin(client.app, fake_browser)
    outcome = LinkedInJobsScanner().run(make_ctx(client.app, NOW))
    due = (NOW + timedelta(minutes=40)).astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    assert outcome.status == ScanStatus.skipped
    assert outcome.message == f"Next LinkedIn search after {due:%H:%M}."
    assert fake_browser.opened == []

    ctx = make_ctx(client.app, NOW)
    ctx.settings = ctx.settings.model_copy(update={"linkedin_search_interval_minutes": 15})
    fake_browser.routes = [("linkedin.com/jobs/search", lambda url: page(url, results_html(no_results=True)))]
    assert LinkedInJobsScanner().run(ctx).status == ScanStatus.ok


def test_captured_responses_are_read(client, fake_browser):
    body = json.dumps(voyager(json_card("4012345690", "Backend Developer"), total=1))
    fake_browser.routes = [
        ("linkedin.com/jobs/search", lambda url: page(url, "<html><body></body></html>", captured=[api(url, body)])),
    ]
    report = LinkedInJobsScanner().scan(FakeSession(fake_browser, None, True), make_ctx(client.app, NOW, max_loads=1))
    assert [c.external_id for c in report.candidates] == ["4012345690"]
    assert report.candidates[0].apply_type == ApplyType.easy_apply


def test_a_run_started_from_the_app_searches_after_15_minutes(client, fake_browser):
    """"Run now" (2026-10-15): the hourly gap is 15 minutes for a run started from the app."""
    engine = client.app.state.engine
    connect_linkedin(client.app, fake_browser)
    fake_browser.routes = [("linkedin.com/jobs/search", lambda url: page(url, results_html(no_results=True)))]
    add_run(engine, ScanStatus.ok, 20)
    scheduled = make_ctx(client.app, NOW)
    assert LinkedInJobsScanner().run(scheduled).status == ScanStatus.skipped
    manual = make_ctx(client.app, NOW)
    manual.manual = True
    assert LinkedInJobsScanner().run(manual).status == ScanStatus.ok

    add_run(engine, ScanStatus.ok, 10)
    again = make_ctx(client.app, NOW)
    again.manual = True
    outcome = LinkedInJobsScanner().run(again)
    assert outcome.status == ScanStatus.skipped and outcome.message.startswith("Next LinkedIn search after ")
