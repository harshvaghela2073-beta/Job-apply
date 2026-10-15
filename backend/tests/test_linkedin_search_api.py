"""The LinkedIn job search through LinkedIn's own job-data request (``voyagerJobsDashJobCards``,
``app.sources.linkedin.search_api``): URL rewriting, headers, the answer's JSON, and the scan with a
fake browser (first page rendered per location, the rest through the request, rendering again when
the request fails). No real browser or network; jobs and companies are made up."""

import re
from datetime import timedelta
from urllib.parse import unquote

import pytest

from app.browser.driver import ApiResponse, CapturedResponse
from app.models import ApplyType
from app.sources.linkedin import search_api
from app.sources.linkedin.jobs import MAX_API_CALLS, LinkedInJobsScanner
from app.sources.linkedin.parse import parse_search_data
from tests.fakes import FakeSession, page
from tests.scan_helpers import epoch_ms, make_ctx, ten_am_today
from tests.test_linkedin_search import dom_card, json_card, placeholder, results_html, voyager

NOW = ten_am_today()

# What LinkedIn's web client requests for the list (REST and GraphQL shapes). Rest.li objects in the
# query, reserved characters inside strings percent-encoded; the page's selected job as currentJobId.
GROUP_1 = '"Software Development Engineer" OR SDE1 OR SDE2 OR "Java Developer"'
GROUP_2 = '"Backend Developer" OR "Golang Developer" OR "Full Stack Developer" OR "Forward Deployed Engineer"'
REST_TEMPLATE = (
    "https://www.linkedin.com/voyager/api/voyagerJobsDashJobCards"
    "?decorationId=com.linkedin.voyager.dash.deco.jobs.search.JobSearchCardsCollection-220&count=7&q=jobSearch"
    "&query=(currentJobId:4012345678,origin:JOB_SEARCH_PAGE_JOB_FILTER,"
    "keywords:%22Software%20Development%20Engineer%22%20OR%20SDE1%20OR%20SDE2%20OR%20%22Java%20Developer%22,"
    "locationUnion:(geoId:105214831),selectedFilters:(sortBy:List(DD),timePostedRange:List(r86400),"
    "experience:List(2,3,4)),spellCorrectionEnabled:true)&start=0"
)
GRAPHQL_TEMPLATE = (
    "https://www.linkedin.com/voyager/api/graphql?variables=(count:7,start:0,query:(origin:JOB_SEARCH_PAGE_JOB_FILTER,"
    "keywords:%22Software%20Development%20Engineer%22%20OR%20SDE1,locationUnion:(geoId:102713980),"
    "selectedFilters:(sortBy:List(DD),timePostedRange:List(r86400),workplaceType:List(2),experience:List(2,3,4)),"
    "spellCorrectionEnabled:true))&queryId=voyagerJobsDashJobCards.93590893e4adb90623f00d61719b838c"
)
COOKIES = [
    {"name": "li_at", "value": "t", "domain": ".linkedin.com"},
    {"name": "JSESSIONID", "value": '"ajax:1234567890"', "domain": ".www.linkedin.com"},
]


def decode(url: str) -> dict:
    """What a rewritten request asks for: keywords, start, count, remote, the decoded Rest.li text."""
    query = url.split("?", 1)[1]
    params = dict(part.split("=", 1) for part in query.split("&"))
    restli = unquote(params.get("query") or params.get("variables"))
    start = params.get("start") or re.search(r"\bstart:(\d+)", restli).group(1)
    count = params.get("count") or re.search(r"\bcount:(\d+)", restli).group(1)
    return {
        "keywords": re.search(r"keywords:([^,]*)", restli).group(1),
        "start": int(start),
        "count": int(count),
        "remote": "workplaceType:List(2)" in restli,
        "restli": restli,
    }


# ---------------------------------------------------------------------------
# URL rewriting


def test_rest_template_is_rewritten():
    url = search_api.rewrite(REST_TEMPLATE, '"Backend Developer" OR SDE1', 25)
    assert url.startswith(
        "https://www.linkedin.com/voyager/api/voyagerJobsDashJobCards"
        "?decorationId=com.linkedin.voyager.dash.deco.jobs.search.JobSearchCardsCollection-220&count=25&q=jobSearch&query=("
    )
    assert url.endswith("&start=25")
    assert (
        "query=(origin:JOB_SEARCH_PAGE_JOB_FILTER,keywords:%22Backend%20Developer%22%20OR%20SDE1,"
        "locationUnion:(geoId:105214831),selectedFilters:(sortBy:List(DD),timePostedRange:List(r86400),"
        "experience:List(2,3,4)),spellCorrectionEnabled:true)"
    ) in url
    assert "currentJobId" not in url  # the job selected on the rendered page
    asked = decode(url)
    assert (asked["keywords"], asked["start"], asked["count"]) == ('"Backend Developer" OR SDE1', 25, 25)


