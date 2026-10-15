import re
from datetime import timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import ApplyType, Job, JobStatus, Platform
from app.timeutils import utcnow
from tests.factories import make_job

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_get_job_shape(client, session):
    job = make_job(session, apply_url="https://careers.example.com/1")
    data = client.get(f"/api/jobs/{job.id}").json()
    assert set(data) == {
        "id", "platform", "external_id", "title", "company", "location", "url", "apply_url",
        "form_url", "contact_email", "post_url", "short_description", "apply_type", "status",
        "status_reason", "found_at", "applied_at", "updated_at",
        "posted_at", "experience", "matched_role", "priority",
    }
    assert data["apply_url"] == "https://careers.example.com/1"
    assert data["applied_at"] is None
    assert data["posted_at"] is None and data["priority"] is None
    assert ISO_Z.match(data["found_at"]) and ISO_Z.match(data["updated_at"])


def test_get_job_404(client):
    response = client.get("/api/jobs/999")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_list_jobs_filters_and_sort(client, session):
    now = utcnow()
    old = make_job(session, found_at=now - timedelta(days=2), apply_type=ApplyType.google_form)
    new = make_job(session, found_at=now, apply_type=ApplyType.google_form, status=JobStatus.found)
    make_job(session, platform=Platform.naukri, apply_type=ApplyType.one_click, status=JobStatus.applied)

    data = client.get("/api/jobs").json()
    assert data["total"] == 3

    data = client.get("/api/jobs", params={"platform": "linkedin", "apply_type": "google_form"}).json()
    assert [j["id"] for j in data["items"]] == [new.id, old.id]  # found_at descending

    data = client.get("/api/jobs?status=found&status=needs_manual").json()
    assert {j["id"] for j in data["items"]} == {new.id, old.id}

    data = client.get("/api/jobs", params={"limit": 1, "offset": 1, "apply_type": "google_form"}).json()
    assert data["total"] == 2 and [j["id"] for j in data["items"]] == [old.id]


def test_list_validation(client):
    assert client.get("/api/jobs", params={"limit": 201}).status_code == 422
    assert client.get("/api/jobs", params={"limit": 0}).status_code == 422
    assert client.get("/api/jobs", params={"offset": -1}).status_code == 422
    assert client.get("/api/jobs", params={"platform": "indeed"}).status_code == 422
    assert client.get("/api/jobs", params={"status": "bogus"}).status_code == 422


