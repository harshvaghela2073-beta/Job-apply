"""LinkedIn job pages in the 2026-10-14 layout ("SDUI": generated class names). People are made up."""

from app.sources.linkedin.easy_apply import read_job_page
from app.sources.linkedin.parse import parse_hiring_team

PAGE = """<html><body><div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails">
 <div class="a1"><h1>Java Software Engineer</h1>
  <button type="button" componentkey="k1" aria-label="Easy Apply to this job"><span>Easy Apply</span></button>
  <button type="button" componentkey="k2" aria-label="Save the job">Save</button></div>
 <div class="b2"><div class="b3"><p class="b4">Meet the hiring team</p></div>
  <div class="b5"><div class="b6">
   <a href="https://www.linkedin.com/in/priya-ta-12ab/?trk=x">Priya Sharma • 2nd Senior Talent Acquisition Specialist | Tech Hiring</a>
   <a href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AX" aria-label="Message">Message</a>
  </div></div></div>
</div></body></html>"""

EXTERNAL = """<html><body><a aria-label="Apply to Backend Engineer on company website" href="https://jobs.example.com/1">Apply</a></body></html>"""


def test_easy_apply_button_in_new_layout():
    state = read_job_page(PAGE)
    assert state.easy_apply and not state.external_apply and not state.applied


def test_company_site_button_in_new_layout():
    state = read_job_page(EXTERNAL)
    assert state.external_apply and not state.easy_apply


def test_hiring_team_in_new_layout():
    (person,) = parse_hiring_team(PAGE)
    assert person.name == "Priya Sharma"
    assert person.degree == "2nd"
    assert person.headline.startswith("Senior Talent Acquisition Specialist")
    assert person.profile_url == "https://www.linkedin.com/in/priya-ta-12ab/"


# Seen live on 2026-10-14: a job listed as Easy Apply that was applied to an hour earlier shows no
# Apply button at all, only this card (generated class names).
APPLIED = """<html><body><div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails">
 <div class="c1"><p class="c2">Acme HR Solutions</p><h1>Java Developer</h1>
  <p>Bengaluru, Karnataka, India · 3 hours ago · Over 100 applicants</p></div>
 <div class="c3"><h2 class="c4">Take the next step in your job search</h2></div>
 <div class="c5"><div class="c6"><h2 class="c7">Application status</h2></div>
  <ul class="c8"><li><div><p class="c9">Application submitted</p><p>1 hour ago</p>
   <a href="https://jobs.example.com/apply/1">Go to company site</a></div></li></ul></div>
</div></body></html>"""


def test_application_status_card_means_already_applied():
    state = read_job_page(APPLIED)
    assert state.applied and not state.easy_apply and not state.external_apply


def test_application_status_heading_alone_is_not_applied():
    page = APPLIED.replace("Application submitted", "Not applied yet").replace("1 hour ago", "")
    assert not read_job_page(page).applied


FACTS_PAGE = """<html><head><title>Backend Engineer (Java) | Acme Talent Partners | LinkedIn</title></head><body>
<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails">
 <div class="t1"><a href="https://www.linkedin.com/company/acme-talent/">Acme Talent Partners</a>
  <p class="t2">Backend Engineer (Java)</p><p>Bengaluru, Karnataka, India · 58 minutes ago · 30 applicants</p>
  <ul><li><span>4M INR/yr - 6M INR/yr</span></li><li><span>On-site</span></li></ul>
  <button aria-label="Easy Apply to this job">Easy Apply</button></div>
 <div class="t3"><h2>About the job</h2><div><p><span data-testid="expandable-text-box">Build Java and Spring Boot
  services. 1-3 years of experience.</span></p></div></div>
 <div class="t4"><h2>About the company</h2><div><p>Acme Talent Partners</p><p>12,000 followers</p>
  <p>Staffing and Recruiting • 51-200 employees</p></div></div>
 <div class="t5"><h2>More jobs</h2><p>Java Lead</p><span>9M INR/yr - 12M INR/yr</span></div>
</div></body></html>"""


def test_job_facts_in_new_layout():
    from app.sources.linkedin.screening import parse_job_facts

    facts = parse_job_facts(FACTS_PAGE)
    assert (facts.title, facts.company) == ("Backend Engineer (Java)", "Acme Talent Partners")
    assert facts.salary == "4M INR/yr - 6M INR/yr"  # the top card's pill, not a "More jobs" card's
    assert facts.description == "Build Java and Spring Boot services. 1-3 years of experience."
    assert facts.industry == "Staffing and Recruiting"


def test_inr_salary_format():
    from app.sources.linkedin.screening import linkedin_salary_top_lpa

    assert linkedin_salary_top_lpa("1M INR/yr - 3M INR/yr") == 30.0
    assert linkedin_salary_top_lpa("50K INR/mo") == 6.0


# 2026-10-15 (Cisco, saved page): the company-site button is a link through LinkedIn's safety redirect,
# and the top card says how old the job is (a promoted listing in a 24-hour search).
COMPANY_SITE_PAGE = """<html><head><title>Software Engineer | Acme Networks | LinkedIn</title></head><body>
<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails">
 <div><p>Acme Networks</p><p>Software Engineer</p>
  <p>Bengaluru, Karnataka, India · 2 weeks ago · Over 100 people clicked apply</p>
  <a href="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fcareers%2Eacme%2Ecom%2Fjobs%2F123%3Fsrc%3Dli&urlhash=x"
     target="_blank" componentkey="0e92" aria-label="Apply on company website"><span>Apply</span></a></div>
 <div><h2>Use AI to assess how you fit</h2><p>Posted 3 days ago in another card</p></div>
</div></body></html>"""


def test_company_website_link_and_its_target():
    from app.sources.linkedin.easy_apply import external_target

    state = read_job_page(COMPANY_SITE_PAGE)
    assert state.external_apply and not state.easy_apply
    assert state.external_url == "https://careers.acme.com/jobs/123?src=li"
    assert read_job_page(EXTERNAL).external_url == "https://jobs.example.com/1"
    assert external_target("https://www.linkedin.com/jobs/view/1/") is None


def test_posted_age_and_old_jobs():
    from datetime import datetime, timezone

    from app.sources.linkedin.easy_apply import posted_too_long_ago
    from app.sources.linkedin.screening import parse_job_facts

    assert parse_job_facts(COMPANY_SITE_PAGE).posted == "2 weeks ago"  # the top card, not later cards
    now = datetime(2026, 10, 15, 7, 0, tzinfo=timezone.utc)
    assert posted_too_long_ago("2 weeks ago", now) and posted_too_long_ago("Reposted 3 days ago", now)
    assert not posted_too_long_ago("5 hours ago", now) and not posted_too_long_ago("", now)
