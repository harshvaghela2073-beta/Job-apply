"""Who gets a connection note or an email (2026-10-27, ``app.sources.linkedin.hiring``): the replies
that started it were a job seeker ("which company?"), recruiters abroad ("outside Vietnam",
"Indonesian only") and people who aren't recruiters. The posts below are modelled on the ones that
got notes then; names and companies are made up."""

import pytest

from app.sources.linkedin.hiring import (
    in_bengaluru_or_remote_india,
    is_hiring_headline,
    is_reshare,
    is_seeker_headline,
    is_seeker_post,
    post_outreach_problem,
    team_outreach_problem,
)
from app.sources.linkedin.parse import HiringPerson, Post, pick_contact
from app.sources.linkedin.posts import LinkedInPostsScanner
from app.scanner.matching import RoleMatcher
from tests.scan_helpers import make_ctx
from tests.test_linkedin import NOW

AUTHOR = "https://www.linkedin.com/in/asha-example/"


@pytest.mark.parametrize(
    "text",
    [
        "🚀 #OpenToWork | Java Backend Developer | Immediate Joiner. I’m actively looking for my next opportunity!",
        "Hi everyone! I'm Asha, a CSE graduate. I've gained practical experience through five internships in backend.",
        "I'm currently open to new opportunities and available to join immediately! 2 years of experience.",
        "Hello Recruiters and Hiring Managers! Experienced IT Consultants Available for Your Hiring Needs.",
        "I’m seeking Software Engineering Internships and Full-Time Graduate Opportunities.",
    ],
)
def test_job_seekers_posts(text):
    assert is_seeker_post(text)


@pytest.mark.parametrize(
    "text",
    [
        "We're looking for a Java Developer to join our team in Bengaluru. Share your CV at jobs@acme.com",
        "I'm hiring for my team: Backend Engineer (2+ years), Bengaluru.",
        "Looking for an opportunity to build payments at scale? Acme is hiring SDE1s in Bangalore.",
    ],
)
def test_hiring_posts_are_not_seekers(text):
    assert not is_seeker_post(text)


@pytest.mark.parametrize(
    ("headline", "seeker"),
    [
        ("Open to work | Java Developer", True),
        ("Aspiring Software Engineer | CSE Student", True),
        ("Seeking new opportunities in backend", True),
        ("Hiring | Looking for Java developers | Talent Acquisition at Acme", False),
        ("Graduate Hiring Lead at Acme", False),
        ("Engineering Manager at Acme", False),
    ],
)
def test_seeker_headlines(headline, seeker):
    assert is_seeker_headline(headline) is seeker


@pytest.mark.parametrize(
    ("headline", "hiring"),
    [
        ("Talent Acquisition @ Acme", True),
        ("Senior Technical Recruiter at Example Labs", True),
        ("HR Business Partner | Acme", True),
        ("Co-founder & CTO at Example Labs", True),
        ("Engineering Manager at Acme", True),
        ("SDE2 at Acme, ex-Example", False),
        ("Software Engineer at Acme", False),
        ("Recruiter | Open to work", False),
        ("", False),
    ],
)
def test_hiring_headlines(headline, hiring):
    assert is_hiring_headline(headline) is hiring


@pytest.mark.parametrize(
    ("text", "company", "reshare"),
    [
        ("🚨 UBER IS HIRING — SDE 2! Uber is hiring for two engineering roles. Fill the form.", "Comviva", True),
        ("🚨 #𝐀𝐦𝐚𝐳𝐨𝐧 𝐢𝐬 #𝐇𝐢𝐫𝐢𝐧𝐠 | #𝐒𝐃𝐄 𝐈 | Location: #Remote (as claimed; verify official listing)", None, True),
        ("📢 Join WhatsApp Channel for more referrals & daily job updates. Special Hiring Alert!", "NextHire", True),
        ("Company: Acme. Role: SDE1.\nCompany: Example Labs. Role: Backend Engineer.", "Talent Hub", True),
        ("Acme Software is hiring SDE1 (Backend) in Bengaluru!", "Acme Software", False),
        ("Exciting news, Acme is hiring Java Developers in Bangalore.", "Acme", False),
        ("We're hiring! My team is hiring backend engineers in Bengaluru.", "Acme", False),
        ("We are hiring a Java Developer in Bengaluru.", None, False),
    ],
)
def test_job_alerts_and_reshares(text, company, reshare):
    assert is_reshare(text, company) is reshare


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("Location: Bangalore (Work From Office)", True),
        ("Bengaluru, Karnataka, India (Hybrid)", True),
        ("Greater Bengaluru Area (On-site)", True),
        ("Hiring SDE-2 | Golang | Remote (India)", True),
        ("Gurugram, Haryana, India (Remote)", True),
        ("India (Remote)", True),
        ("Remote", False),
        ("Location: Redmond, WA. Onsite.", False),
        ("SDE II - HYD || Immediate joiners only who are in HYD", False),
        ("Location: PAN India", False),
        ("", False),
    ],
)
def test_bengaluru_or_remote_india(text, ok):
    assert in_bengaluru_or_remote_india(text) is ok


