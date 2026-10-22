"""LinkedIn connection notes: the hiring-team and post-author parsers, the profile flow with a
scripted fake profile, the ``linkedin_outreach`` scanner and the test-note endpoint. No real browser
or network; nothing is ever really sent."""

from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.models import (
    ApplyType,
    Job,
    JobStatus,
    OutreachKind,
    OutreachMessage,
    OutreachStatus,
    Platform,
    ScanKind,
    ScanRun,
    ScanStatus,
    TemplateKind,
)
from app.scanner.base import LoginWall
from app.services.app_settings import AUTO_SEND_LINKEDIN_NOTES, set_flag
from app.services.outreach import DraftProblem, note_templates
from app.sources.linkedin.easy_apply import remember_hiring_team
from app.sources.linkedin.outreach import (
    LinkedInOutreach,
    NoteResult,
    build_note,
    send_connection_note,
)
from app.sources.linkedin.parse import normalize_profile_url, parse_hiring_team, parse_posts, pick_contact, read_profile
from app.sources.linkedin.posts import LinkedInPostsScanner
from app.timeutils import local_day_bounds, utcnow
from tests.factories import make_job, make_message, make_template
from tests.fakes import FakeProfile, FakeSession, page
from tests.scan_helpers import connect_linkedin, make_ctx, ten_am_today

PRIYA = "https://www.linkedin.com/in/priya-sharma-1a2b3c/"
NOTE = "Hi Priya, I'm Harsh, an SDE with 1.9+ years of experience. I noticed openings at Acme that match my profile."

INTERESTED = (
    "Hi [Name], I’m Harsh Vaghela, an SDE at VerSe Innovation (Dailyhunt) with 1.9+ years of experience. I noticed "
    "openings at [Company] that match my profile. I’d be grateful if you could consider me for a relevant role. "
    "Looking forward to connecting!"
)
AFTER_EASY_APPLY = (
    "Hi, I have 1.9+ years of experience as an SDE and I'm looking for an opportunity. I've applied through Easy Apply "
    "for the position. It would be helpful if you could look into my profile and consider whether I'm a good fit."
)


def hiring_team_html(*people) -> str:
    """LinkedIn's "Meet the hiring team" card (people: (name, slug, degree, poster, headline))."""
    cards = "".join(
        f"""<div class="display-flex align-items-center mt4">
  <a href="https://www.linkedin.com/in/{slug}/" aria-label="{name}"><img alt="{name}" src="x.jpg"></a>
  <div class="hirer-card__hirer-information t-14">
    <a class="app-aware-link" href="https://www.linkedin.com/in/{slug}/?trk=job-poster">
      <span class="jobs-poster__name t-14 t-black mb0"><strong>{name}</strong></span></a>
    <span class="hirer-card__connection-degree t-black--light"> · {degree}</span>
    <div class="linked-area flex-1 cursor-pointer"><div class="text-body-small t-black">{headline}</div></div>
    {'<div class="hirer-card__job-poster text-body-small t-black--light">Job poster</div>' if poster else ''}
  </div>
  <button aria-label="Message {name}">Message</button>
</div>"""
        for name, slug, degree, poster, headline in people
    )
    return f"""<html><body><div class="jobs-unified-top-card"><h1>Backend Developer</h1></div>
<div class="job-details-people-who-can-help__section--two-pane artdeco-card">
  <h2 class="text-heading-medium">Meet the hiring team</h2>{cards}</div>
<div id="job-details"><p>Java, Spring Boot</p></div></body></html>"""


# ---------------------------------------------------------------------------
# Parsers


def test_normalize_profile_url():
    assert normalize_profile_url("https://in.linkedin.com/in/Priya-Sharma-1a2b3c?trk=x") == PRIYA
    assert normalize_profile_url("/in/priya-sharma-1a2b3c/details/") == PRIYA
    assert normalize_profile_url("www.linkedin.com/in/priya-sharma-1a2b3c") == PRIYA
    assert normalize_profile_url("https://www.linkedin.com/company/acme/") is None
    assert normalize_profile_url("https://evil.example.com/in/priya") is None
    assert normalize_profile_url("") is None


