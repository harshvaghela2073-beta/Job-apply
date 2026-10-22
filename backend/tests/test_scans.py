import json
import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.browser.driver import ApiResponse
from app.models import ApplyType, Job, JobStatus, Platform, ScanKind, ScanRun, ScanStatus
from app.scanner.base import ScanOutcome
from app.scheduler.loop import initial_next_run
from app.scheduler.runner import GROUP_OF, GROUPS
from app.timeutils import utcnow
from tests.conftest import make_app
from tests.fakes import api, page
from tests.scan_helpers import SEARCH_FIELDS, epoch_ms
from tests.test_naukri import nk_job, nk_json

INTERVAL = timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Next-run logic


def test_initial_next_run():
    now = utcnow()
    # No run yet, or the last one is older than the interval: one minute after startup.
    assert initial_next_run(None, now, INTERVAL) == now + timedelta(minutes=1)
    assert initial_next_run(now - timedelta(minutes=45), now, INTERVAL) == now + timedelta(minutes=1)
    # A recent run: the interval after it finished, so restarts don't trigger extra runs.
    assert initial_next_run(now - timedelta(minutes=10), now, INTERVAL) == now + timedelta(minutes=20)
    assert initial_next_run(now - timedelta(seconds=5), now, INTERVAL) == now + timedelta(minutes=29, seconds=55)


# ---------------------------------------------------------------------------
# Fake scanners


class FakeScanner:
    def __init__(self, kind, outcome=None, gate: threading.Event | None = None, error=None):
        self.kind = kind
        self.outcome = outcome or ScanOutcome(status=ScanStatus.ok, found=2, new=1)
        self.gate = gate
        self.error = error
        self.contexts = []
        self.started = threading.Event()  # set once run() has begun

    def run(self, ctx):
        self.contexts.append(ctx)
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return self.outcome


def fake_scanners(gate=None):
    return [
        FakeScanner(ScanKind.naukri_jobs, gate=gate),
        FakeScanner(ScanKind.linkedin_jobs),
        FakeScanner(ScanKind.linkedin_posts, ScanOutcome(status=ScanStatus.skipped, message="LinkedIn isn't connected.")),
    ]


@pytest.fixture
def seeded_settings(settings, fake_browser):
    """Settings for a database that already has the search_* profile fields."""
    with TestClient(make_app(settings, fake_browser)) as client:
        assert client.put("/api/profile/fields", json={"fields": SEARCH_FIELDS}).status_code == 200
    return settings


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


IDLE_GROUP = {"running": False, "interval_minutes": 30, "next_run_at": None, "last_finished_at": None}


def test_status_when_disabled(client):
    assert client.get("/api/scans/status").json() == {
        "enabled": False, "running": False, "next_run_at": None, "interval_minutes": 30,
        "platforms": {"linkedin": IDLE_GROUP, "naukri": IDLE_GROUP},
        "latest": [],
    }
    assert client.get("/api/scans").json() == {"items": [], "total": 0}


def test_groups():
    assert GROUPS == {
        Platform.linkedin: (ScanKind.linkedin_jobs, ScanKind.linkedin_apply, ScanKind.linkedin_posts,
                            ScanKind.email_send, ScanKind.linkedin_outreach),
        Platform.naukri: (ScanKind.naukri_jobs, ScanKind.naukri_apply),
    }
    assert sorted(GROUP_OF) == sorted(ScanKind)  # every scanner belongs to exactly one group


