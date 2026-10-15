from datetime import timedelta

import pytest

from app.models import ApplyType
from app.sources.linkedin.parse import (
    account_name,
    company_from_headline,
    find_emails,
    find_form_links,
    guest_block_reason,
    parse_job_cards,
    parse_posts,
    parse_result_count,
)
from app.sources.linkedin.selectors import POST_FORM_SCROLLS, POST_SCROLLS
from app.sources.linkedin.posts import FORM_QUERIES, LinkedInPostsScanner, best_role, post_queries, search_url
from app.scanner.matching import RoleMatcher
from tests.fakes import FakeSession, page
from tests.scan_helpers import PREFS, make_ctx, ten_am_today

NOW = ten_am_today()


def job_card(job_id, title, *, company="Acme", location="Bengaluru, Karnataka, India",
             ago="2 hours ago", date="2026-10-13", benefit=None):
    benefit_html = (
        f'<div class="job-posting-benefits text-sm"><span class="job-posting-benefits__text">{benefit}</span></div>'
        if benefit else ""
    )
    return f"""
<li>
  <div class="base-card relative w-full base-card--link base-search-card job-search-card"
       data-entity-urn="urn:li:jobPosting:{job_id}" data-row="1">
    <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/{title.lower().replace(' ', '-')}-at-acme-{job_id}?position=1&amp;pageNum=0">
      <span class="sr-only">{title}</span></a>
    <div class="base-search-card__info">
      <h3 class="base-search-card__title">{title}</h3>
      <h4 class="base-search-card__subtitle"><a class="hidden-nested-link" href="https://in.linkedin.com/company/acme">{company}</a></h4>
      <div class="base-search-card__metadata">
        <span class="job-search-card__location">{location}</span>
        {benefit_html}
        <time class="job-search-card__listdate--new" datetime="{date}">{ago}</time>
      </div>
    </div>
  </div>
</li>"""


def results_page(*cards, count=None):
    count = len(cards) if count is None else count
    return f"""<html><head><title>jobs</title></head><body>
<h1><span class="results-context-header__job-count">{count:,}</span> jobs</h1>
<ul class="jobs-search__results-list">{''.join(cards)}</ul></body></html>"""


# ---------------------------------------------------------------------------
# Public job search parsers (guest pages; the scanner uses the logged-in search since 2026-10-13,
# see test_linkedin_search.py)


def test_parse_job_cards():
    html = results_page(
        job_card("4465486961", "SDE II (Backend)", company="Navi", ago="6 hours ago", benefit="Actively Hiring"),
        job_card("4477502258", "Golang Developer (Fintech)", location="Greater Bengaluru Area", ago="23 hours ago", date="2026-10-12"),
        count=765,
    )
    cards = parse_job_cards(html)
    assert [c.job_id for c in cards] == ["4465486961", "4477502258"]
    sde = cards[0]
    assert (sde.title, sde.company, sde.location) == ("SDE II (Backend)", "Navi", "Bengaluru, Karnataka, India")
    assert sde.url == "https://www.linkedin.com/jobs/view/4465486961/"
    assert (sde.posted_label, sde.posted_attr, sde.benefit) == ("6 hours ago", "2026-10-13", "Actively Hiring")
    assert parse_result_count(html) == 765
    assert parse_result_count("<html></html>") is None


def test_job_card_id_from_link_when_no_urn():
    html = '<div class="base-card"><a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/backend-at-x-4478774071?position=7"></a><h3 class="base-search-card__title">Back End Developer</h3></div>'
    (card,) = parse_job_cards(html)
    assert card.job_id == "4478774071" and card.title == "Back End Developer"


def test_guest_block_reason():
    assert guest_block_reason(page("https://www.linkedin.com/authwall?trk=x")) == \
        "LinkedIn redirected the public job search to authwall"
    assert guest_block_reason(page("https://www.linkedin.com/jobs/search/", status=999)) == \
        "LinkedIn returned HTTP 999 to the public job search"
    assert guest_block_reason(page("https://www.linkedin.com/jobs/search/?keywords=x")) is None