def test_parse_hiring_team():
    html = hiring_team_html(
        ("Vikram Rao", "vikram-rao", "1st", False, "Engineering Manager at Acme"),
        ("Priya Sharma (She/Her)", "priya-sharma-1a2b3c", "2nd", True, "Talent Acquisition at Acme Software"),
    )
    team = parse_hiring_team(html)
    assert [(p.name, p.profile_url, p.degree, p.job_poster) for p in team] == [
        ("Priya Sharma", PRIYA, "2nd", True),  # the job poster first
        ("Vikram Rao", "https://www.linkedin.com/in/vikram-rao/", "1st", False),
    ]
    assert team[0].headline == "Talent Acquisition at Acme Software"
    # The person to write to: never someone already connected (1st).
    assert pick_contact([team[1].as_dict()]) is None
    assert pick_contact([p.as_dict() for p in team]).profile_url == PRIYA
    assert parse_hiring_team("<html><body><h2>About the job</h2></body></html>") == []


def test_parse_hiring_team_unknown_markup_uses_the_heading():
    html = """<section><h2>Meet the hiring team</h2><div><ul><li>
      <a href="https://www.linkedin.com/in/anita-desai-99/"><span>Anita Desai</span></a></li></ul></div></section>"""
    (person,) = parse_hiring_team(html)
    assert (person.name, person.profile_url) == ("Anita Desai", "https://www.linkedin.com/in/anita-desai-99/")


def actor_post(urn: str, href: str, text: str) -> str:
    return f"""<div class="feed-shared-update-v2" data-urn="{urn}">
  <div class="update-components-actor"><div class="update-components-actor__container">
    <a class="update-components-actor__meta-link" href="{href}">
      <span class="update-components-actor__title"><span aria-hidden="true">Priya Sharma</span></span>
      <span class="update-components-actor__description"><span aria-hidden="true">Talent Acquisition at Acme Corp</span></span>
    </a>
    <span class="update-components-actor__sub-description"><span aria-hidden="true">1h • </span></span>
  </div></div>
  <div class="update-components-text"><span class="break-words">{text}</span>
    <a href="https://www.linkedin.com/in/someone-mentioned/">Someone Mentioned</a></div>
</div>"""


def test_post_author_profile(client):
    html = "<html><body>" + actor_post(
        "urn:li:activity:7390000000000000001",
        "https://www.linkedin.com/in/Priya-Sharma-1a2b3c?miniProfileUrn=urn%3Ali%3Afs_miniProfile%3AACo",
        "We're hiring Backend Developers (1-3 years) in Bangalore! Share your resume at jobs@acme.com",
    ) + actor_post(
        "urn:li:activity:7390000000000000002",
        "https://www.linkedin.com/company/acme-corp/posts",
        "Hiring Java Developer in Bengaluru, 2+ years. careers@acme.com",
    ) + "</body></html>"
    person, company = parse_posts(html)
    assert person.author_url == PRIYA and company.author_url is None
    scanner = LinkedInPostsScanner()
    from app.scanner.matching import RoleMatcher
    from tests.scan_helpers import PREFS

    ctx = make_ctx(client.app)
    matcher = RoleMatcher(PREFS.roles)
    candidate = scanner.candidate(person, ctx, matcher)
    assert (candidate.contact_name, candidate.contact_profile_url) == ("Priya Sharma", PRIYA)
    from_company = scanner.candidate(company, ctx, matcher)
    assert (from_company.contact_name, from_company.contact_profile_url) == (None, None)


def test_read_profile():
    profile = FakeProfile()
    view = read_profile(profile.html())
    assert view.name == "Priya Sharma" and not view.dialog and not view.pending
    assert view.buttons == ["Message", "Invite Priya Sharma to connect", "More actions"]  # not the sidebar's


# ---------------------------------------------------------------------------
# Sending one note (scripted profile)


def profile_browser(fake_browser, profile: FakeProfile, url: str = PRIYA) -> FakeSession:
    fake_browser.forms.append((url.removeprefix("https://www.linkedin.com"), profile))
    return FakeSession(fake_browser, None, True)


