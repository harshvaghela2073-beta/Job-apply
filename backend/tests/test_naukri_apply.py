import pytest
from sqlmodel import Session, select

from app.models import ApplyType, Job, JobStatus, Platform, ScanStatus, UnansweredQuestion
from app.scanner.base import LoginWall
from app.services.app_settings import AUTO_APPLY_NAUKRI, set_flag
from app.sources.naukri.apply import ApplyResult, NaukriApplier, apply_on_page
from tests.factories import make_job
from tests.fakes import FakeSession, page
from tests.scan_helpers import make_ctx
from tests.test_naukri import connect_naukri

JOB_URL = "https://www.naukri.com/job-listings-sde-acme-bengaluru-1-to-3-years-111"
OUTREACH_OFF = {
    "auto_send_email": False, "auto_send_linkedin_notes": False, "pause_naukri": False,
    "pause_linkedin": False, "pause_linkedin_jobs": False, "pause_linkedin_posts": False, "pause_naukri_jobs": False,
    "linkedin_scan_interval_minutes": 30, "naukri_scan_interval_minutes": 30,
    "linkedin_search_interval_minutes": 60,
}
APPLY_PAGE = '<html><body><button id="apply-button">Apply</button></body></html>'
COMPANY_PAGE = '<html><body><button id="company-site-button">Apply on company site</button></body></html>'
APPLIED_PAGE = '<html><body><button id="apply-button">Applied</button></body></html>'
SUCCESS_PAGE = "<html><body><p>You have successfully applied to Software Engineer</p></body></html>"
QUESTIONS_PAGE = """<html><body><div class="chatbot_DrawerContentWrapper">
  <p>Kindly answer all the recruiter's questions to successfully apply for the job.</p>
  <ul><li class="botItem"><div class="botMsg"><span>What is your notice period?</span></div></li>
  <li class="botItem"><div class="botMsg"><span>Are you willing to relocate to Pune?</span></div></li></ul>
</div></body></html>"""
QUOTA_PAGE = "<html><body><p>You have reached the daily quota of applies.</p></body></html>"


def session_for(fake_browser) -> FakeSession:
    return FakeSession(fake_browser, None, True)


def test_one_click_apply_succeeds(fake_browser):
    fake_browser.routes = [("job-listings", page(JOB_URL, APPLY_PAGE))]
    fake_browser.click_routes = [("job-listings", page(JOB_URL, SUCCESS_PAGE))]
    attempt = apply_on_page(session_for(fake_browser), JOB_URL)
    assert attempt.result == ApplyResult.applied
    assert fake_browser.clicks == [(JOB_URL, "#apply-button, button[class*='apply-button']")]


def test_button_turning_into_applied_counts_as_success(fake_browser):
    fake_browser.routes = [("job-listings", page(JOB_URL, APPLY_PAGE))]
    fake_browser.click_routes = [("job-listings", page(JOB_URL, APPLIED_PAGE))]
    assert apply_on_page(session_for(fake_browser), JOB_URL).result == ApplyResult.applied


def test_recruiter_questions_are_not_answered(fake_browser):
    fake_browser.routes = [("job-listings", page(JOB_URL, APPLY_PAGE))]
    fake_browser.click_routes = [("job-listings", page(JOB_URL, QUESTIONS_PAGE))]
    attempt = apply_on_page(session_for(fake_browser), JOB_URL)
    assert attempt.result == ApplyResult.questionnaire
    assert attempt.questions == ["What is your notice period?", "Are you willing to relocate to Pune?"]
    assert fake_browser.screenshots  # saved for a look at the drawer


def test_company_site_job_is_not_clicked(fake_browser):
    fake_browser.routes = [("job-listings", page(JOB_URL, COMPANY_PAGE))]
    attempt = apply_on_page(session_for(fake_browser), JOB_URL)
    assert attempt.result == ApplyResult.company_site and fake_browser.clicks == []


def test_already_applied(fake_browser):
    fake_browser.routes = [("job-listings", page(JOB_URL, APPLIED_PAGE))]
    assert apply_on_page(session_for(fake_browser), JOB_URL).result == ApplyResult.already_applied


def test_dry_run_never_clicks(fake_browser):
    fake_browser.routes = [("job-listings", page(JOB_URL, APPLY_PAGE))]
    fake_browser.click_routes = [("job-listings", page(JOB_URL, SUCCESS_PAGE))]
    attempt = apply_on_page(session_for(fake_browser), JOB_URL, dry_run=True)
    assert attempt.result == ApplyResult.would_apply and fake_browser.clicks == []


def test_no_button_and_no_confirmation(fake_browser):
    fake_browser.routes = [("job-listings", page(JOB_URL, "<html><body>Job closed</body></html>"))]
    assert apply_on_page(session_for(fake_browser), JOB_URL).result == ApplyResult.no_button

    fake_browser.routes = [("job-listings", page(JOB_URL, APPLY_PAGE))]
    fake_browser.click_routes = [("job-listings", page(JOB_URL, APPLY_PAGE))]  # nothing changes
    assert apply_on_page(session_for(fake_browser), JOB_URL).result == ApplyResult.no_confirmation


def test_quota_and_login_wall(fake_browser):
    fake_browser.routes = [("job-listings", page(JOB_URL, APPLY_PAGE))]
    fake_browser.click_routes = [("job-listings", page(JOB_URL, QUOTA_PAGE))]
    assert apply_on_page(session_for(fake_browser), JOB_URL).result == ApplyResult.quota_reached

    fake_browser.routes = [("job-listings", page("https://www.naukri.com/nlogin/login", "<html></html>"))]
    with pytest.raises(LoginWall):
        apply_on_page(session_for(fake_browser), JOB_URL)


