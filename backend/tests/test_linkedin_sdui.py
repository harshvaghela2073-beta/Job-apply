"""LinkedIn's post search in its "SDUI" layout (seen live on 2026-10-14): generated class names,
posts wrapped in div[componentkey="expanded<id>FeedType_FLAGSHIP_SEARCH"], text in the expandable
text box. People and companies below are made up."""

from app.sources.linkedin.parse import parse_posts

PAGE = """<html><body><div data-sdui-screen="com.linkedin.sdui.flagshipnav.search.SearchResultsContent">
<div data-testid="lazy-column">
 <div class="x1" componentkey="expandedAbC123FeedType_FLAGSHIP_SEARCH">
  <span>Feed post</span>
  <a href="https://www.linkedin.com/in/asha-recruiter-12ab/"><svg aria-label="View Asha Recruiter’s profile"></svg></a>
  <div><a href="https://www.linkedin.com/in/asha-recruiter-12ab/?miniProfileUrn=x">Asha Recruiter • 3rd+</a></div>
  <div>Talent Acquisition at Acme Payments</div>
  <div>2h • <button aria-label="Follow Asha Recruiter">Follow</button></div>
  <div data-testid="expandable-text-box">We're hiring Backend Developers (Java, 1-3 yrs) in Bengaluru!
   Apply here: <a href="https://lnkd.in/gAbCd12">https://lnkd.in/gAbCd12</a> or mail
   <a href="mailto:jobs@acme.example">jobs@acme.example</a>
   <a href="https://www.linkedin.com/search/results/all/?keywords=%23hiring">#hiring</a>
   <button aria-hidden="true" data-testid="expandable-text-button"><span><span>…</span><span>more</span></span></button></div>
 </div>
 <div class="x1" componentkey="expandedXyZ789FeedType_FLAGSHIP_SEARCH">
  <span>Feed post</span>
  <a href="https://www.linkedin.com/company/onog-example/"><svg aria-label="View company: ONOG Example"></svg></a>
  <a href="https://www.linkedin.com/company/onog-example/">ONOG Example</a>
  <div>3m • Follow</div>
  <div data-testid="expandable-text-box">WE'RE HIRING | JAVA FULL STACK DEVELOPER in Noida. Fill the form
   https://forms.gle/AbCdEf123</div>
 </div>
</div></div></body></html>"""


def test_sdui_posts():
    first, second = parse_posts(PAGE)

    assert first.urn == "sdui:AbC123"
    assert first.author == "Asha Recruiter"
    assert first.author_url == "https://www.linkedin.com/in/asha-recruiter-12ab/"
    assert first.url == "https://www.linkedin.com/in/asha-recruiter-12ab/recent-activity/all/"
    assert first.headline == "Talent Acquisition at Acme Payments"
    assert first.time_label.startswith("2h")
    assert "Backend Developers (Java, 1-3 yrs) in Bengaluru" in first.text
    assert "mailto:jobs@acme.example" in first.links and "https://lnkd.in/gAbCd12" in first.links
    assert first.text.endswith("#hiring")  # not the "… more" button (seen on a saved page, 2026-10-14)

    assert second.urn == "sdui:XyZ789"
    assert second.author == "ONOG Example" and second.author_url is None
    assert second.url == "https://www.linkedin.com/company/onog-example/posts/"
    assert "forms.gle/AbCdEf123" in second.text


def test_old_layout_still_parses_when_no_sdui_posts():
    assert parse_posts("<html><body><p>No results found</p></body></html>") == []