def test_connect_add_note_send(fake_browser):
    profile = FakeProfile()
    attempt = send_connection_note(profile_browser(fake_browser, profile), PRIYA, NOTE)
    assert attempt.result == NoteResult.sent and attempt.clicked_send
    assert profile.clicks == ["Invite Priya Sharma to connect", "Add a note", "Send invitation"]
    assert profile.note == NOTE and profile.sent and not profile.sent_without_note
    assert {"Add a note", "Send without a note", "Send invitation"} <= set(attempt.buttons)
    assert attempt.name == "Priya Sharma"


def test_more_then_connect(fake_browser):
    profile = FakeProfile(connect="more")
    attempt = send_connection_note(profile_browser(fake_browser, profile), PRIYA, NOTE)
    assert attempt.result == NoteResult.sent
    assert profile.clicks == ["More actions", "Invite Priya Sharma to connect", "Add a note", "Send invitation"]


@pytest.mark.parametrize("connect", [None, "pending"])
def test_no_connect_is_skipped(fake_browser, connect):
    profile = FakeProfile(connect=connect)
    attempt = send_connection_note(profile_browser(fake_browser, profile), PRIYA, NOTE)
    assert attempt.result == NoteResult.no_connect and not attempt.clicked_send
    assert FakeProfile.OTHER not in profile.clicks  # never someone else's Connect
    assert profile.clicks == ([] if connect else ["More actions", "More actions"])
    assert not profile.sent


@pytest.mark.parametrize(
    ("setup", "result", "clicked"),
    [
        ({"on_connect": "weekly_limit"}, NoteResult.weekly_limit, False),
        ({"on_send": "weekly_limit"}, NoteResult.weekly_limit, True),
        ({"on_add_note": "note_limit"}, NoteResult.note_limit, False),
        ({"on_connect": "email"}, NoteResult.email_required, False),
    ],
)
def test_limits_and_email_never_send_without_the_note(fake_browser, setup, result, clicked):
    profile = FakeProfile(**setup)
    attempt = send_connection_note(profile_browser(fake_browser, profile), PRIYA, NOTE)
    assert attempt.result == result and attempt.clicked_send is clicked
    assert "Send without a note" not in profile.clicks and not profile.sent
    assert profile.clicks[-1] == "Dismiss" and profile.state == "page"


def test_note_over_300_characters_is_skipped(fake_browser):
    profile = FakeProfile()
    attempt = send_connection_note(profile_browser(fake_browser, profile), PRIYA, "x" * 301)
    assert attempt.result == NoteResult.too_long and profile.clicks == []


def test_dry_run_fills_the_note_and_never_sends(fake_browser):
    profile = FakeProfile()
    attempt = send_connection_note(profile_browser(fake_browser, profile), PRIYA, lambda name: f"Hi {name.split()[0]}!", dry_run=True)
    assert attempt.result == NoteResult.would_send and attempt.note == "Hi Priya!"
    assert profile.fills == ["Hi Priya!"] and not profile.sent
    assert "Send invitation" not in profile.clicks and profile.clicks[-1] == "Dismiss"
    assert "Send invitation" in attempt.buttons


def test_short_note_box_and_send_errors(fake_browser):
    profile = FakeProfile(maxlength=200)
    attempt = send_connection_note(profile_browser(fake_browser, profile), PRIYA, "y" * 250)
    assert attempt.result == NoteResult.failed and "couldn't be filled in" in attempt.detail
    assert not profile.sent and "Send invitation" not in profile.clicks

    fake_browser.forms.clear()
    profile = FakeProfile(on_send="error")
    attempt = send_connection_note(profile_browser(fake_browser, profile), PRIYA, NOTE)
    assert attempt.result == NoteResult.failed and "invitation not sent" in attempt.detail and attempt.clicked_send

    fake_browser.forms.clear()
    profile = FakeProfile(on_connect="instant")
    attempt = send_connection_note(profile_browser(fake_browser, profile), PRIYA, NOTE)
    assert attempt.result == NoteResult.sent_without_note and "Add a note" not in profile.clicks