# ---------------------------------------------------------------------------
# Hiring posts (markup needs a live check after login)


def post_html(urn, text, *, time="2h • ", headline="Engineering Manager at Acme Corp | Hiring", links=""):
    return f"""
<div class="feed-shared-update-v2 feed-shared-update-v2--minor-update" data-urn="{urn}" role="article">
  <div class="update-components-actor">
    <span class="update-components-actor__title"><span aria-hidden="true">Priya Sharma</span></span>
    <span class="update-components-actor__description"><span aria-hidden="true">{headline}</span></span>
    <span class="update-components-actor__sub-description"><span aria-hidden="true">{time}</span></span>
  </div>
  <div class="feed-shared-update-v2__description">
    <div class="update-components-text"><span class="break-words">{text}</span></div>
  </div>
  {links}
</div>"""


FORM_POST = post_html(
    "urn:li:activity:7381000000000000001",
    "We're hiring Backend Developers (1-3 years) in Bangalore!<br>Apply via the Google Form: "
    '<a href="https://forms.gle/AbC123xyz">https://forms.gle/AbC123xyz</a>',
)
EMAIL_POST = post_html(
    "urn:li:activity:7381000000000000002",
    "Hiring SDE-2 | Golang | Remote. 2+ years. Share your resume at Jobs.Acme@Example.com.",
    headline="Talent Acquisition @ Example Labs",
)
NEITHER_POST = post_html("urn:li:activity:7381000000000000003", "Hiring Java Developers in Bengaluru! DM me.")
INTERN_POST = post_html(
    "urn:li:activity:7381000000000000004",
    "Hiring Backend Developer Interns in Bangalore. Email intern@acme.com",
)
OLD_POST = post_html("urn:li:activity:7381000000000000005", "Hiring Java Developer. jobs@acme.com", time="1d • ")
OTHER_CITY_POST = post_html("urn:li:activity:7381000000000000006", "Hiring Java Developer in Hyderabad. jobs@acme.com")
SENIOR_POST = post_html("urn:li:activity:7381000000000000007", "Hiring Backend Developer, 5-8 years. jobs@acme.com")
SHORT_LINK_POST = post_html(
    "urn:li:activity:7381000000000000008",
    'Hiring Full Stack Developers! Fill this Google Form: <a href="https://lnkd.in/gXyZ12">https://lnkd.in/gXyZ12</a>',
)
SEARCH_PAGE = "<html><body><ul>" + "".join(
    f"<li>{p}</li>" for p in (FORM_POST, EMAIL_POST, NEITHER_POST, INTERN_POST, OLD_POST,
                              OTHER_CITY_POST, SENIOR_POST, SHORT_LINK_POST)
) + "</ul></body></html>"


def test_parse_posts():
    posts = parse_posts(SEARCH_PAGE)
    assert len(posts) == 8
    form = posts[0]
    assert form.urn == "urn:li:activity:7381000000000000001"
    assert form.url == "https://www.linkedin.com/feed/update/urn:li:activity:7381000000000000001/"
    assert form.author == "Priya Sharma" and form.headline == "Engineering Manager at Acme Corp | Hiring"
    assert form.time_label == "2h •"
    assert "https://forms.gle/AbC123xyz" in form.links
    assert find_form_links(form.text, *form.links) == ["https://forms.gle/AbC123xyz"]
    assert find_emails(posts[1].text) == ["jobs.acme@example.com"]
    assert find_form_links(posts[2].text) == [] and find_emails(posts[2].text) == []


def test_parse_posts_unknown_markup_falls_back_to_urns():
    html = '<div data-id="urn:li:activity:99"><p>Hiring Golang Developer, form: docs.google.com/forms/d/e/1FAIpQ/viewform</p></div>'
    (post,) = parse_posts(html)
    assert post.urn == "urn:li:activity:99"
    assert find_form_links(post.text) == ["https://docs.google.com/forms/d/e/1FAIpQ/viewform"]