def test_graphql_template_is_rewritten():
    url = search_api.rewrite(GRAPHQL_TEMPLATE, "Golang Developer", 50)
    assert url.startswith("https://www.linkedin.com/voyager/api/graphql?variables=(count:25,start:50,query:(")
    assert url.endswith("&queryId=voyagerJobsDashJobCards.93590893e4adb90623f00d61719b838c")
    asked = decode(url)
    assert (asked["keywords"], asked["start"], asked["remote"]) == ("Golang Developer", 50, True)
    assert "locationUnion:(geoId:102713980)" in asked["restli"]


def test_reserved_characters_in_keywords_are_encoded():
    url = search_api.rewrite(REST_TEMPLATE, "C++ (Backend), Node.js: SDE-1's", 0)
    assert "keywords:C%2B%2B%20%28Backend%29%2C%20Node.js%3A%20SDE-1%27s," in url
    # The object still has its structure: the keywords didn't add keys or parentheses.
    assert [key for key, _ in search_api.parse_object(url.split("query=")[1].split("&")[0])] == [
        "origin", "keywords", "locationUnion", "selectedFilters", "spellCorrectionEnabled",
    ]


def test_template_encoded_as_a_whole_and_without_keywords():
    encoded = REST_TEMPLATE.replace(
        REST_TEMPLATE.split("query=")[1].split("&")[0],
        "%28origin%3AJOB_SEARCH_PAGE_JOB_FILTER%2ClocationUnion%3A%28geoId%3A105214831%29%29",
    )
    url = search_api.rewrite(encoded, "SDE1", 0)
    assert "query=(origin:JOB_SEARCH_PAGE_JOB_FILTER,keywords:SDE1,locationUnion:(geoId:105214831))" in url


@pytest.mark.parametrize(
    "bad",
    [
        "https://www.linkedin.com/voyager/api/voyagerJobsDashJobCards?q=jobSearch&query=(origin:X,keywords:a",
        "https://www.linkedin.com/voyager/api/voyagerJobsDashJobCards?decorationId=x&ids=List(1,2)",
        "https://www.linkedin.com/voyager/api/graphql?variables=(count:7)&queryId=voyagerJobsDashJobCards.1",
    ],
)
def test_templates_that_cant_be_rewritten(bad):
    with pytest.raises(ValueError):
        search_api.rewrite(bad, "SDE1", 0)


def test_pick_template():
    def captured(url, status=200):
        return CapturedResponse(url=url, status=status, body="{}")

    lite = REST_TEMPLATE.replace("JobSearchCardsCollection-220", "JobSearchCardsCollectionLite-88")
    cards_by_id = (
        "https://www.linkedin.com/voyager/api/voyagerJobsDashJobCards?decorationId=com.linkedin.voyager.dash.deco."
        "jobs.search.JobSearchCard-71&ids=List(urn%3Ali%3Afsd_jobPostingCard%3A%284012345678%2CJOBS_SEARCH%29)"
    )
    assert search_api.pick_template([captured(cards_by_id), captured(lite), captured(REST_TEMPLATE)]) == REST_TEMPLATE
    assert search_api.pick_template([captured(REST_TEMPLATE, 500), captured(lite)]) == lite
    assert search_api.pick_template([captured(GRAPHQL_TEMPLATE)]) == GRAPHQL_TEMPLATE
    assert search_api.pick_template([captured(cards_by_id)]) is None
    # Never another host, and never the results page itself.
    assert search_api.pick_template([captured(REST_TEMPLATE.replace("www.linkedin.com", "evil.example"))]) is None
    assert search_api.pick_template([captured("https://www.linkedin.com/jobs/search/?keywords=voyagerJobsDashJobCards")]) is None


def test_headers():
    assert search_api.headers("ajax:1") == {
        "x-restli-protocol-version": "2.0.0",
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "x-li-lang": "en_US",
        "csrf-token": "ajax:1",
    }


# ---------------------------------------------------------------------------
# The answer


def posting(job_id, *, listed=None, offsite=False):
    """The JobPosting entity the answer includes next to each card."""
    apply = {"$type": "com.linkedin.voyager.dash.jobs.OffsiteApply"} if offsite else {
        "$type": "com.linkedin.voyager.dash.jobs.ComplexOnsiteApply", "easyApplyUrl": "https://www.linkedin.com/x"}
    return {
        "$type": "com.linkedin.voyager.dash.jobs.JobPosting", "entityUrn": f"urn:li:fsd_jobPosting:{job_id}",
        "title": "ignored when the card has one", "repostedJob": False,
        "listedAt": epoch_ms(listed or NOW - timedelta(minutes=40)), "applyMethod": apply,
    }