def test_login_wall_on_the_profile(fake_browser):
    fake_browser.routes = [("linkedin.com", page("https://www.linkedin.com/checkpoint/challenge/x"))]
    with pytest.raises(LoginWall, match="security check"):
        send_connection_note(FakeSession(fake_browser, None, True), PRIYA, NOTE)


# ---------------------------------------------------------------------------
# The note


def seed_templates(session):
    make_template(session, kind=TemplateKind.linkedin_message, question="SDE opportunity (InMail)",
                  answer="Hi, I’m Harsh. Please consider my profile.", subject="SDE Opportunity")
    make_template(session, kind=TemplateKind.linkedin_message, question="Alumni ask (DA-IICT senior)",
                  answer="Hi bhaiya, Harsh this side. I went through the opening at [Company].")
    make_template(session, kind=TemplateKind.linkedin_message, question="Interested in openings at [Company]", answer=INTERESTED)
    make_template(session, kind=TemplateKind.linkedin_message, question="After Easy Apply", answer=AFTER_EASY_APPLY)


def test_note_templates_and_choice(session):
    seed_templates(session)
    usable = [t.question for t in note_templates(session)]
    assert usable == ["Interested in openings at [Company]", "After Easy Apply"]  # no InMail, no alumni ask
    templates = note_templates(session)
    found = make_job(session, title="Backend Developer", company="Acme Software", apply_type=ApplyType.easy_apply, status=JobStatus.found)
    note = build_note(templates, found, "Dr. Priya Sharma")
    assert note.startswith("Hi Priya, I’m Harsh Vaghela") and "openings at Acme Software that match" in note
    applied = make_job(session, apply_type=ApplyType.easy_apply, status=JobStatus.applied)
    assert build_note(templates, applied, "Priya Sharma") == AFTER_EASY_APPLY
    emailed = make_job(session, apply_type=ApplyType.email, status=JobStatus.applied, company="LinkedIn post")
    assert "openings at your company" in build_note(templates, emailed, "Priya Sharma")  # not "Easy Apply"
    assert len(note) <= 300
    with pytest.raises(DraftProblem):
        build_note([], found, "Priya")


# ---------------------------------------------------------------------------
# The scanner


def li_job(session, n: int, priority: int, **extra) -> Job:
    values = dict(
        platform=Platform.linkedin, external_id=f"40200000{n:02d}", url=f"https://www.linkedin.com/jobs/view/40200000{n:02d}/",
        title="Backend Developer", company="Acme Software", apply_type=ApplyType.company_site, status=JobStatus.found,
        priority=priority, found_at=ten_am_today() - timedelta(hours=1),
    )
    values.update(extra)
    return make_job(session, **values)


def outreach(**extra) -> LinkedInOutreach:
    return LinkedInOutreach(rng=lambda low, high: low, **extra)


def test_off_by_default_and_not_connected(client):
    outcome = outreach().run(make_ctx(client.app))
    assert (outcome.status, outcome.message) == (ScanStatus.skipped, "Automatic LinkedIn notes are off.")
    with Session(client.app.state.engine) as session:
        set_flag(session, AUTO_SEND_LINKEDIN_NOTES, True)
    outcome = outreach().run(make_ctx(client.app))
    assert outcome.message == "There is no LinkedIn message template for connection notes, so no notes were sent."
    with Session(client.app.state.engine) as session:
        seed_templates(session)
        li_job(session, 1, priority=0, contact_name="Priya Sharma", contact_profile_url=PRIYA)
    outcome = outreach().run(make_ctx(client.app))
    assert (outcome.status, outcome.message) == (ScanStatus.skipped, "LinkedIn isn't connected, so no notes were sent.")


