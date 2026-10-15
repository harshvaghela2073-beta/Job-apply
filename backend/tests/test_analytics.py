from datetime import timedelta

from app.models import ApplyType, JobStatus, OutreachKind, OutreachStatus, Platform
from app.timeutils import local_day_bounds, utcnow
from tests.factories import make_job, make_message


def counts(found: int, applied: int) -> dict:
    return {"found": found, "applied": applied, "pending": found - applied}


def period(li: dict | None = None, notes: int = 0, nk: dict | None = None) -> dict:
    li, nk = li or {}, nk or {}
    zero = counts(0, 0)
    return {
        "linkedin": {
            **{key: li.get(key, zero) for key in ("easy_apply", "company_site", "google_form", "email")},
            "connection_notes_sent": notes,
        },
        "naukri": {key: nk.get(key, zero) for key in ("one_click", "company_site")},
    }


def test_analytics_empty(client):
    response = client.get("/api/analytics")
    assert response.status_code == 200
    assert response.json() == {"today": period(), "all": period()}
    assert response.json()["today"]["naukri"]["one_click"] == {"found": 0, "applied": 0, "pending": 0}


def test_analytics_counts(client, session):
    now = utcnow()
    day_start, _ = local_day_bounds()
    yesterday = day_start - timedelta(minutes=1)
    li, nk = Platform.linkedin, Platform.naukri
    applied = JobStatus.applied

    def job(platform, apply_type, status, found_at=now, **extra):
        make_job(session, platform=platform, apply_type=apply_type, status=status, found_at=found_at, **extra)

    # Found today.
    job(li, ApplyType.easy_apply, applied, applied_at=now)
    job(li, ApplyType.easy_apply, JobStatus.found)
    job(li, ApplyType.easy_apply, JobStatus.needs_manual)
    job(li, ApplyType.company_site, JobStatus.needs_manual)
    job(li, ApplyType.company_site, applied, applied_at=now)
    job(li, ApplyType.google_form, JobStatus.found, found_at=day_start)  # the start of today counts
    job(li, ApplyType.email, applied, applied_at=now)  # emailed
    job(nk, ApplyType.one_click, applied, applied_at=now)
    job(nk, ApplyType.one_click, applied, applied_at=now)
    job(nk, ApplyType.one_click, JobStatus.failed)
    # Found before today (an older job applied today counts only in "all").
    job(li, ApplyType.easy_apply, applied, found_at=yesterday, applied_at=now)
    job(li, ApplyType.easy_apply, JobStatus.skipped, found_at=yesterday)
    job(li, ApplyType.email, JobStatus.found, found_at=yesterday)
    job(nk, ApplyType.company_site, JobStatus.needs_manual, found_at=yesterday)
    job(nk, ApplyType.company_site, applied, found_at=yesterday, applied_at=yesterday)
    # Not an Analysis bucket.
    job(nk, ApplyType.email, applied, applied_at=now)

    note = OutreachKind.connection_note
    make_message(session, kind=note, status=OutreachStatus.sent, sent_at=now)
    make_message(session, kind=note, status=OutreachStatus.sent, sent_at=day_start)
    make_message(session, kind=note, status=OutreachStatus.sent, sent_at=yesterday)
    make_message(session, kind=note, status=OutreachStatus.draft)
    make_message(session, kind=note, status=OutreachStatus.failed)
    make_message(session, kind=OutreachKind.inmail, status=OutreachStatus.sent, sent_at=now)
    make_message(session, kind=OutreachKind.email, status=OutreachStatus.sent, sent_at=now)

    data = client.get("/api/analytics").json()
    assert data["today"] == period(
        li={
            "easy_apply": counts(3, 1),
            "company_site": counts(2, 1),
            "google_form": counts(1, 0),
            "email": counts(1, 1),
        },
        notes=2,
        nk={"one_click": counts(3, 2)},  # no Naukri company-site job today: zeros
    )
    assert data["all"] == period(
        li={
            "easy_apply": counts(5, 2),
            "company_site": counts(2, 1),
            "google_form": counts(1, 0),
            "email": counts(2, 1),
        },
        notes=3,
        nk={"one_click": counts(3, 2), "company_site": counts(2, 1)},
    )