def answer(*cards, total, without_details=(), extra=()):
    """A REST voyagerJobsDashJobCards answer (normalized JSON): the list in data.elements as card
    urns, the cards and postings in included."""
    urns = [card["entityUrn"] for card in cards] + [f"urn:li:fsd_jobPostingCard:({i},JOBS_SEARCH)" for i in without_details]
    return {
        "data": {
            "$type": "com.linkedin.restli.common.CollectionResponse",
            "elements": [{"jobCardUnion": {"*jobPostingCard": urn}, "$type": "com.linkedin.voyager.dash.jobs.JobCardUnion"} for urn in urns],
            "paging": {"count": 25, "start": 0, "total": total, "links": []},
            "metadata": {"$type": "com.linkedin.voyager.dash.jobs.JobSearchMetadata"},
        },
        "included": [*cards, *extra],
    }


def test_parse_search_data():
    data = answer(
        json_card("4012345601", "Backend Developer", company="Navi"),
        json_card("4012345602", "Java Developer", easy=False),
        total=48,
        without_details=["4012345603"],
        extra=[posting("4012345601"), posting("4012345602", offsite=True)],
    )
    page_ = parse_search_data(data)
    assert [(j.job_id, j.title, j.company, j.easy_apply) for j in page_.readable] == [
        ("4012345601", "Backend Developer", "Navi", True),
        ("4012345602", "Java Developer", "Acme", False),
    ]
    assert page_.total == 48 and page_.listed == ["4012345601", "4012345602", "4012345603"]
    assert page_.missing == ["4012345603"] and not page_.no_results
    empty = parse_search_data(answer(total=0))
    assert empty.jobs == [] and empty.no_results
    assert parse_search_data({"unexpected": True}).total is None


# ---------------------------------------------------------------------------
# The scan


class FakeLinkedIn:
    """The rendered results pages (with the list request they make) and the job-data answers.
    ``status`` / ``body`` override the job-data answers (failure cases)."""

    def __init__(self, status=200, body="ok", remote_template=GRAPHQL_TEMPLATE):
        self.status, self.body, self.remote_template = status, body, remote_template
        self.headers: list[dict] = []
        self.calls: list[dict] = []

    def results(self, url):
        remote = "f_WT=2" in url
        if remote:  # the lazy list rendered one card
            html = results_html(
                dom_card("4000000301", "Full Stack Developer", location="India (Remote)", easy=False),
                placeholder("4000000302"),
            )
            template = self.remote_template
        else:
            html = results_html(
                dom_card("4000000101", "Backend Developer"), placeholder("4000000102"),
                blocks=[voyager(json_card("4000000101", "Backend Developer"), total=30)],
            )
            template = REST_TEMPLATE
        captured = [CapturedResponse(url=template, status=200, body="{}")] if template else []
        return page(url, html, captured=captured)

    def api(self, url, headers):
        self.headers.append(headers)
        asked = decode(url)
        self.calls.append(asked)
        if self.status != 200 or self.body != "ok":
            return ApiResponse(status=self.status, url=url, json=None if self.body in ("ok", None) else self.body)
        first_group = asked["keywords"] == GROUP_1
        if asked["remote"]:
            data = (
                answer(json_card("4000000301", "Full Stack Developer", location="India (Remote)", easy=False), total=1)
                if first_group else answer(total=0)
            )
        elif not first_group:
            data = answer(json_card("4000000201", "Golang Developer"), total=1)
        elif asked["start"] == 0:
            data = answer(
                json_card("4000000101", "Backend Developer"), json_card("4000000102", "Java Developer", easy=False),
                total=30, without_details=["4000000199"],
            )
        else:
            data = answer(json_card("4000000103", "SDE II"), total=30)
        return ApiResponse(status=200, url=url, json=data)


class AllJobsOnly(LinkedInJobsScanner):
    """These tests cover the job-data request with one pass of searches (no Easy Apply filter); the
    Easy Apply pass (2026-10-15) is tested in test_linkedin_search.py."""

    def queries(self, prefs):
        return [query for query in super().queries(prefs) if not query.easy_only]


def scan(fake_browser, linkedin: FakeLinkedIn, **ctx_args):
    fake_browser.cookies = list(COOKIES)
    fake_browser.routes = [("linkedin.com/jobs/search", linkedin.results)]
    fake_browser.api_routes = [("voyager", linkedin.api)]
    return AllJobsOnly().scan(FakeSession(fake_browser, None, True), make_ctx(fake_browser.app, NOW, **ctx_args))


@pytest.fixture
def browser_with_app(client, fake_browser):
    fake_browser.app = client.app
    return fake_browser


