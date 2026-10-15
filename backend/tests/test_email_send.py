"""Automatic emails to hiring posts: message building, the SMTP mailer (with a fake server), the
``email_send`` scanner and the test-email endpoint. No network: every SMTP call goes to FakeSMTP."""

import smtplib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlmodel import Session, select

from app.config import Settings
from app.models import (
    ApplyType,
    Job,
    JobStatus,
    OutreachKind,
    OutreachMessage,
    OutreachStatus,
    Platform,
    ScanStatus,
    TemplateKind,
)
from app.scanner.email_send import EmailSender
from app.services.app_settings import AUTO_SEND_EMAIL, set_flag
from app.services.mailer import Mailer, MailError, SmtpConfig, build_email
from app.services.outreach import (
    DraftProblem,
    clean_job_title,
    fill_placeholders,
    first_name,
    known_company,
)
from app.timeutils import local_day_bounds
from tests.conftest import FAKE_PDF, make_app
from tests.factories import make_job, make_message, make_template
from tests.fakes import FakeSMTP
from tests.scan_helpers import make_ctx, ten_am_today

PASSWORD = "abcdefghijklmnop"
BODY = "Hi [Name],\n\nI'd like to apply for the [Job title] role at [Company].\n\nRegards,\nHarsh"


def smtp_settings(settings: Settings, **extra) -> Settings:
    values = dict(
        smtp_user="harsh@gmail.com", smtp_from_name="Harsh Vaghela", smtp_app_password=SecretStr(PASSWORD), **extra
    )
    return settings.model_copy(update=values)


def config(**extra) -> SmtpConfig:
    values = dict(host="smtp.gmail.com", port=587, user="harsh@gmail.com", from_name="Harsh Vaghela", password=SecretStr(PASSWORD))
    values.update(extra)
    return SmtpConfig(**values)


def template(session, answer=BODY, subject="[Job title] Opportunity"):
    return make_template(session, kind=TemplateKind.email, question="Application email", answer=answer, subject=subject)


def email_job(session, n: int, priority: int, email: str | None = None, **extra) -> Job:
    values = dict(
        platform=Platform.linkedin, external_id=f"urn:li:activity:{n}", title="Backend Developer (hiring post)",
        company="Acme Corp", url=f"https://www.linkedin.com/feed/update/urn:li:activity:{n}/",
        apply_type=ApplyType.email, status=JobStatus.found, priority=priority,
        contact_email=email or f"hr{n}@acme.com", found_at=ten_am_today() - timedelta(hours=1),
    )
    values.update(extra)
    return make_job(session, **values)


# ---------------------------------------------------------------------------
# Building the message


def test_placeholders():
    assert clean_job_title("Backend Developer (hiring post)") == "Backend Developer"
    assert clean_job_title("SDE2") == "SDE2"
    assert first_name("Dr. Priya Sharma, PHR") == "Priya"
    assert first_name("PRIYA SHARMA") == "Priya"
    assert first_name("A. Kumar") == "Kumar"
    assert first_name("Priya Sharma (She/Her)") == "Priya"
    assert first_name("") is None and first_name(None) is None
    assert known_company("LinkedIn post") is None
    assert known_company("Priya Sharma", "Priya Sharma") is None  # the post scanner's fallback
    assert known_company("Acme Corp", "Priya Sharma") == "Acme Corp"

    text = "Hi [Name], the [job title] role at [Company]."
    assert fill_placeholders(text, job_title="SDE", company="Acme", name="Priya") == "Hi Priya, the SDE role at Acme."
    assert fill_placeholders(text, job_title="SDE") == "Hi there, the SDE role at your company."
    assert fill_placeholders("Dear [Name],", job_title="SDE") == "Dear Hiring Team,"