@pytest.mark.parametrize(
    "headline, company",
    [
        ("Engineering Manager at Acme Corp | Hiring", "Acme Corp"),
        ("Talent Acquisition @ Example Labs", "Example Labs"),
        ("SDE2 at Flipkart, ex-Amazon", "Flipkart"),
        ("Recruiter", None),
    ],
)
def test_company_from_headline(headline, company):
    assert company_from_headline(headline) == company


def test_account_name():
    assert account_name('<img class="global-nav__me-photo" alt="Harsh Vaghela" src="x">') == "Harsh Vaghela"
    assert account_name('<div class="profile-card-name">Harsh V</div>') == "Harsh V"
    assert account_name("<div></div>") is None


def test_post_queries():
    queries = post_queries(("Software Development Engineer", "SDE1", "SDE2", "Java Developer", "Backend Developer"))
    assert queries == [
        # Google Form searches first (2026-10-15), then one search per role.
        'hiring "google form"',
        '"google form" developer',
        '"google form" "software engineer"',
        '"google form" SDE',
        '"fill the form" hiring',
        'hiring "Software Development Engineer"',
        "hiring SDE1",
        "hiring SDE2",
        'hiring "Java Developer"',
        'hiring "Backend Developer"',
    ]
    first = queries[0]  # a Google Form search: the past week (2026-10-15)
    assert "datePosted=%22past-week%22" in search_url(first) and "sortBy=%22date_posted%22" in search_url(first)
    assert "datePosted=%22past-24h%22" in search_url(queries[-1])


def test_best_role_skips_intern_lines():
    matcher = RoleMatcher(PREFS.roles)
    assert best_role(matcher, "Hiring Backend Developer interns.\nAlso hiring Java Developers!") == ("Java Developer", 3)
    assert best_role(matcher, "Backend Developer internship") is None


def test_posts_scan(client, fake_browser):
    fake_browser.routes = [
        ("lnkd.in/gXyZ12", page("https://www.linkedin.com/safety/go?url=https%3A%2F%2Fforms.gle%2FZZ99&trk=x")),
        ("search/results/content", page("https://www.linkedin.com/search/results/content/?keywords=x", SEARCH_PAGE)),
    ]
    session = FakeSession(fake_browser, None, True)
    report = LinkedInPostsScanner().scan(session, make_ctx(client.app, NOW))
    assert report.failure is None and report.login_ok
    # More scrolls for the Google Form searches than for the role searches.
    assert session.scrolls == POST_FORM_SCROLLS * len(FORM_QUERIES) + POST_SCROLLS * len(PREFS.roles)
    # The lnkd.in link is followed with a plain request, not a page load.
    assert "LINK https://lnkd.in/gXyZ12" in fake_browser.visited
    assert not any(url.startswith("https://lnkd.in") for url in fake_browser.visited)
    by_id = {c.external_id: c for c in report.candidates}
    assert set(by_id) == {
        "urn:li:activity:7381000000000000001",
        "urn:li:activity:7381000000000000002",
        "urn:li:activity:7381000000000000008",
    }
    form = by_id["urn:li:activity:7381000000000000001"]
    assert form.apply_type == ApplyType.google_form and form.form_url == "https://forms.gle/AbC123xyz"
    assert form.title == "Backend Developer (hiring post)" and form.company == "Acme Corp"
    assert form.post_url == form.url == "https://www.linkedin.com/feed/update/urn:li:activity:7381000000000000001/"
    assert (form.location, form.experience, form.priority) == ("Bengaluru", "1-3 years", 40)
    assert form.posted_at == NOW - timedelta(hours=2)

    email = by_id["urn:li:activity:7381000000000000002"]
    assert email.apply_type == ApplyType.email and email.contact_email == "jobs.acme@example.com"
    assert email.form_url is None and email.company == "Example Labs"
    assert (email.matched_role, email.location, email.priority) == ("SDE2", "Remote", 121)

    short = by_id["urn:li:activity:7381000000000000008"]
    assert short.form_url == "https://forms.gle/ZZ99"
    assert short.location == "" and short.priority == 2 * 100 + 6 * 10 + 1  # no location named: last
    assert any("Google Form (1 behind lnkd.in links)" in note and "Followed 1 lnkd.in link(s)." in note
               for note in report.notes)