def test_run_now_per_platform_409_while_running_then_records_runs(seeded_settings, fake_browser):
    gate = threading.Event()
    scanners = fake_scanners(gate)  # naukri_jobs waits for the gate
    app = make_app(seeded_settings, fake_browser, scanners=scanners)
    scans = app.state.scans
    with TestClient(app) as client:
        assert client.post("/api/scans/run", params={"platform": "bing"}).status_code == 422
        response = client.post("/api/scans/run", params={"platform": "naukri"})
        assert response.status_code == 202 and response.json() == {"started": True, "platforms": ["naukri"]}
        status = client.get("/api/scans/status").json()
        assert status["running"] is True
        assert status["platforms"]["naukri"]["running"] is True and status["platforms"]["linkedin"]["running"] is False
        conflict = client.post("/api/scans/run", params={"platform": "naukri"})
        assert conflict.status_code == 409 and conflict.json()["detail"] == "The Naukri scan is already running."
        running = client.get("/api/scans").json()["items"][0]
        assert running["kind"] == "naukri_jobs" and running["status"] == "running" and running["finished_at"] is None

        # LinkedIn runs on its own while the Naukri run is still going.
        response = client.post("/api/scans/run", params={"platform": "linkedin"})
        assert response.status_code == 202 and response.json() == {"started": True, "platforms": ["linkedin"]}
        assert wait_until(lambda: not scans.running_for(Platform.linkedin))
        assert scans.running_for(Platform.naukri) and scans.running
        status = client.get("/api/scans/status").json()
        assert status["platforms"]["linkedin"]["last_finished_at"] is not None
        assert status["platforms"]["naukri"]["last_finished_at"] is None
        assert [(r["kind"], r["status"]) for r in status["latest"]] == [
            ("linkedin_jobs", "ok"), ("linkedin_posts", "skipped"), ("naukri_jobs", "running"),
        ]
        # Without a platform: only the group that isn't running starts.
        response = client.post("/api/scans/run")
        assert response.status_code == 202 and response.json()["platforms"] == ["linkedin"]
        assert wait_until(lambda: not scans.running_for(Platform.linkedin))

        gate.set()
        assert scans.wait(5)
        runs = client.get("/api/scans").json()
        assert runs["total"] == 5
        naukri = next(r for r in runs["items"] if r["kind"] == "naukri_jobs")
        assert (naukri["status"], naukri["found"], naukri["new"], naukri["message"]) == ("ok", 2, 1, None)
        assert naukri["finished_at"] >= naukri["started_at"]
        posts = runs["items"][0]  # the second LinkedIn run started last
        assert (posts["kind"], posts["status"], posts["message"]) == ("linkedin_posts", "skipped", "LinkedIn isn't connected.")

        status = client.get("/api/scans/status").json()
        assert status["running"] is False and status["platforms"]["naukri"]["running"] is False
        assert [r["kind"] for r in status["latest"]] == ["linkedin_jobs", "linkedin_posts", "naukri_jobs"]  # ScanKind order
        assert status["platforms"]["naukri"]["last_finished_at"] == naukri["finished_at"]
        # The search preferences come from the profile fields.
        assert scanners[0].contexts[0].prefs.locations == ("Bengaluru", "Remote")
        assert all(ctx.manual for scanner in scanners for ctx in scanner.contexts)  # "Run now"

        response = client.post("/api/scans/run")  # both groups start again
        assert response.status_code == 202 and response.json() == {"started": True, "platforms": ["linkedin", "naukri"]}
        assert scans.wait(5)
        assert client.get("/api/scans", params={"limit": 2}).json()["total"] == 8


def test_groups_run_side_by_side(seeded_settings, fake_browser):
    linkedin_gate, naukri_gate = threading.Event(), threading.Event()
    linkedin = FakeScanner(ScanKind.linkedin_jobs, gate=linkedin_gate)
    naukri = FakeScanner(ScanKind.naukri_jobs, gate=naukri_gate)
    app = make_app(seeded_settings, fake_browser, scanners=[linkedin, naukri])
    scans = app.state.scans
    with TestClient(app) as client:
        response = client.post("/api/scans/run")
        assert response.status_code == 202 and response.json() == {"started": True, "platforms": ["linkedin", "naukri"]}
        # Both scanners are inside run() at the same time.
        assert linkedin.started.wait(1) and naukri.started.wait(1)
        both = client.post("/api/scans/run")
        assert both.status_code == 409 and both.json()["detail"] == "Both scans are already running."
        one = client.post("/api/scans/run", params={"platform": "linkedin"})
        assert one.status_code == 409 and one.json()["detail"] == "The LinkedIn scan is already running."
        assert scans.wait(0.05) is False

        naukri_gate.set()  # Naukri finishes while LinkedIn is still running
        assert wait_until(lambda: not scans.running_for(Platform.naukri))
        assert scans.running_for(Platform.linkedin) and scans.running and scans.wait(0.05) is False
        assert scans.last_finished_at(Platform.naukri) is not None and scans.last_finished_at(Platform.linkedin) is None
        again = client.post("/api/scans/run", params={"platform": "naukri"})  # and can run again meanwhile
        assert again.status_code == 202 and again.json()["platforms"] == ["naukri"]
        assert wait_until(lambda: len(naukri.contexts) == 2 and not scans.running_for(Platform.naukri))
        assert len(linkedin.contexts) == 1 and scans.running_for(Platform.linkedin)

        linkedin_gate.set()
        assert scans.wait(5) and not scans.running
        kinds = [r["kind"] for r in client.get("/api/scans").json()["items"]]
        assert sorted(kinds) == ["linkedin_jobs", "naukri_jobs", "naukri_jobs"]