def one_click_job(session, n: int, priority: int) -> Job:
    return make_job(
        session,
        platform=Platform.naukri,
        external_id=f"nk-{n}",
        url=f"https://www.naukri.com/job-listings-role-{n}",
        apply_type=ApplyType.one_click,
        status=JobStatus.found,
        priority=priority,
    )


def test_applier_is_off_by_default(client):
    outcome = NaukriApplier().run(make_ctx(client.app))
    assert (outcome.status, outcome.message) == (ScanStatus.skipped, "Auto-apply is off.")


def test_applier_applies_in_priority_order_and_records_outcomes(client, settings, fake_browser):
    connect_naukri(client, settings)
    engine = client.app.state.engine
    with Session(engine) as session:
        set_flag(session, AUTO_APPLY_NAUKRI, True)
        low = one_click_job(session, 1, priority=140)
        high = one_click_job(session, 2, priority=0)
        questions = one_click_job(session, 3, priority=20)
        ids = (low.id, high.id, questions.id)
    fake_browser.routes = [("job-listings", page("https://www.naukri.com/job-listings", APPLY_PAGE))]
    fake_browser.routes = [(f"role-{n}", page(f"https://www.naukri.com/job-listings-role-{n}", APPLY_PAGE)) for n in (1, 2, 3)]
    fake_browser.click_routes = [
        ("role-3", page("https://www.naukri.com/job-listings-role-3", QUESTIONS_PAGE)),
        ("job-listings", page("https://www.naukri.com/job-listings-x", SUCCESS_PAGE)),
    ]

    outcome = NaukriApplier().run(make_ctx(client.app))

    assert [url for url, _ in fake_browser.clicks] == [
        "https://www.naukri.com/job-listings-role-2",  # priority 0
        "https://www.naukri.com/job-listings-role-3",  # priority 20
        "https://www.naukri.com/job-listings-role-1",  # priority 140
    ]
    assert (outcome.status, outcome.found, outcome.new) == (ScanStatus.ok, 3, 2)
    assert outcome.message == "Applied to 2, 1 need your answers (recruiter questions)."
    with Session(engine) as session:
        low, high, asked = (session.get(Job, i) for i in ids)
        assert low.status == high.status == JobStatus.applied and high.applied_at is not None
        assert asked.status == JobStatus.needs_manual and "What is your notice period?" in asked.status_reason
        stored = [q.question for q in session.exec(select(UnansweredQuestion)).all()]
        assert stored == ["What is your notice period?", "Are you willing to relocate to Pune?"]


def test_applier_respects_the_daily_cap(client, settings, fake_browser):
    connect_naukri(client, settings)
    ctx = make_ctx(client.app)
    ctx.settings = ctx.settings.model_copy(update={"naukri_daily_cap": 1})
    with Session(client.app.state.engine) as session:
        set_flag(session, AUTO_APPLY_NAUKRI, True)
        one_click_job(session, 1, priority=0)
        one_click_job(session, 2, priority=10)
    fake_browser.routes = [("job-listings", page("https://www.naukri.com/job-listings", APPLY_PAGE))]
    fake_browser.click_routes = [("job-listings", page("https://www.naukri.com/job-listings", SUCCESS_PAGE))]

    assert NaukriApplier().run(ctx).new == 1
    second = NaukriApplier().run(ctx)
    assert second.status == ScanStatus.skipped and "limit of 1" in second.message


def test_settings_endpoint(client):
    assert client.get("/api/settings").json() == {"auto_apply_naukri": False, "auto_apply_linkedin": False, **OUTREACH_OFF}
    assert client.put("/api/settings", json={"auto_apply_naukri": True}).json()["auto_apply_naukri"] is True
    assert client.get("/api/settings").json() == {"auto_apply_naukri": True, "auto_apply_linkedin": False, **OUTREACH_OFF}
    assert client.put("/api/settings", json={}).json() == {"auto_apply_naukri": True, "auto_apply_linkedin": False, **OUTREACH_OFF}


def test_apply_now_endpoint(client, settings, fake_browser):
    with Session(client.app.state.engine) as session:
        job = one_click_job(session, 9, priority=0)
        other = make_job(session)  # a LinkedIn company-site job
        job_id, other_id = job.id, other.id
    assert client.post(f"/api/jobs/{other_id}/apply").status_code == 422
    assert client.post(f"/api/jobs/{job_id}/apply").status_code == 409  # not connected

    connect_naukri(client, settings)
    fake_browser.routes = [("job-listings", page("https://www.naukri.com/job-listings-role-9", APPLY_PAGE))]
    fake_browser.click_routes = [("job-listings", page("https://www.naukri.com/job-listings-role-9", SUCCESS_PAGE))]

    dry = client.post(f"/api/jobs/{job_id}/apply", params={"dry_run": True}).json()
    assert dry["result"] == "would_apply" and dry["job"]["status"] == "found" and fake_browser.clicks == []

    done = client.post(f"/api/jobs/{job_id}/apply").json()
    assert done["result"] == "applied" and done["job"]["status"] == "applied"
    assert client.post(f"/api/jobs/{job_id}/apply").status_code == 409  # already applied