def test_sends_notes_to_post_authors_and_the_hiring_team(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    engine = client.app.state.engine
    rahul = "https://www.linkedin.com/in/rahul-mehta/"
    anita = "https://www.linkedin.com/in/anita-desai-99/"
    with Session(engine) as session:
        set_flag(session, AUTO_SEND_LINKEDIN_NOTES, True)
        seed_templates(session)
        post = li_job(session, 1, priority=0, apply_type=ApplyType.email, status=JobStatus.applied, company="Acme Corp",
                      title="Backend Developer (hiring post)", url="https://www.linkedin.com/feed/update/urn:li:activity:1/",
                      contact_name="Priya Sharma", contact_profile_url=PRIYA)
        easy = li_job(session, 2, priority=10, apply_type=ApplyType.easy_apply, status=JobStatus.applied,
                      contact_name="Rahul Mehta", contact_profile_url=rahul)
        team = li_job(session, 3, priority=20)  # no contact yet: its job page is checked
        li_job(session, 4, priority=5, contact_name="Deepak K", contact_profile_url="https://www.linkedin.com/in/deepak-k/")
        make_message(session, kind=OutreachKind.connection_note, subject=None, recipient_email=None,
                     recipient_profile_url="https://in.linkedin.com/in/Deepak-K?trk=x", status=OutreachStatus.rejected)
        li_job(session, 5, priority=1, status=JobStatus.skipped, contact_name="Skip Me",
               contact_profile_url="https://www.linkedin.com/in/skip-me/")
        # Older than the 7 days the notes look back (2026-10-21).
        li_job(session, 6, priority=1, found_at=ten_am_today() - timedelta(days=8), contact_name="Old",
               contact_profile_url="https://www.linkedin.com/in/old/")
        ids = {"post": post.id, "easy": easy.id, "team": team.id}
        team_url = team.url
    fake_browser.routes = [(team_url, page(team_url, hiring_team_html(
        ("Anita Desai", "anita-desai-99", "2nd", True, "Engineering Manager at Acme Software"),
    )))]
    profiles = {
        PRIYA: FakeProfile(),
        rahul: FakeProfile(name="Rahul Mehta", connect="more"),
        anita: FakeProfile(name="Anita Desai"),
    }
    for url, profile in profiles.items():
        fake_browser.forms.append((url.removeprefix("https://www.linkedin.com"), profile))
    sender = outreach()

    outcome = sender.run(make_ctx(client.app))

    assert (outcome.status, outcome.found, outcome.new) == (ScanStatus.ok, 3, 3)
    assert outcome.message == "Sent 3 connection notes. Checked 1 job page for the hiring team (1 to write to)."
    visited = [url for url in fake_browser.visited if "/in/" in url or "/jobs/view/" in url]
    assert visited == [team_url, PRIYA, rahul, anita], visited  # the job page first, then the profiles
    assert all(p.sent for p in profiles.values())
    assert profiles[PRIYA].note.startswith("Hi Priya, I’m Harsh Vaghela") and "openings at Acme Corp" in profiles[PRIYA].note
    assert profiles[rahul].note == AFTER_EASY_APPLY  # applied with Easy Apply
    assert profiles[anita].note.startswith("Hi Anita,") and "openings at Acme Software" in profiles[anita].note
    assert sender.pauses == [60.0, 60.0]  # between notes only
    with Session(engine) as session:
        rows = session.exec(
            select(OutreachMessage).where(OutreachMessage.status == OutreachStatus.sent).order_by(OutreachMessage.id)
        ).all()
        assert [(r.recipient_profile_url, r.job_id, r.kind) for r in rows] == [
            (PRIYA, ids["post"], OutreachKind.connection_note),
            (rahul, ids["easy"], OutreachKind.connection_note),
            (anita, ids["team"], OutreachKind.connection_note),
        ]
        assert rows[0].recipient_name == "Priya Sharma" and rows[0].body == profiles[PRIYA].note and rows[0].sent_at
        job = session.get(Job, ids["team"])
        assert (job.contact_name, job.contact_profile_url) == ("Anita Desai", anita) and job.contact_checked_at
        assert session.get(Job, ids["post"]).status == JobStatus.applied  # notes don't change the job
    dashboard = client.get("/api/dashboard/summary").json()
    assert dashboard["caps"]["connection_notes_month"]["used"] == 3

    # Everyone has been written to: the next run has nobody new and opens nothing.
    fake_browser.visited.clear()
    again = outreach().run(make_ctx(client.app))
    assert (again.status, again.message) == (ScanStatus.ok, "No new people to send a connection note to.")
    assert fake_browser.visited == []


def test_caps_skips_and_long_notes(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    engine = client.app.state.engine
    urls = {name: f"https://www.linkedin.com/in/{name}/" for name in ("connected", "fine", "later")}
    with Session(engine) as session:
        set_flag(session, AUTO_SEND_LINKEDIN_NOTES, True)
        seed_templates(session)
        li_job(session, 1, priority=0, company="A" * 120, contact_name="Long Company", contact_profile_url="https://www.linkedin.com/in/long/")
        li_job(session, 2, priority=1, contact_name="Already Connected", contact_profile_url=urls["connected"])
        li_job(session, 3, priority=2, contact_name="Fine Person", contact_profile_url=urls["fine"])
        li_job(session, 4, priority=3, contact_name="Later Person", contact_profile_url=urls["later"])
        start, _ = local_day_bounds(ten_am_today())
        for n in range(11):  # 11 of today's 12 already sent
            make_message(session, kind=OutreachKind.connection_note, subject=None, recipient_email=None, body="Hi",
                         recipient_profile_url=f"https://www.linkedin.com/in/earlier-{n}/", status=OutreachStatus.sent,
                         sent_at=start + timedelta(minutes=n))
    profiles = {
        "connected": FakeProfile(name="Already Connected", connect=None),
        "fine": FakeProfile(name="Fine Person"),
        "later": FakeProfile(name="Later Person"),
    }
    for name, profile in profiles.items():
        fake_browser.forms.append((f"/in/{name}/", profile))
    sender = outreach()
    outcome = sender.run(make_ctx(client.app))
    assert (outcome.found, outcome.new) == (2, 1)
    assert outcome.message == (
        "Sent 1 connection note, 1 skipped (no Connect button: already connected, pending or follow-only), "
        "1 skipped (note over 300 characters)."
    )
    assert profiles["fine"].sent and not profiles["later"].clicks and not profiles["connected"].sent
    assert sender.pauses == []  # nothing was sent before the last note
    with Session(engine) as session:
        skipped = session.exec(
            select(OutreachMessage).where(OutreachMessage.recipient_profile_url == urls["connected"])
        ).one()
        assert skipped.status == OutreachStatus.failed and skipped.error.startswith("Not sent: LinkedIn shows no Connect button")
        assert session.exec(
            select(OutreachMessage).where(OutreachMessage.recipient_profile_url == "https://www.linkedin.com/in/long/")
        ).first() is None
    again = outreach().run(make_ctx(client.app))
    assert (again.status, again.message) == (ScanStatus.skipped, "Today's limit of 12 connection notes is reached.")


def record_run(engine, outcome, now=None):
    with Session(engine) as session:
        session.add(ScanRun(kind=ScanKind.linkedin_outreach, status=outcome.status, message=outcome.message,
                            started_at=now or utcnow(), finished_at=utcnow()))
        session.commit()


def test_weekly_limit_stops_and_pauses(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    engine = client.app.state.engine
    with Session(engine) as session:
        set_flag(session, AUTO_SEND_LINKEDIN_NOTES, True)
        seed_templates(session)
        li_job(session, 1, priority=0, contact_name="Priya Sharma", contact_profile_url=PRIYA)
        li_job(session, 2, priority=1, contact_name="Next One", contact_profile_url="https://www.linkedin.com/in/next-one/")
    profile = FakeProfile(on_connect="weekly_limit")
    other = FakeProfile(name="Next One")
    fake_browser.forms += [("/in/priya-sharma-1a2b3c/", profile), ("/in/next-one/", other)]
    outcome = outreach().run(make_ctx(client.app))
    assert outcome.status == ScanStatus.ok
    assert outcome.message == "Sent 0 connection notes. Stopped: LinkedIn says the weekly invitation limit is reached; nothing was sent."
    assert not other.clicks and not profile.sent
    with Session(engine) as session:
        assert session.exec(select(OutreachMessage)).all() == []  # Priya is tried again after the pause
    record_run(engine, outcome)
    paused = outreach().run(make_ctx(client.app))
    assert paused.status == ScanStatus.skipped
    assert paused.message.startswith("Paused: LinkedIn said the weekly invitation limit was reached; the next try is after ")
    # A day later it tries again.
    record_run(engine, paused)
    with Session(engine) as session:
        for run in session.exec(select(ScanRun)).all():
            run.started_at = run.started_at - timedelta(hours=25)
            session.add(run)
        session.commit()
    profile.on_connect = "dialog"
    final = outreach().run(make_ctx(client.app, utcnow()))
    assert final.new == 2, final.message


def test_login_wall_marks_linkedin_disconnected(client, fake_browser):
    connect_linkedin(client.app, fake_browser)
    with Session(client.app.state.engine) as session:
        set_flag(session, AUTO_SEND_LINKEDIN_NOTES, True)
        seed_templates(session)
        li_job(session, 1, priority=0, contact_name="Priya Sharma", contact_profile_url=PRIYA)
    fake_browser.routes = [("linkedin.com", page("https://www.linkedin.com/uas/login?session_redirect=x"))]
    outcome = outreach().run(make_ctx(client.app))
    assert outcome.status == ScanStatus.failed
    assert outcome.message.endswith("Stopped: LinkedIn redirected to its login page; the saved login has expired.")
    linkedin = client.get("/api/connections").json()["items"][0]
    assert linkedin["status"] == "disconnected" and "expired" in linkedin["message"]


def test_easy_apply_remembers_the_hiring_team(session):
    job = make_job(session, platform=Platform.linkedin, apply_type=ApplyType.easy_apply, status=JobStatus.found)
    team = [p.as_dict() for p in parse_hiring_team(hiring_team_html(
        ("Vikram Rao", "vikram-rao", "1st", True, "EM"), ("Priya Sharma", "priya-sharma-1a2b3c", "3rd", False, "TA"),
    ))]
    remember_hiring_team(job, team)
    assert (job.contact_name, job.contact_profile_url) == ("Priya Sharma", PRIYA) and job.contact_checked_at


# ---------------------------------------------------------------------------
# The test-note endpoint


def test_test_note_endpoint(client, fake_browser):
    with Session(client.app.state.engine) as session:
        seed_templates(session)
        li_job(session, 1, priority=0, company="Acme Corp", contact_name="Priya Sharma", contact_profile_url=PRIYA)
    bad = client.post("/api/outreach/test-note", params={"profile_url": "https://www.linkedin.com/company/acme/"})
    assert bad.status_code == 422
    response = client.post("/api/outreach/test-note", params={"profile_url": PRIYA})
    assert response.status_code == 409 and response.json()["detail"] == "LinkedIn isn't connected. Connect it first."

    connect_linkedin(client.app, fake_browser)
    profile = FakeProfile()
    fake_browser.forms.append(("/in/priya-sharma-1a2b3c/", profile))
    dry = client.post("/api/outreach/test-note", params={"profile_url": "https://in.linkedin.com/in/priya-sharma-1a2b3c?x=1"})
    assert dry.status_code == 200
    body = dry.json()
    assert body["result"] == "would_send" and body["profile_url"] == PRIYA and body["name"] == "Priya Sharma"
    assert body["note"].startswith("Hi Priya, I’m Harsh Vaghela") and "openings at Acme Corp" in body["note"]
    assert {"Invite Priya Sharma to connect", "Add a note", "Send invitation"} <= set(body["buttons_found"])
    assert not profile.sent and "Send invitation" not in profile.clicks
    assert client.get("/api/outreach").json()["total"] == 0  # a dry run records nothing

    sent = client.post("/api/outreach/test-note", params={"profile_url": PRIYA, "dry_run": False}).json()
    assert sent["result"] == "sent" and profile.sent and profile.note == body["note"]
    (row,) = client.get("/api/outreach").json()["items"]
    assert (row["kind"], row["status"], row["recipient_profile_url"]) == ("connection_note", "sent", PRIYA)
    again = client.post("/api/outreach/test-note", params={"profile_url": PRIYA, "dry_run": False})
    assert again.status_code == 409 and "never sent twice" in again.json()["detail"]


# ---------------------------------------------------------------------------
# The 2026-10-15 profile layout (saved page): no top card, no <h1>; Connect is a link to the
# invitation page of that person; other people's Connect links sit in "More profiles for you"; the
# search box's suggestions are a role="dialog" popover. People are made up.

NEW_LAYOUT = """<html><head><title>(2) Asha Example | LinkedIn</title></head><body>
<div role="dialog" data-testid="popover-floating"><input data-testid="typeahead-input"></div>
<main><section><p>Asha Example</p><p>Talent Acquisition @ Acme</p>
 <button type="button" aria-expanded="false">More</button>
 <a href="/messaging/compose/?profileUrn=x">Message</a>
 <a href="/preload/custom-invite/?vanityName=asha-example-12ab" aria-label="Invite Asha Example to connect">Connect</a>
</section>
<aside><a href="/preload/custom-invite/?vanityName=someone-else" aria-label="Invite Someone Else to connect">Connect</a>
 <button aria-label="Follow Someone Else">Follow</button></aside></main>
<dialog><button aria-label="Dismiss"></button>Ad Options</dialog>
</body></html>"""


def test_new_layout_profile_reads_its_own_connect_link():
    from app.sources.linkedin.parse import profile_slug, read_profile

    slug = profile_slug("https://www.linkedin.com/in/Asha-Example-12ab/?trk=x")
    assert slug == "asha-example-12ab"
    view = read_profile(NEW_LAYOUT, slug)
    assert view.new_layout and view.name == "Asha Example"
    assert view.connect_href == "https://www.linkedin.com/preload/custom-invite/?vanityName=asha-example-12ab"
    assert "Invite Asha Example to connect" in view.buttons and "Invite Someone Else to connect" not in view.buttons
    assert not view.dialog  # the search popover and a closed <dialog> aren't the invitation
    assert not view.pending

    other = read_profile(NEW_LAYOUT, "nobody-here")
    assert other.connect_href is None  # never another person's link

    pending = NEW_LAYOUT.replace(
        '<a href="/preload/custom-invite/?vanityName=asha-example-12ab" aria-label="Invite Asha Example to connect">Connect</a>',
        '<button aria-label="Pending, click to withdraw invitation sent to Asha Example">Pending</button>',
    )
    assert read_profile(pending, slug).pending


def test_people_blocked_by_the_layout_bug_are_tried_once_more(session):
    from datetime import datetime, timezone

    from app.models import OutreachKind, OutreachMessage, OutreachStatus
    from app.sources.linkedin.outreach import NO_CONNECT, contacted_profiles

    def note(url, error, created):
        session.add(OutreachMessage(kind=OutreachKind.connection_note, status=OutreachStatus.failed, body="Hi", recipient_name="Test Person",
                                    recipient_profile_url=url, error=error, created_at=created))

    before = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
    after = datetime(2026, 10, 15, 13, 0, tzinfo=timezone.utc)
    note("https://www.linkedin.com/in/blocked-by-bug/", f"Not sent: {NO_CONNECT}", before)
    note("https://www.linkedin.com/in/really-no-connect/", f"Not sent: {NO_CONNECT}", after)
    note("https://www.linkedin.com/in/other-failure/", "Not sent: The note couldn't be filled in.", before)
    # The invitation in LinkedIn's hidden frame (fixed 2026-10-21 12:30 UTC): tried once more too.
    frame_before = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
    frame_after = datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc)
    note("https://www.linkedin.com/in/hidden-frame/", "The invitation dialog didn't open after Connect.", frame_before)
    note("https://www.linkedin.com/in/note-box/", "The note box didn't open; nothing was sent.", frame_before)
    note("https://www.linkedin.com/in/frame-after-fix/", "The invitation dialog didn't open after Connect.", frame_after)
    session.commit()
    contacted = contacted_profiles(session)
    retried = {"https://www.linkedin.com/in/blocked-by-bug/", "https://www.linkedin.com/in/hidden-frame/",
               "https://www.linkedin.com/in/note-box/"}
    assert not (retried & contacted)
    assert {"https://www.linkedin.com/in/really-no-connect/", "https://www.linkedin.com/in/other-failure/",
            "https://www.linkedin.com/in/frame-after-fix/"} <= contacted