def test_apply_and_test_note_wait_only_for_their_own_group(seeded_settings, fake_browser):
    from tests.test_linkedin_easy_apply import easy_job
    from tests.test_linkedin_outreach import PRIYA, seed_templates
    from tests.test_naukri import connect_naukri
    from tests.test_naukri_apply import APPLY_PAGE, SUCCESS_PAGE, one_click_job

    linkedin_gate, naukri_gate = threading.Event(), threading.Event()
    linkedin = FakeScanner(ScanKind.linkedin_jobs, gate=linkedin_gate)
    naukri = FakeScanner(ScanKind.naukri_jobs, gate=naukri_gate)
    app = make_app(seeded_settings, fake_browser, scanners=[linkedin, naukri])
    with TestClient(app) as client:
        with Session(app.state.engine) as session:
            seed_templates(session)
            naukri_id = one_click_job(session, 9, priority=0).id
            linkedin_id = easy_job(session, 1, priority=0).id
        connect_naukri(client, seeded_settings)
        fake_browser.routes = [("job-listings", page("https://www.naukri.com/job-listings-role-9", APPLY_PAGE))]
        fake_browser.click_routes = [("job-listings", page("https://www.naukri.com/job-listings-role-9", SUCCESS_PAGE))]
        busy_linkedin = "A LinkedIn scan is running; try again when it has finished."

        assert client.post("/api/scans/run", params={"platform": "linkedin"}).status_code == 202
        assert linkedin.started.wait(1)
        response = client.post(f"/api/jobs/{linkedin_id}/apply")
        assert response.status_code == 409 and response.json()["detail"] == busy_linkedin
        note = client.post("/api/outreach/test-note", params={"profile_url": PRIYA})
        assert note.status_code == 409 and note.json()["detail"] == busy_linkedin
        # A Naukri job can be applied to while LinkedIn is scanned (each platform has its own browser).
        done = client.post(f"/api/jobs/{naukri_id}/apply").json()
        assert done["result"] == "applied" and done["job"]["status"] == "applied"
        linkedin_gate.set()
        assert client.app.state.scans.wait(5)

        assert client.post("/api/scans/run", params={"platform": "naukri"}).status_code == 202
        assert naukri.started.wait(1)
        with Session(app.state.engine) as session:
            other_naukri = one_click_job(session, 10, priority=0).id
        response = client.post(f"/api/jobs/{other_naukri}/apply")
        assert response.status_code == 409 and response.json()["detail"] == "A Naukri scan is running; try again when it has finished."
        # LinkedIn's endpoints get past the scan check (and stop at the missing login instead).
        for response in (client.post(f"/api/jobs/{linkedin_id}/apply"), client.post("/api/outreach/test-note", params={"profile_url": PRIYA})):
            assert response.status_code == 409 and response.json()["detail"] == "LinkedIn isn't connected. Connect it first."
        naukri_gate.set()
        assert client.app.state.scans.wait(5)


