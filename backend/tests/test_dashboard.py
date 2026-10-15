from datetime import timedelta

from app.models import ApplyType, JobStatus, OutreachKind, OutreachStatus, Platform
from app.timeutils import local_month_bounds, utcnow
from tests.factories import make_job, make_message, make_question


def test_summary_empty_shape(client):
    response = client.get("/api/dashboard/summary")
    assert response.status_code == 200
    assert response.json() == {
        "applied_today": 0,
        "applied_total": 0,
        "applied_by_platform": {"linkedin": 0, "naukri": 0},
        "needs_action": {
            "easy_apply": 0,
            "one_click": 0,
            "company_site": 0,
            "google_form": 0,
            "email": 0,
        },
        "outreach_pending": 0,
        "unanswered_questions": 0,
        "caps": {
            "easy_apply_today": {"used": 0, "limit": 25},
            "naukri_apply_today": {"used": 0, "limit": 50},
            "connection_notes_month": {"used": 0, "limit": 250},
            "inmail_month": {"used": 0, "limit": 5},
        },
    }


def test_summary_counts(client, session):
    now = utcnow()
    long_ago = now - timedelta(days=3)
    last_month = local_month_bounds()[0] - timedelta(hours=1)

    li, nk = Platform.linkedin, Platform.naukri
    applied = JobStatus.applied
    # Applied today: 2 LinkedIn Easy Apply, 1 Naukri one-click, 1 LinkedIn company site.
    make_job(session, platform=li, apply_type=ApplyType.easy_apply, status=applied, applied_at=now)
    make_job(session, platform=li, apply_type=ApplyType.easy_apply, status=applied, applied_at=now)
    make_job(session, platform=nk, apply_type=ApplyType.one_click, status=applied, applied_at=now)
    make_job(session, platform=li, apply_type=ApplyType.company_site, status=applied, applied_at=now)
    # Applied earlier.
    make_job(session, platform=li, apply_type=ApplyType.easy_apply, status=applied, applied_at=long_ago)
    make_job(session, platform=nk, apply_type=ApplyType.one_click, status=applied, applied_at=long_ago)
    # Needing action.
    make_job(session, apply_type=ApplyType.company_site, status=JobStatus.needs_manual)
    make_job(session, platform=nk, apply_type=ApplyType.company_site, status=JobStatus.found)
    make_job(session, apply_type=ApplyType.google_form, status=JobStatus.needs_manual)
    make_job(session, apply_type=ApplyType.google_form, status=JobStatus.skipped)  # not counted
    make_job(session, apply_type=ApplyType.easy_apply, status=JobStatus.queued)  # not counted

    note, inmail = OutreachKind.connection_note, OutreachKind.inmail
    sent = OutreachStatus.sent
    make_message(session)  # draft email
    make_message(session, kind=note, status=OutreachStatus.draft)
    make_message(session, kind=note, status=OutreachStatus.approved)
    make_message(session, kind=note, status=sent, sent_at=now)
    make_message(session, kind=note, status=sent, sent_at=now)
    make_message(session, kind=note, status=sent, sent_at=last_month)  # previous month
    make_message(session, kind=inmail, status=sent, sent_at=now)
    make_message(session, kind=inmail, status=OutreachStatus.failed)

    make_question(session)
    make_question(session)
    make_question(session, answered_at=now)

    data = client.get("/api/dashboard/summary").json()
    assert data["applied_today"] == 4
    assert data["applied_total"] == 6
    assert data["applied_by_platform"] == {"linkedin": 4, "naukri": 2}
    assert data["needs_action"] == {
        "easy_apply": 0,
        "one_click": 0,
        "company_site": 2,
        "google_form": 1,
        "email": 0,
    }
    assert data["outreach_pending"] == 2
    assert data["unanswered_questions"] == 2
    assert data["caps"] == {
        "easy_apply_today": {"used": 2, "limit": 25},
        "naukri_apply_today": {"used": 1, "limit": 50},
        "connection_notes_month": {"used": 2, "limit": 250},
        "inmail_month": {"used": 1, "limit": 5},
    }


def test_cap_limits_come_from_settings(tmp_path, profile_path):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'caps.db'}",
        profile_path=profile_path,
        easy_apply_daily_cap=3,
        naukri_daily_cap=4,
        connection_notes_monthly_cap=5,
        inmail_monthly_cap=6,
    )
    with TestClient(create_app(settings)) as client:
        caps = client.get("/api/dashboard/summary").json()["caps"]
    assert [c["limit"] for c in caps.values()] == [3, 4, 5, 6]