def test_post_outreach_problem():
    hiring = "We're hiring a Backend Developer (1-3 years) in Bengaluru. Send your CV to jobs@acme.com"
    assert post_outreach_problem(hiring, "Talent Acquisition at Acme", "Acme") is None
    assert post_outreach_problem(hiring, "Software Engineer at Acme", "Acme") is None  # "we're hiring"
    assert post_outreach_problem(hiring, "Talent Acquisition at Acme", None) == "company"
    assert post_outreach_problem(hiring, "Talent Acquisition", None, need_company=False) is None  # an email
    plain = "Backend Developer (1-3 years), Bengaluru. CV: jobs@acme.com"
    assert post_outreach_problem(plain, "Software Engineer at Acme", "Acme") == "author"
    assert post_outreach_problem(plain, "HR at Acme", "Acme") is None
    assert post_outreach_problem(hiring.replace("Bengaluru", "Ho Chi Minh City"), "HR at Acme", "Acme") == "location"
    assert post_outreach_problem("#OpenToWork Backend Developer in Bengaluru", "Open to work", None) == "seeker"
    assert post_outreach_problem("Uber is hiring SDE2 in Bengaluru! Fill the form.", "SDE at Comviva", "Comviva") == "reshare"


def test_team_outreach_problem():
    assert team_outreach_problem("Bengaluru, Karnataka, India (On-site)", "Engineering Manager at Acme") is None
    assert team_outreach_problem("India (Remote)", "") is None
    assert team_outreach_problem("Remote", "Recruiter") == "location"
    assert team_outreach_problem("Chennai, Tamil Nadu, India (On-site)", "Recruiter") == "location"
    assert team_outreach_problem("Bengaluru", "Open to work | SDE") == "seeker"


def test_pick_contact_skips_job_seekers():
    seeker = HiringPerson("Asha Example", "https://www.linkedin.com/in/asha/", "Open to work | Java Developer", "2nd", True)
    recruiter = HiringPerson("Ravi Example", "https://www.linkedin.com/in/ravi/", "Recruiter at Acme", "3rd")
    assert pick_contact([seeker, recruiter]) is recruiter
    assert pick_contact([seeker]) is None


def test_posts_keep_only_hiring_authors_and_emails(client):
    ctx = make_ctx(client.app, NOW)
    matcher = RoleMatcher(ctx.prefs.roles)
    scanner = LinkedInPostsScanner()

    def judge(text, headline="Talent Acquisition at Acme Corp", author_url=AUTHOR):
        post = Post(urn="urn:li:activity:1", text=text, author="Asha Example", headline=headline, time_label="1h •",
                    author_url=author_url)
        return scanner.judge(post, ctx, matcher)

    email = "We're hiring Backend Developers (1-3 years) in Bengaluru. Share your CV at jobs@acme.com"
    job, why = judge(email)
    assert why == "" and job.contact_email == "jobs@acme.com"
    assert (job.contact_name, job.contact_profile_url) == ("Asha Example", AUTHOR)  # gets a note too
    # A job seeker's post is never kept, even with a form.
    assert judge("#OpenToWork Backend Developer, Bengaluru. My form: https://forms.gle/AbC123xyz",
                 headline="Open to work") == (None, "seeker")
    # An email-only job alert is left out; a Google Form alert is kept, without a note to its author.
    alert = "Uber is hiring Backend Developers (1-3 years) in Bengaluru! "
    assert judge(alert + "Mail jobs@uber.example.com", headline="SDE at Comviva") == (None, "reshare")
    form, why = judge(alert + "Apply: https://forms.gle/AbC123xyz", headline="SDE at Comviva")
    assert why == "" and form.form_url and form.contact_profile_url is None and form.contact_name is None
    # The author's company must be known for a note; the email still goes.
    job, _ = judge(email, headline="Talent Acquisition")
    assert job.contact_email and job.contact_profile_url is None