def test_missing_prefs_and_errors_fail_the_run(settings, fake_browser):
    scanners = [FakeScanner(ScanKind.naukri_jobs), FakeScanner(ScanKind.linkedin_jobs, error=RuntimeError("boom"))]
    app = make_app(settings, fake_browser, scanners=scanners)
    with TestClient(app) as client:
        client.post("/api/scans/run")
        assert app.state.scans.wait(5)
        latest = client.get("/api/scans/status").json()["latest"]
        assert latest[0]["status"] == "failed"
        assert latest[0]["message"] == "The profile field 'search_experience_order' is empty, so there is nothing to search for."
        assert scanners[0].contexts == []

        client.put("/api/profile/fields", json={"fields": SEARCH_FIELDS})
        client.post("/api/scans/run")
        assert app.state.scans.wait(5)
        latest = client.get("/api/scans/status").json()["latest"]
        assert [(r["kind"], r["status"]) for r in latest] == [("linkedin_jobs", "failed"), ("naukri_jobs", "ok")]
        assert latest[0]["message"] == "The scan failed: boom"


def test_stale_running_runs_are_marked_failed(settings, fake_browser):
    with TestClient(make_app(settings, fake_browser)) as client:
        with Session(client.app.state.engine) as session:
            session.add(ScanRun(kind=ScanKind.naukri_jobs, status=ScanStatus.running))
            session.commit()
    with TestClient(make_app(settings, fake_browser)) as client:
        (run,) = client.get("/api/scans").json()["items"]
    assert run["status"] == "failed" and run["finished_at"] is not None
    assert run["message"] == "Interrupted: the backend stopped during this run."


# ---------------------------------------------------------------------------
# Scheduler loop


def test_scheduler_runs_each_group_when_due(seeded_settings, fake_browser):
    naukri_gate = threading.Event()
    scanners = [FakeScanner(ScanKind.linkedin_jobs), FakeScanner(ScanKind.naukri_jobs, gate=naukri_gate)]
    app = make_app(seeded_settings.model_copy(update={"scan_enabled": True}), fake_browser, scanners=scanners)
    scheduler, scans = app.state.scheduler, app.state.scans
    scheduler.tick_seconds = 0.02
    with TestClient(app) as client:
        status = client.get("/api/scans/status").json()
        assert status["enabled"] is True and status["running"] is False
        # One minute after startup for both groups: no runs yet.
        for platform in Platform:
            assert scheduler.next_run_at(platform) - utcnow() > timedelta(seconds=50)
            assert status["platforms"][platform]["next_run_at"] is not None
        linkedin_next = scheduler.next_run_at(Platform.linkedin)

        scheduler._next[Platform.naukri] = utcnow()  # make only Naukri due now
        assert wait_until(lambda: scans.running_for(Platform.naukri))
        status = client.get("/api/scans/status").json()
        assert status["platforms"]["naukri"]["next_run_at"] is None  # while running
        assert status["platforms"]["linkedin"]["running"] is False
        assert status["next_run_at"] == status["platforms"]["linkedin"]["next_run_at"]  # the earliest planned
        assert scheduler.next_run_at(Platform.linkedin) == linkedin_next  # untouched

        scheduler._next[Platform.linkedin] = utcnow()  # LinkedIn falls due while Naukri still runs
        assert wait_until(lambda: scans.last_finished_at(Platform.linkedin) is not None and not scans.running_for(Platform.linkedin))
        assert scans.running_for(Platform.naukri)
        linkedin_finished = scans.last_finished_at(Platform.linkedin)
        assert scheduler.next_run_at(Platform.linkedin) == linkedin_finished + INTERVAL
        assert scheduler.next_run_at() == linkedin_finished + INTERVAL  # Naukri is running
        naukri_gate.set()
        assert scans.wait(5)
        naukri_finished = scans.last_finished_at(Platform.naukri)
        assert naukri_finished > linkedin_finished
        assert scheduler.next_run_at(Platform.naukri) == naukri_finished + INTERVAL
        assert scheduler.next_run_at() == linkedin_finished + INTERVAL
        assert [len(s.contexts) for s in scanners] == [1, 1]  # each ran once
        assert not any(ctx.manual for s in scanners for ctx in s.contexts)  # scheduled runs

    # After a restart each group continues from its own last finished run.
    app = make_app(seeded_settings.model_copy(update={"scan_enabled": True}), fake_browser, scanners=fake_scanners())
    with TestClient(app):
        assert app.state.scheduler.next_run_at(Platform.linkedin) == linkedin_finished + INTERVAL
        assert app.state.scheduler.next_run_at(Platform.naukri) == naukri_finished + INTERVAL


