"""The pause switches (2026-10-15): a paused scanner records "skipped" and opens no browser; applying
to one job from the app still works."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models import ScanKind
from app.scheduler.runner import GROUPS
from tests.conftest import make_app
from tests.fakes import api, page
from tests.scan_helpers import SEARCH_FIELDS, connect_linkedin
from tests.test_naukri import connect_naukri, nk_job, nk_json
from tests.test_scans import FakeScanner

NEW_FLAGS = ("pause_linkedin", "pause_linkedin_jobs", "pause_linkedin_posts", "pause_naukri_jobs")
ALL_PAUSES = {"pause_naukri": True, **{flag: True for flag in NEW_FLAGS}}
LINKEDIN_KINDS = (
    ScanKind.linkedin_jobs, ScanKind.linkedin_apply, ScanKind.linkedin_posts, ScanKind.email_send,
    ScanKind.linkedin_outreach,
)


@pytest.fixture
def seeded_settings(settings, fake_browser):
    """Settings for a database that already has the search_* profile fields."""
    with TestClient(make_app(settings, fake_browser)) as client:
        assert client.put("/api/profile/fields", json={"fields": SEARCH_FIELDS}).status_code == 200
    return settings


def run_scans(app, client) -> dict[str, dict]:
    assert client.post("/api/scans/run").status_code == 202
    assert app.state.scans.wait(10)
    return {r["kind"]: r for r in client.get("/api/scans/status").json()["latest"]}


# ---------------------------------------------------------------------------
# Settings


def test_pause_flags_default_off_and_save(client):
    data = client.get("/api/settings").json()
    assert all(data[flag] is False for flag in NEW_FLAGS)
    for flag in NEW_FLAGS:
        body = client.put("/api/settings", json={flag: True}).json()
        assert body[flag] is True
    data = client.get("/api/settings").json()
    assert all(data[flag] is True for flag in NEW_FLAGS)
    assert data["pause_naukri"] is False  # the other switches are untouched
    assert client.put("/api/settings", json={}).json() == data  # left out = unchanged
    body = client.put("/api/settings", json={"pause_linkedin_posts": False}).json()
    assert body["pause_linkedin_posts"] is False and body["pause_linkedin"] is True
    assert client.put("/api/settings", json={"pause_linkedin": "maybe"}).status_code == 422


# ---------------------------------------------------------------------------
# The runner's gate


@pytest.mark.parametrize(
    "flag, paused",
    [
        ("pause_linkedin", {kind: "LinkedIn is paused." for kind in LINKEDIN_KINDS}),
        ("pause_linkedin_jobs", {ScanKind.linkedin_jobs: "The LinkedIn job search is paused."}),
        ("pause_linkedin_posts", {ScanKind.linkedin_posts: "The hiring-post search is paused."}),
        ("pause_naukri_jobs", {ScanKind.naukri_jobs: "The Naukri search is paused."}),
        ("pause_naukri", {ScanKind.naukri_jobs: "Naukri is paused.", ScanKind.naukri_apply: "Naukri is paused."}),
    ],
)
def test_each_pause_skips_its_scanners(seeded_settings, fake_browser, flag, paused):
    scanners = [FakeScanner(kind) for kind in ScanKind]
    app = make_app(seeded_settings, fake_browser, scanners=scanners)
    with TestClient(app) as client:
        assert client.put("/api/settings", json={flag: True}).json()[flag] is True
        latest = run_scans(app, client)
        runs = client.get("/api/scans").json()["items"]
    order = [r["kind"] for r in reversed(runs)]
    assert sorted(order) == sorted(ScanKind)
    for kinds in GROUPS.values():  # each group's order is unchanged (the groups run side by side)
        assert [kind for kind in order if kind in kinds] == list(kinds)
    for scanner in scanners:
        run = latest[scanner.kind]
        if scanner.kind in paused:
            assert (run["status"], run["message"], run["found"]) == ("skipped", paused[scanner.kind], 0)
            assert scanner.contexts == []  # never started
        else:
            assert (run["status"], run["message"]) == ("ok", None)
            assert len(scanner.contexts) == 1


def test_platform_pause_wins_and_needs_no_search_fields(settings, fake_browser):
    """The platform's message comes first, and a paused scanner is skipped even before the search
    fields are filled in (an unpaused one fails for them)."""
    scanners = [FakeScanner(ScanKind.linkedin_jobs), FakeScanner(ScanKind.naukri_jobs)]
    app = make_app(settings, fake_browser, scanners=scanners)
    with TestClient(app) as client:
        client.put("/api/settings", json={"pause_linkedin": True, "pause_linkedin_jobs": True})
        latest = run_scans(app, client)
    assert (latest["linkedin_jobs"]["status"], latest["linkedin_jobs"]["message"]) == ("skipped", "LinkedIn is paused.")
    assert latest["naukri_jobs"]["status"] == "failed" and "search_experience_order" in latest["naukri_jobs"]["message"]
    assert [len(s.contexts) for s in scanners] == [0, 0]


def test_paused_real_scanners_open_no_browser(seeded_settings, fake_browser):
    fake_browser.routes = [
        ("naukri.com", lambda url: page(url, "", captured=[api("https://www.naukri.com/jobapi/v3/search", nk_json(nk_job("n1", "Backend Developer")))])),
    ]
    app = make_app(seeded_settings, fake_browser)
    with TestClient(app) as client:
        connect_linkedin(app, fake_browser)
        connect_naukri(client, seeded_settings)
        client.put("/api/settings", json={"auto_apply_linkedin": True, "auto_apply_naukri": True, **ALL_PAUSES})
        latest = run_scans(app, client)
        assert {kind: r["status"] for kind, r in latest.items()} == {kind.value: "skipped" for kind in ScanKind}
        assert latest["linkedin_outreach"]["message"] == "LinkedIn is paused."
        assert latest["naukri_apply"]["message"] == "Naukri is paused."
        assert fake_browser.opened == [] and fake_browser.visited == []
        assert client.get("/api/jobs").json()["total"] == 0

        # Unpaused, the same run opens the browser and stores the Naukri job.
        client.put("/api/settings", json={flag: False for flag in ALL_PAUSES})
        latest = run_scans(app, client)
        assert latest["naukri_jobs"]["status"] == "ok" and latest["naukri_jobs"]["new"] == 1
        assert fake_browser.opened != []


# ---------------------------------------------------------------------------
# Applying from the app while paused


def test_naukri_apply_from_app_while_paused(client, settings, fake_browser):
    from tests.test_naukri_apply import APPLY_PAGE, SUCCESS_PAGE, one_click_job

    with Session(client.app.state.engine) as session:
        job_id = one_click_job(session, 9, priority=0).id
    connect_naukri(client, settings)
    client.put("/api/settings", json=ALL_PAUSES)
    fake_browser.routes = [("job-listings", page("https://www.naukri.com/job-listings-role-9", APPLY_PAGE))]
    fake_browser.click_routes = [("job-listings", page("https://www.naukri.com/job-listings-role-9", SUCCESS_PAGE))]
    done = client.post(f"/api/jobs/{job_id}/apply").json()
    assert done["result"] == "applied" and done["job"]["status"] == "applied"


def test_linkedin_apply_from_app_while_paused(client, fake_browser):
    from tests.test_linkedin_easy_apply import easy_job, scripted, seed_profile

    with Session(client.app.state.engine) as session:
        seed_profile(session)
        job = easy_job(session, 1, priority=0)
        job_id, url = job.id, job.url
    form = scripted(fake_browser, url=url)
    connect_linkedin(client.app, fake_browser)
    client.put("/api/settings", json=ALL_PAUSES)
    done = client.post(f"/api/jobs/{job_id}/apply").json()
    assert done["result"] == "applied" and done["job"]["status"] == "applied" and form.submitted