def test_build_email(tmp_path, resume_path):
    with_template = type("T", (), {"answer": BODY, "subject": "[Job title] Opportunity"})()
    message = build_email(
        with_template, config=config(), to="hr@acme.com", resume_path=resume_path,
        job_title="Backend Developer (hiring post)", company="Acme Corp", name="Priya",
    )
    assert message["Subject"] == "Backend Developer Opportunity"
    assert message["To"] == "hr@acme.com"
    assert message["From"] == "Harsh Vaghela <harsh@gmail.com>"
    body = message.get_body(("plain",)).get_content()
    assert body.startswith("Hi Priya,\n\nI'd like to apply for the Backend Developer role at Acme Corp.\n")
    (attachment,) = list(message.iter_attachments())
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_filename() == resume_path.name
    assert attachment.get_content() == FAKE_PDF

    test = build_email(with_template, config=config(), to="harsh@gmail.com", resume_path=resume_path, job_title="Software Engineer", test=True)
    assert test["Subject"] == "[TEST] Software Engineer Opportunity"
    assert "Hi there," in test.get_body(("plain",)).get_content()  # no name known


def test_build_email_refuses_leftover_placeholders(resume_path):
    bad = type("T", (), {"answer": "Hi, my portfolio: [Portfolio link]", "subject": None})()
    with pytest.raises(DraftProblem, match=r"\[Portfolio link\]"):
        build_email(bad, config=config(), to="x@y.com", resume_path=resume_path, job_title="SDE")


# ---------------------------------------------------------------------------
# The mailer