def test_manual_run_moves_only_its_group(seeded_settings, fake_browser):
    app = make_app(seeded_settings.model_copy(update={"scan_enabled": True}), fake_browser, scanners=fake_scanners())
    scheduler = app.state.scheduler
    with TestClient(app) as client:
        naukri_next = scheduler.next_run_at(Platform.naukri)
        client.post("/api/scans/run", params={"platform": "linkedin"})
        assert app.state.scans.wait(5)
        assert scheduler.next_run_at(Platform.linkedin) == app.state.scans.last_finished_at(Platform.linkedin) + INTERVAL
        assert scheduler.next_run_at(Platform.naukri) == naukri_next
        assert app.state.scans.last_finished_at(Platform.naukri) is None


# ---------------------------------------------------------------------------
# The real scanners, end to end with the fake browser


def test_full_run_with_real_scanners(seeded_settings, fake_browser, monkeypatch):
    from app.sources.naukri import selectors as naukri_selectors

    # The fake Naukri answers one search per role; the URL filters (one search per experience level) are
    # tested in test_naukri.py.
    monkeypatch.setattr(naukri_selectors, "EXPERIENCE_PARAM", None)
    monkeypatch.setattr(naukri_selectors, "FRESHNESS_PARAM", None)
    now = utcnow()
    naukri = nk_json(
        nk_job("n1", "Backend Developer", posted=now - timedelta(minutes=30)),
        nk_job("n2", "Golang Developer", posted=now - timedelta(days=2), label="2 Days Ago"),
    )
    fake_browser.routes = [
        ("naukri.com", lambda url: page(url, "<html></html>", captured=[api("https://www.naukri.com/jobapi/v3/search?x", naukri)])),
    ]
    app = make_app(seeded_settings, fake_browser)
    with TestClient(app) as client:
        assert client.post("/api/scans/run").status_code == 202
        assert app.state.scans.wait(10)
        latest = {r["kind"]: r for r in client.get("/api/scans/status").json()["latest"]}
        assert list(latest) == [
            "linkedin_jobs", "linkedin_apply", "linkedin_posts", "email_send", "linkedin_outreach", "naukri_jobs",
            "naukri_apply",
        ]
        assert latest["naukri_jobs"]["status"] == "ok"
        assert (latest["naukri_jobs"]["found"], latest["naukri_jobs"]["new"]) == (1, 1)
        assert latest["naukri_jobs"]["message"].startswith("Search: 2 jobs read; 1 matched. Naukri isn't connected")
        # The LinkedIn search needs the login since 2026-10-13.
        assert latest["linkedin_jobs"]["status"] == "skipped"
        assert latest["linkedin_jobs"]["message"].startswith("LinkedIn isn't connected, so jobs weren't searched")
        assert (latest["linkedin_apply"]["status"], latest["linkedin_apply"]["message"]) == (
            "skipped", "LinkedIn auto-apply is off."
        )
        assert latest["linkedin_posts"]["status"] == "skipped"
        assert "isn't connected" in latest["linkedin_posts"]["message"]
        # Nothing is sent until the candidate switches it on.
        assert (latest["email_send"]["status"], latest["email_send"]["message"]) == ("skipped", "Automatic emails are off.")
        assert (latest["linkedin_outreach"]["status"], latest["linkedin_outreach"]["message"]) == (
            "skipped", "Automatic LinkedIn notes are off."
        )

        jobs = client.get("/api/jobs", params={"status": "found"}).json()["items"]
        (naukri_job,) = jobs
        assert naukri_job["external_id"] == "n1" and naukri_job["apply_type"] == "one_click"
        assert naukri_job["priority"] == 40 and naukri_job["experience"] == "1-4 Yrs"
        assert all(profile is None for profile, _ in fake_browser.opened)  # no saved login was used

        # A second run finds the same job and stores nothing new.
        client.post("/api/scans/run")
        assert app.state.scans.wait(10)
        latest = {r["kind"]: r for r in client.get("/api/scans/status").json()["latest"]}
        assert (latest["naukri_jobs"]["found"], latest["naukri_jobs"]["new"]) == (1, 0)
        assert client.get("/api/jobs").json()["total"] == 1