def test_scan_renders_one_page_per_location_and_uses_the_request(browser_with_app):
    linkedin = FakeLinkedIn()
    report = scan(browser_with_app, linkedin)
    assert report.failure is None and report.login_ok

    rendered = [url for url in browser_with_app.visited if "/jobs/search/" in url]
    assert len(rendered) == 2 and "f_WT=2" in rendered[1]  # Bengaluru, then Remote; first role group each
    # Bengaluru group 2, Remote group 2, then (priority order) Bengaluru group 1 again from 0 (the
    # rendered page showed 1 of its jobs) and its page 2, and the Remote first page likewise.
    assert [(c["remote"], c["keywords"] == GROUP_1, c["start"]) for c in linkedin.calls] == [
        (False, False, 0), (True, False, 0), (False, True, 0), (False, True, 25), (True, True, 0),
    ]
    assert all(c["count"] == 25 for c in linkedin.calls)
    assert linkedin.calls[1]["keywords"] == GROUP_2
    assert all(h["csrf-token"] == "ajax:1234567890" and h["x-li-lang"] == "en_US" for h in linkedin.headers)
    assert report.page_loads == 7

    by_id = {c.external_id: c for c in report.candidates}
    assert set(by_id) == {"4000000101", "4000000102", "4000000103", "4000000201", "4000000301"}
    assert by_id["4000000102"].apply_type == ApplyType.company_site  # only the request had this card
    assert (by_id["4000000103"].matched_role, by_id["4000000103"].apply_type) == ("SDE2", ApplyType.easy_apply)
    assert by_id["4000000301"].location == "India (Remote)"
    assert "Read 5 jobs (3 through LinkedIn's job-data request in 5 call(s), 2 from 2 rendered page(s)); " \
        "5 matched your roles, experience and locations." in report.notes
    assert "1 listed job(s) came without details (title, company) and were skipped." in report.notes
    assert not any("didn't work" in note or "Stopped at" in note for note in report.notes)


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (403, "ok", "HTTP 403"),
        (302, "ok", "HTTP 302"),
        (200, None, "the answer wasn't JSON"),
        (200, {"data": {"paging": {"total": 3}}}, "no readable jobs in the answer"),
    ],
)
def test_request_failure_falls_back_to_rendering(browser_with_app, status, body, reason):
    linkedin = FakeLinkedIn(status=status, body=body)
    report = scan(browser_with_app, linkedin)
    assert report.failure is None
    assert (
        f"LinkedIn's job-data request didn't work for Bengaluru ({reason}); its pages are rendered instead (slower)."
        in report.notes
    )
    # Each location's request is tried once; the failed pages are rendered instead.
    assert len(linkedin.calls) == 2
    rendered = [url for url in browser_with_app.visited if "/jobs/search/" in url]
    # 4 first pages and the further pages the fake has, within MAX_RENDERS (12).
    assert len(rendered) == 7 and report.page_loads == 9
    assert "4000000101" in {c.external_id for c in report.candidates}


def test_too_many_requests_stops_the_run(browser_with_app):
    report = scan(browser_with_app, FakeLinkedIn(status=429))
    assert report.failure.startswith("LinkedIn returned HTTP 429 to the job-data request; stopped.")
    assert "4000000101" in {c.external_id for c in report.candidates}  # kept what was read before


def test_no_request_seen_or_no_csrf_cookie(browser_with_app):
    linkedin = FakeLinkedIn(remote_template=None)
    report = scan(browser_with_app, linkedin)
    assert "LinkedIn's job-data request wasn't seen on the Remote results page; its pages are rendered instead." \
        in report.notes
    assert not any(c["remote"] for c in linkedin.calls)

    browser_with_app.visited.clear()
    linkedin = FakeLinkedIn()
    browser_with_app.cookies = [COOKIES[0]]
    browser_with_app.routes = [("linkedin.com/jobs/search", linkedin.results)]
    report = AllJobsOnly().scan(FakeSession(browser_with_app, None, True), make_ctx(browser_with_app.app, NOW))
    assert linkedin.calls == []
    assert any("(no JSESSIONID cookie for its csrf-token)" in note for note in report.notes)


def test_request_budget(browser_with_app):
    class Endless(FakeLinkedIn):
        def api(self, url, headers):
            asked = decode(url)
            self.calls.append(asked)
            cards = [json_card(str(4100000000 + asked["start"] + i), "Backend Developer") for i in range(25)]
            return ApiResponse(status=200, url=url, json=answer(*cards, total=900))

    linkedin = Endless()
    report = scan(browser_with_app, linkedin)
    assert len(linkedin.calls) == MAX_API_CALLS and report.page_loads == 2 + MAX_API_CALLS
    assert any(note.startswith(f"Stopped at this run's limit (2 rendered page(s), {MAX_API_CALLS} job-data request(s))")
               for note in report.notes)