def test_settings_strip_and_hide_the_password(monkeypatch):
    monkeypatch.setenv("SMTP_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("SMTP_USER", " harsh@gmail.com ")
    settings = Settings(_env_file=None)
    assert settings.smtp_app_password.get_secret_value() == PASSWORD
    assert settings.smtp_user == "harsh@gmail.com"
    assert (settings.smtp_host, settings.smtp_port, settings.email_daily_cap) == ("smtp.gmail.com", 587, 20)
    assert PASSWORD not in repr(settings) and PASSWORD not in str(settings.model_dump())
    assert PASSWORD not in repr(SmtpConfig.from_settings(settings))


def test_mailer_logs_in_with_starttls_and_sends(resume_path):
    server = FakeSMTP()
    message = build_email(
        type("T", (), {"answer": BODY, "subject": None})(), config=config(), to="hr@acme.com",
        resume_path=resume_path, job_title="SDE",
    )
    Mailer(config(), server.factory).send(message)
    assert server.calls == [
        ("connect", "smtp.gmail.com", 587),
        ("ehlo",),
        ("starttls", True),
        ("ehlo",),
        ("login", "harsh@gmail.com", PASSWORD),
        ("send", "hr@acme.com"),
        ("quit",),
    ]
    assert message["Subject"] == "Application for SDE"  # no subject on the template


def test_mailer_errors_never_show_the_password(resume_path):
    message = build_email(
        type("T", (), {"answer": "Hi", "subject": "S"})(), config=config(), to="nobody@acme.com",
        resume_path=resume_path, job_title="SDE",
    )
    echo = smtplib.SMTPAuthenticationError(535, f"5.7.8 Username and Password not accepted {PASSWORD}".encode())
    with pytest.raises(MailError) as error:
        Mailer(config(), FakeSMTP(login_error=echo).factory).send(message)
    assert error.value.connection and "535 5.7.8 Username and Password not accepted" in error.value.text
    assert PASSWORD not in error.value.text

    with pytest.raises(MailError) as error:
        Mailer(config(), FakeSMTP(refuse={"nobody@acme.com"}).factory).send(message)
    assert not error.value.connection and "refused the recipient" in error.value.text

    with pytest.raises(MailError) as error:
        Mailer(config(), FakeSMTP(connect_error=OSError("[Errno 8] nodename nor servname provided")).factory).send(message)
    assert error.value.connection and error.value.text.startswith("Couldn't reach the mail server smtp.gmail.com:587")


# ---------------------------------------------------------------------------
# The email_send scanner


def sender(server: FakeSMTP) -> EmailSender:
    return EmailSender(rng=lambda low, high: low, transport_factory=server.factory, sleep=lambda seconds, stop: True)


def test_off_by_default(client):
    outcome = sender(FakeSMTP()).run(make_ctx(client.app))
    assert (outcome.status, outcome.message) == (ScanStatus.skipped, "Automatic emails are off.")


def test_missing_password_skips(client):
    with Session(client.app.state.engine) as session:
        set_flag(session, AUTO_SEND_EMAIL, True)
        template(session)
        email_job(session, 1, priority=0)
    server = FakeSMTP()
    outcome = sender(server).run(make_ctx(client.app))
    assert outcome.status == ScanStatus.skipped
    assert outcome.message == "SMTP_APP_PASSWORD isn't set in backend/.env, so no emails were sent."
    assert server.calls == []


def ctx_with_smtp(client, **extra):
    ctx = make_ctx(client.app)
    ctx.settings = smtp_settings(ctx.settings, **extra)
    return ctx


def test_order_limits_dedupe_waits_and_recording(client):
    engine = client.app.state.engine
    now = ten_am_today()
    with Session(engine) as session:
        set_flag(session, AUTO_SEND_EMAIL, True)
        template(session)
        make_template(session, kind=TemplateKind.email, question="Second", answer="Never used", subject="x")
        jobs = {
            "recent": email_job(session, 1, priority=0, email="Recent@Acme.com"),  # emailed 10 days ago
            "first": email_job(session, 2, priority=10, contact_name="Priya Sharma"),
            "old": email_job(session, 3, priority=20, email="old@acme.com"),  # emailed 40 days ago: fine
            "twin": email_job(session, 4, priority=25, email="hr2@acme.com"),  # same address as "first"
            "third": email_job(session, 5, priority=30, company="Priya Sharma", contact_name="Priya Sharma"),
            "fourth": email_job(session, 6, priority=40),
            "fifth": email_job(session, 7, priority=50),
            "sixth": email_job(session, 8, priority=60),  # over the 5-per-run limit
            # Found the day before yesterday: too old (yesterday's posts are still sent, 2026-10-15).
            "older": email_job(session, 9, priority=0, found_at=now - timedelta(days=2, hours=2)),
            "manual": email_job(session, 10, priority=0, status=JobStatus.needs_manual),
            "form": email_job(session, 11, priority=0, apply_type=ApplyType.google_form),
        }
        ids = {name: job.id for name, job in jobs.items()}
        make_message(session, recipient_email="recent@acme.com", status=OutreachStatus.sent, sent_at=now - timedelta(days=10))
        make_message(session, recipient_email="old@acme.com", status=OutreachStatus.sent, sent_at=now - timedelta(days=40))
        make_message(session, recipient_email="hr5@acme.com", status=OutreachStatus.failed)  # not sent: doesn't count
    server = FakeSMTP()
    email = sender(server)

    outcome = email.run(ctx_with_smtp(client))

    sent_to = [str(m["To"]) for m in server.sent]
    assert sent_to == ["hr2@acme.com", "old@acme.com", "hr5@acme.com", "hr6@acme.com", "hr7@acme.com"]
    assert email.pauses == [30.0] * 4  # between emails only
    assert (outcome.status, outcome.found, outcome.new) == (ScanStatus.ok, 7, 5)
    assert outcome.message == "Emailed 5, 2 skipped (address already emailed in the last 30 days)."
    first = server.sent[0]
    assert first["Subject"] == "Backend Developer Opportunity"
    assert first.get_body(("plain",)).get_content().startswith("Hi Priya,\n\nI'd like to apply for the Backend Developer role at Acme Corp.")
    third = server.sent[2].get_body(("plain",)).get_content()
    assert "Hi Priya," in third and "role at your company." in third  # company was the author's name
    assert all(len(list(m.iter_attachments())) == 1 for m in server.sent)

    with Session(engine) as session:
        status = {name: session.get(Job, ids[name]) for name in ids}
        assert {name: job.status for name, job in status.items()} == {
            "recent": JobStatus.skipped, "first": JobStatus.applied, "old": JobStatus.applied,
            "twin": JobStatus.skipped, "third": JobStatus.applied, "fourth": JobStatus.applied,
            "fifth": JobStatus.applied, "sixth": JobStatus.found, "older": JobStatus.found,
            "manual": JobStatus.needs_manual, "form": JobStatus.found,
        }
        assert status["first"].status_reason == "Emailed hr2@acme.com" and status["first"].applied_at is not None
        assert status["recent"].status_reason.startswith("Not emailed: recent@acme.com was already emailed on ")
        assert status["twin"].status_reason.startswith("Not emailed: hr2@acme.com was already emailed on ")
        rows = session.exec(
            select(OutreachMessage).where(OutreachMessage.kind == OutreachKind.email, OutreachMessage.job_id.is_not(None))
        ).all()
        assert len(rows) == 5 and all(r.status == OutreachStatus.sent and r.sent_at is not None for r in rows)
        row = next(r for r in rows if r.job_id == ids["first"])
        assert (row.recipient_email, row.recipient_name, row.subject) == ("hr2@acme.com", "Priya Sharma", "Backend Developer Opportunity")
        assert row.body.startswith("Hi Priya,")


def test_daily_cap(client):
    engine = client.app.state.engine
    with Session(engine) as session:
        set_flag(session, AUTO_SEND_EMAIL, True)
        template(session)
        for n in range(1, 4):
            email_job(session, n, priority=n)
        start, _ = local_day_bounds(ten_am_today())
        make_message(session, recipient_email="earlier@x.com", status=OutreachStatus.sent, sent_at=start + timedelta(hours=1))
    server = FakeSMTP()
    ctx = ctx_with_smtp(client, email_daily_cap=2)
    assert sender(server).run(ctx).new == 1
    again = sender(server).run(ctx)
    assert (again.status, again.message) == (ScanStatus.skipped, "Today's limit of 2 emails is reached.")
    assert len(server.sent) == 1


def test_refused_recipient_is_recorded_and_the_run_goes_on(client):
    engine = client.app.state.engine
    with Session(engine) as session:
        set_flag(session, AUTO_SEND_EMAIL, True)
        template(session)
        bad, good = email_job(session, 1, priority=0, email="gone@acme.com"), email_job(session, 2, priority=10)
        bad_id, good_id = bad.id, good.id
    outcome = sender(FakeSMTP(refuse={"gone@acme.com"})).run(ctx_with_smtp(client))
    assert (outcome.status, outcome.new) == (ScanStatus.ok, 1)
    assert outcome.message == "Emailed 1, 1 refused by the mail server."
    with Session(engine) as session:
        job = session.get(Job, bad_id)
        assert job.status == JobStatus.failed and job.status_reason.startswith("Email to gone@acme.com failed:")
        failed = session.exec(select(OutreachMessage).where(OutreachMessage.job_id == bad_id)).one()
        assert failed.status == OutreachStatus.failed and "refused the recipient" in failed.error and failed.sent_at is None
        assert session.get(Job, good_id).status == JobStatus.applied


def test_login_failure_stops_and_records_nothing(client):
    engine = client.app.state.engine
    with Session(engine) as session:
        set_flag(session, AUTO_SEND_EMAIL, True)
        template(session)
        job_id = email_job(session, 1, priority=0).id
        email_job(session, 2, priority=1)
    error = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted.")
    server = FakeSMTP(login_error=error)
    email = sender(server)
    outcome = email.run(ctx_with_smtp(client))
    assert outcome.status == ScanStatus.failed
    assert outcome.message == (
        "Emailed 0. Stopped: The mail server didn't accept SMTP_USER and SMTP_APP_PASSWORD: "
        "535 5.7.8 Username and Password not accepted."
    )
    assert PASSWORD not in outcome.message
    assert [c for c in server.calls if c[0] == "login"] == [("login", "harsh@gmail.com", PASSWORD)]  # tried once
    assert email.pauses == []
    with Session(engine) as session:
        assert session.get(Job, job_id).status == JobStatus.found
        assert session.exec(select(OutreachMessage)).all() == []


def test_template_problem_stops_before_sending(client):
    with Session(client.app.state.engine) as session:
        set_flag(session, AUTO_SEND_EMAIL, True)
        template(session, answer="Hi, see [Portfolio link]")
        email_job(session, 1, priority=0)
    server = FakeSMTP()
    outcome = sender(server).run(ctx_with_smtp(client))
    assert outcome.status == ScanStatus.failed and "[Portfolio link]" in outcome.message and server.calls == []


# ---------------------------------------------------------------------------
# Settings and the test endpoint


def test_settings_flags(client):
    assert client.get("/api/settings").json()["auto_send_email"] is False
    assert client.get("/api/settings").json()["auto_send_linkedin_notes"] is False
    body = client.put("/api/settings", json={"auto_send_email": True}).json()
    assert body == {"auto_apply_naukri": False, "auto_apply_linkedin": False, "auto_send_email": True,
                    "auto_send_linkedin_notes": False, "pause_naukri": False, "pause_linkedin": False,
                    "pause_linkedin_jobs": False, "pause_linkedin_posts": False, "pause_naukri_jobs": False,
                    "linkedin_scan_interval_minutes": 30, "naukri_scan_interval_minutes": 30,
                    "linkedin_search_interval_minutes": 60}
    body = client.put("/api/settings", json={"auto_send_linkedin_notes": True, "auto_send_email": False}).json()
    assert (body["auto_send_email"], body["auto_send_linkedin_notes"]) == (False, True)


def test_test_email_needs_the_password(client):
    response = client.post("/api/outreach/test-email")
    assert response.status_code == 409
    assert response.json()["detail"] == "SMTP_APP_PASSWORD isn't set in backend/.env."


def test_test_email_sends_to_myself(settings, fake_browser):
    server = FakeSMTP()
    app = make_app(smtp_settings(settings), fake_browser)
    app.state.smtp_transport = server.factory
    with TestClient(app) as client:
        assert client.post("/api/outreach/test-email").status_code == 409  # no email template yet
        with Session(app.state.engine) as session:
            template(session)
        response = client.post("/api/outreach/test-email")
        assert response.status_code == 200
        assert response.json() == {"sent": True, "to": "harsh@gmail.com", "subject": "[TEST] Software Engineer Opportunity"}
        (message,) = server.sent
        assert message["To"] == "harsh@gmail.com"
        assert "the Software Engineer role at your company" in message.get_body(("plain",)).get_content()
        assert [a.get_filename() for a in message.iter_attachments()] == [settings.resume_path.name]
        assert client.get("/api/outreach").json()["total"] == 0  # nothing recorded

        server.login_error = smtplib.SMTPAuthenticationError(535, f"5.7.8 Bad credentials {PASSWORD}".encode())
        response = client.post("/api/outreach/test-email")
        assert response.status_code == 502
        assert response.json()["detail"].startswith("The mail server didn't accept SMTP_USER and SMTP_APP_PASSWORD: 535")
        assert PASSWORD not in response.text


def test_scanner_order_linkedin_first():
    from app.models import ScanKind
    from app.scheduler.runner import default_scanners

    kinds = [scanner.kind.value for scanner in default_scanners()]
    assert kinds == [
        "linkedin_jobs", "linkedin_apply", "linkedin_posts", "email_send", "linkedin_outreach", "naukri_jobs",
        "naukri_apply",
    ]
    assert kinds == [kind.value for kind in ScanKind]  # /scans/status "latest" uses the same order


def test_unreachable_mail_server_is_a_local_problem():
    from app.scheduler.runner import is_local_problem

    assert is_local_problem("Emailed 0. Stopped: Couldn't reach the mail server smtp.gmail.com:587: [Errno 8] nodename")
    assert not is_local_problem("Emailed 0. Stopped: The mail server didn't accept SMTP_USER and SMTP_APP_PASSWORD: 535")


def test_yesterdays_posts_are_still_emailed(client):
    """Posts the daily cap held back yesterday go out today (2026-10-15)."""
    engine = client.app.state.engine
    now = ten_am_today()
    with Session(engine) as session:
        set_flag(session, AUTO_SEND_EMAIL, True)
        template(session)
        email_job(session, 1, priority=10, found_at=now - timedelta(days=1, hours=2))
        email_job(session, 2, priority=10, found_at=now - timedelta(days=2, hours=2))
    server = FakeSMTP()
    outcome = sender(server).run(ctx_with_smtp(client))
    assert [str(m["To"]) for m in server.sent] == ["hr1@acme.com"]
    assert (outcome.found, outcome.new) == (1, 1)