def test_full_run_with_linkedin_connected(seeded_settings, fake_browser):
    from tests.scan_helpers import connect_linkedin
    from tests.test_linkedin_search import dom_card, results_html

    fake_browser.routes = [
        ("naukri.com", lambda url: page(url, "", captured=[api("https://www.naukri.com/jobapi/v3/search", nk_json())])),
        ("linkedin.com/jobs/search", lambda url: page(url, results_html(dom_card("4470000001", "Java Developer", ago="1 minute ago")))),
        ("search/results/content", lambda url: page(url, "<html><body>No results found</body></html>")),
    ]
    app = make_app(seeded_settings, fake_browser)
    with TestClient(app) as client:
        connect_linkedin(app, fake_browser)
        client.post("/api/scans/run")
        assert app.state.scans.wait(10)
        latest = {r["kind"]: r for r in client.get("/api/scans/status").json()["latest"]}
        assert latest["linkedin_jobs"]["status"] == "ok"
        assert (latest["linkedin_jobs"]["found"], latest["linkedin_jobs"]["new"]) == (1, 1)
        (job,) = client.get("/api/jobs", params={"platform": "linkedin"}).json()["items"]
        assert (job["apply_type"], job["matched_role"]) == ("easy_apply", "Java Developer")
        profile = app.state.browsers.profile_dir(Platform.linkedin)
        assert (profile, True) in fake_browser.opened

        # Within the hour the search sits out (and says when it runs next).
        client.post("/api/scans/run")
        assert app.state.scans.wait(10)
        latest = {r["kind"]: r for r in client.get("/api/scans/status").json()["latest"]}
        assert latest["linkedin_jobs"]["status"] == "skipped"
        assert latest["linkedin_jobs"]["message"].startswith("Next LinkedIn search after ")


def test_posts_scan_with_login_marks_disconnected_on_login_wall(seeded_settings, fake_browser):
    from app.models import Connection, ConnectionStatus

    profile = seeded_settings.browser_profiles_dir / "linkedin"
    profile.mkdir(parents=True)
    fake_browser.cookies = [
        {"name": "li_at", "value": "t", "domain": ".linkedin.com"},
        {"name": "JSESSIONID", "value": '"ajax:1"', "domain": ".www.linkedin.com"},
    ]
    # /me confirms the login at the startup check; the post search then hits a login wall.
    me = {"included": [{"$type": "MiniProfile", "firstName": "Harsh", "lastName": "Vaghela"}]}
    fake_browser.api_routes = [("voyager/api/me", ApiResponse(200, "https://www.linkedin.com/voyager/api/me", me))]
    fake_browser.routes = [
        ("/feed/", page("https://www.linkedin.com/feed/")),
        ("search/results/content", page("https://www.linkedin.com/uas/login?session_redirect=x")),
        ("linkedin.com/jobs", lambda url: page(url, "<html><body><h2>No matching jobs found.</h2></body></html>")),
        ("naukri.com", lambda url: page(url, "", captured=[api("https://www.naukri.com/jobapi/v3/search", nk_json())])),
    ]
    app = make_app(seeded_settings, fake_browser)
    with TestClient(app) as client:
        with Session(app.state.engine) as session:
            row = session.get(Connection, Platform.linkedin)
            row.status, row.checked_at = ConnectionStatus.connected, utcnow()
            session.add(row)
            session.commit()
        client.post("/api/scans/run")
        assert app.state.scans.wait(10)
        latest = {r["kind"]: r for r in client.get("/api/scans/status").json()["latest"]}
        assert latest["linkedin_posts"]["status"] == "failed"
        # (The feed, an empty page here, adds its own note.)
        assert latest["linkedin_posts"]["message"].startswith("LinkedIn redirected to its login page; stopped.")
        linkedin = client.get("/api/connections").json()["items"][0]
        assert linkedin["status"] == "disconnected" and "expired" in linkedin["message"]
        assert latest["linkedin_jobs"]["status"] == "ok"  # the search itself worked (no jobs today)
        assert (profile, True) in fake_browser.opened