def test_posts_scan_login_wall(client, fake_browser):
    fake_browser.routes = [("linkedin.com", page("https://www.linkedin.com/uas/login?session_redirect=x"))]
    report = LinkedInPostsScanner().scan(FakeSession(fake_browser, None, True), make_ctx(client.app, NOW))
    assert report.failure == "LinkedIn redirected to its login page; stopped."
    assert report.login_lost == "The saved LinkedIn login has expired. Connect again."

    fake_browser.routes = [("linkedin.com", page("https://www.linkedin.com/checkpoint/challenge/x"))]
    report = LinkedInPostsScanner().scan(FakeSession(fake_browser, None, True), make_ctx(client.app, NOW))
    assert report.failure == "LinkedIn showed a security check; stopped."
    assert "security check" in report.login_lost


def test_posts_scanner_skipped_when_not_connected(client):
    outcome = LinkedInPostsScanner().run(make_ctx(client.app, NOW))
    assert outcome.status == "skipped"
    assert outcome.message.startswith("LinkedIn isn't connected")



def test_course_ads_are_not_jobs():
    from app.sources.linkedin.posts import is_course_ad

    ad = ("✨ Learn frontend technologies like HTML, CSS, JavaScript, and React. Google form: https://lnkd.in/x "
          "⚙️ Explore backend technologies. 🚀 Build real-world projects, strengthen your portfolio.")
    assert is_course_ad(ad)
    assert is_course_ad("Join our Java Full Stack bootcamp! Limited seats, enroll now: forms.gle/abc")
    hiring = ("We're hiring a Backend Developer (1-3 years) in Bengaluru. You'll learn from a great team and "
              "work on real systems. Apply via Google Form: https://forms.gle/abc")
    assert not is_course_ad(hiring)
    assert not is_course_ad("Hiring SDE1! Learn more about the role and apply here: https://forms.gle/xyz")



def test_form_searches_look_back_a_week_and_form_posts_may_be_7_days_old():
    from app.sources.linkedin.posts import recent_enough

    assert "datePosted=%22past-week%22" in search_url('"google form" SDE')
    assert "datePosted=%22past-24h%22" in search_url("hiring SDE1")
    assert recent_enough(NOW - timedelta(days=5), NOW, has_form=True)
    assert not recent_enough(NOW - timedelta(days=8), NOW, has_form=True)
    assert not recent_enough(NOW - timedelta(days=2), NOW, has_form=False)  # email posts: today only
    assert recent_enough(None, NOW, has_form=False)



def test_short_links_of_a_week_old_post_are_followed(client, fake_browser):
    """2026-10-15: a matching post from 3 days ago with only an lnkd.in link was dropped as "too old"
    (the email rule) before its link was followed; the link hid a Google Form."""
    from app.sources.linkedin.posts import LinkedInPostsScanner

    post = ('<div componentkey="expandedurn:li:activity:7381000000000000077FeedType_FLAGSHIP_SEARCH">'
            '<a href="https://www.linkedin.com/in/hr-person/">HR Person</a><span>3d •</span>'
            '<div data-testid="expandable-text-box">We are hiring a Backend Developer (1-3 years) in Bengaluru. '
            'Apply: <a href="https://lnkd.in/gWeek01">https://lnkd.in/gWeek01</a></div></div>')
    fake_browser.routes = [
        ("lnkd.in/gWeek01", page("https://www.linkedin.com/safety/go?url=https%3A%2F%2Fforms.gle%2FWEEK1")),
        ("search/results/content", page("https://www.linkedin.com/search/results/content/?keywords=x",
                                        f"<html><body><main>{post}</main></body></html>")),
    ]
    report = LinkedInPostsScanner().scan(FakeSession(fake_browser, None, True), make_ctx(client.app, NOW))
    forms = [c for c in report.candidates if c.form_url == "https://forms.gle/WEEK1"]
    assert forms and forms[0].apply_type == ApplyType.google_form