def test_patch_applied_sets_applied_at(client, session):
    job = make_job(session, status_reason="Applies on the company careers site.")
    response = client.patch(f"/api/jobs/{job.id}", json={"status": "applied"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "applied"
    assert ISO_Z.match(data["applied_at"])
    assert data["status_reason"] is None
    assert data["updated_at"] >= data["found_at"]

    # Shows up in the applications list.
    apps = client.get("/api/applications").json()
    assert [j["id"] for j in apps["items"]] == [job.id]

    # Marking applied again keeps the original timestamp.
    again = client.patch(f"/api/jobs/{job.id}", json={"status": "applied"}).json()
    assert again["applied_at"] == data["applied_at"]


def test_patch_skipped_with_reason(client, session):
    job = make_job(session, status=JobStatus.applied, applied_at=utcnow())
    data = client.patch(
        f"/api/jobs/{job.id}", json={"status": "skipped", "status_reason": "Not interested"}
    ).json()
    assert data["status"] == "skipped"
    assert data["status_reason"] == "Not interested"
    assert data["applied_at"] is None


@pytest.mark.parametrize("status", ["queued", "failed", "bogus", None])
def test_patch_disallowed_status(client, session, status):
    job = make_job(session)
    response = client.patch(f"/api/jobs/{job.id}", json={"status": status})
    assert response.status_code == 422
    assert session.get(Job, job.id).status == JobStatus.needs_manual


def test_patch_404(client):
    assert client.patch("/api/jobs/999", json={"status": "applied"}).status_code == 404


def test_unique_platform_external_id(session):
    make_job(session, platform=Platform.linkedin, external_id="dup")
    make_job(session, platform=Platform.naukri, external_id="dup")  # other platform is fine
    with pytest.raises(IntegrityError):
        make_job(session, platform=Platform.linkedin, external_id="dup")


def test_applications_list(client, session):
    now = utcnow()
    first = make_job(session, status=JobStatus.applied, applied_at=now - timedelta(hours=5))
    second = make_job(
        session, platform=Platform.naukri, status=JobStatus.applied, applied_at=now - timedelta(hours=1)
    )
    make_job(session, status=JobStatus.found)

    data = client.get("/api/applications").json()
    assert data["total"] == 2
    assert [j["id"] for j in data["items"]] == [second.id, first.id]  # newest applied_at first

    data = client.get("/api/applications", params={"platform": "linkedin"}).json()
    assert [j["id"] for j in data["items"]] == [first.id]


def test_scanner_fields(client, session):
    job = make_job(
        session,
        posted_at=utcnow() - timedelta(hours=3),
        experience="1-4 Yrs",
        matched_role="Backend Developer",
        priority=140,
    )
    data = client.get(f"/api/jobs/{job.id}").json()
    assert ISO_Z.match(data["posted_at"])
    assert (data["experience"], data["matched_role"], data["priority"]) == ("1-4 Yrs", "Backend Developer", 140)


def test_list_jobs_found_after(client, session):
    now = utcnow()
    make_job(session, found_at=now - timedelta(days=1))
    edge = make_job(session, found_at=now - timedelta(hours=2))
    new = make_job(session, found_at=now)
    cutoff = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    data = client.get("/api/jobs", params={"found_after": cutoff}).json()
    assert [j["id"] for j in data["items"]] == [new.id, edge.id]  # "at or after"
    assert data["total"] == 2

    # An offset datetime is converted to UTC.
    local = (now - timedelta(hours=1)).astimezone(timezone(timedelta(hours=5, minutes=30)))
    data = client.get("/api/jobs", params={"found_after": local.isoformat()}).json()
    assert [j["id"] for j in data["items"]] == [new.id]

    assert client.get("/api/jobs", params={"found_after": "yesterday"}).status_code == 422


def test_list_jobs_sort_posted_and_found(client, session):
    now = utcnow()
    posted = now - timedelta(hours=1)
    a = make_job(session, found_at=now - timedelta(minutes=5), posted_at=posted)
    b = make_job(session, found_at=now - timedelta(minutes=30))  # no posted_at: counts by found_at
    c = make_job(session, found_at=now, posted_at=now - timedelta(days=3))
    d = make_job(session, found_at=now - timedelta(hours=2), posted_at=posted)  # same posted_at as a

    by_posted = [b.id, d.id, a.id, c.id]  # ties by id, newest first
    assert [j["id"] for j in client.get("/api/jobs").json()["items"]] == by_posted  # the default
    assert [j["id"] for j in client.get("/api/jobs", params={"sort": "posted"}).json()["items"]] == by_posted
    by_found = [c.id, a.id, b.id, d.id]
    assert [j["id"] for j in client.get("/api/jobs", params={"sort": "found"}).json()["items"]] == by_found

    # Paging and filters work with either order.
    data = client.get("/api/jobs", params={"sort": "posted", "limit": 2, "offset": 1}).json()
    assert data["total"] == 4 and [j["id"] for j in data["items"]] == [d.id, a.id]
    found_after = (now - timedelta(minutes=10)).isoformat()
    data = client.get("/api/jobs", params={"found_after": found_after}).json()
    assert [j["id"] for j in data["items"]] == [a.id, c.id]

    assert client.get("/api/jobs", params={"sort": "newest"}).status_code == 422


def test_patch_found_back_to_pending(client, session):
    job = make_job(
        session, status=JobStatus.applied, applied_at=utcnow(), status_reason="Applied on the company site."
    )
    response = client.patch(f"/api/jobs/{job.id}", json={"status": "found", "status_reason": "ignored"})
    assert response.status_code == 200
    data = response.json()
    assert (data["status"], data["applied_at"], data["status_reason"]) == ("found", None, None)
    assert [j["id"] for j in client.get("/api/jobs", params={"status": "found"}).json()["items"]] == [job.id]
    assert client.get("/api/applications").json()["total"] == 0

    skipped = make_job(session, status=JobStatus.skipped, status_reason="Asked for 5+ years.")
    data = client.patch(f"/api/jobs/{skipped.id}", json={"status": "found"}).json()
    assert (data["status"], data["status_reason"]) == ("found", None)
