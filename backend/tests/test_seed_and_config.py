from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.config import BACKEND_DIR, Settings, get_settings
from app.db import create_db_engine, init_db
from app.models import (
    ApplyType,
    Job,
    JobStatus,
    OutreachKind,
    OutreachMessage,
    OutreachStatus,
    Platform,
    ProfileField,
    Template,
    UnansweredQuestion,
)
from app.seed import SeedRefused, clear, sample_profile_url, seed
from app.seed import main as seed_main
from tests.factories import make_field, make_job, make_message, make_question, make_template


@pytest.fixture
def engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'nested' / 'seed.db'}")
    yield engine
    engine.dispose()


def add_real_data(engine) -> None:
    """Rows that are not sample data and must survive every seed command."""
    with Session(engine) as session:
        real = make_job(session, company="Acme", external_id="real-1")
        make_job(session, company="[sample] Lowercase Is Not Sample", external_id="real-2")
        make_message(session, job_id=real.id, recipient_profile_url="https://www.linkedin.com/in/priya")
        make_message(session, recipient_profile_url="https://www.linkedin.com/in/sample-someone-else")
        make_question(session, job_id=real.id)
        make_question(session)
        make_template(session)
        make_field(session)


def counts(engine) -> dict[str, int]:
    with Session(engine) as session:
        return {
            model.__tablename__: len(session.exec(select(model)).all())
            for model in (Job, OutreachMessage, UnansweredQuestion, Template, ProfileField)
        }


def test_seed_is_idempotent_and_reset_works(engine, profile_path):
    init_db(engine)
    with Session(engine) as session:  # seeding never touches the profile or templates
        make_template(session)
        make_field(session)

    seeded = seed(engine)
    assert seeded is not None and seeded["jobs"] >= 25
    assert seed(engine) is None  # already seeded: skipped

    with Session(engine) as session:
        jobs = session.exec(select(Job)).all()
        assert len(jobs) == seeded["jobs"]
        assert all(job.company.startswith("[Sample] ") for job in jobs)
        combos = {(j.platform, j.apply_type) for j in jobs}
        assert combos == {
            (Platform.linkedin, ApplyType.easy_apply),
            (Platform.linkedin, ApplyType.company_site),
            (Platform.linkedin, ApplyType.google_form),
            (Platform.linkedin, ApplyType.email),
            (Platform.naukri, ApplyType.one_click),
            (Platform.naukri, ApplyType.company_site),
        }
        assert {j.status for j in jobs} == set(JobStatus)
        messages = session.exec(select(OutreachMessage)).all()
        drafts = {m.kind for m in messages if m.status == OutreachStatus.draft}
        assert drafts == set(OutreachKind)
        assert len(session.exec(select(UnansweredQuestion)).all()) == 3

        # Every outreach message and question is linked to a sample job, so --clear removes it.
        sample_ids = {job.id for job in jobs}
        assert all(m.job_id in sample_ids for m in messages)
        assert all(q.job_id in sample_ids for q in session.exec(select(UnansweredQuestion)))

    assert seed(engine, reset=True) == seeded
    assert counts(engine) == {
        "job": seeded["jobs"],
        "outreach_message": seeded["outreach"],
        "unanswered_question": seeded["questions"],
        "template": 1,
        "profile_field": 1,
    }
    assert not profile_path.exists()


def test_seed_refuses_when_real_jobs_exist(engine):
    init_db(engine)
    with Session(engine) as session:
        make_job(session, company="Acme", external_id="real-1")
    for reset in (False, True):
        with pytest.raises(SeedRefused, match="1 job"):
            seed(engine, reset=reset)
    assert counts(engine)["job"] == 1


def test_clear_removes_only_sample_rows(engine):
    seeded = seed(engine)
    add_real_data(engine)
    with Session(engine) as session:  # a message left without its job by an older seed
        make_message(session, recipient_profile_url=sample_profile_url("Sneha Kulkarni"))

    removed = clear(engine)
    assert removed == {
        "jobs": seeded["jobs"],
        "outreach": seeded["outreach"] + 1,
        "questions": seeded["questions"],
    }
    assert counts(engine) == {
        "job": 2,
        "outreach_message": 2,
        "unanswered_question": 2,
        "template": 1,
        "profile_field": 1,
    }
    with Session(engine) as session:
        assert {j.company for j in session.exec(select(Job))} == {"Acme", "[sample] Lowercase Is Not Sample"}
    assert clear(engine) == {"jobs": 0, "outreach": 0, "questions": 0}


@pytest.fixture
def cli_env(monkeypatch, tmp_path, engine):
    """Point the CLI's settings at the temporary database."""
    monkeypatch.setenv("DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("PROFILE_PATH", str(tmp_path / "profile.md"))
    monkeypatch.setenv("RESUME_PATH", str(tmp_path / "resume.pdf"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cli(cli_env, engine, capsys):
    assert seed_main([]) == 0
    assert "Seeded 33 jobs" in capsys.readouterr().out
    add_real_data(engine)

    for argv in ([], ["--reset"]):
        assert seed_main(argv) == 1
        err = capsys.readouterr().err
        assert "Refusing to seed" in err and "2 job(s)" in err

    assert seed_main(["--clear"]) == 0
    assert "Removed 33 jobs, 7 outreach messages and 3 unanswered questions" in capsys.readouterr().out
    assert counts(engine) == {
        "job": 2,
        "outreach_message": 2,
        "unanswered_question": 2,
        "template": 1,
        "profile_field": 1,
    }

    with pytest.raises(SystemExit):
        seed_main(["--reset", "--clear"])


def test_seeded_dashboard(client):
    seed(client.app.state.engine)
    summary = client.get("/api/dashboard/summary").json()
    assert summary["applied_today"] == 5
    assert summary["caps"]["easy_apply_today"]["used"] == 2
    assert summary["caps"]["naukri_apply_today"]["used"] == 2
    assert summary["caps"]["connection_notes_month"]["used"] == 1
    assert summary["caps"]["inmail_month"]["used"] == 1
    assert summary["unanswered_questions"] == 3
    forms = client.get("/api/jobs", params={"platform": "linkedin", "apply_type": "google_form"}).json()
    assert forms["total"] == 4
    assert all(job["form_url"] for job in forms["items"])


def test_relative_paths_resolve_against_backend_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # CWD must not matter
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///data/x.db",
        profile_path="p/profile.md",
        resume_path="r/cv.pdf",
    )
    assert settings.database_url == f"sqlite:///{BACKEND_DIR / 'data' / 'x.db'}"
    assert settings.profile_path == BACKEND_DIR / "p" / "profile.md"
    assert settings.resume_path == BACKEND_DIR / "r" / "cv.pdf"

    defaults = Settings(_env_file=None)
    profile_dir = (BACKEND_DIR.parent / "profile").resolve()
    assert Path(defaults.profile_path) == profile_dir / "CANDIDATE_PROFILE.md"
    assert Path(defaults.resume_path) == profile_dir / "resume" / "Harsh_Vaghela.pdf"


def test_env_vars(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173, https://jobs.example.com")
    monkeypatch.setenv("INMAIL_MONTHLY_CAP", "8")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db:5432/jobs")
    monkeypatch.setenv("RESUME_PATH", "/srv/cv.pdf")
    settings = Settings(_env_file=None)
    assert settings.resume_path == Path("/srv/cv.pdf").resolve()
    assert settings.cors_origin_list == ["http://localhost:5173", "https://jobs.example.com"]
    assert settings.inmail_monthly_cap == 8
    assert settings.database_url == "postgresql://user:pw@db:5432/jobs"


def test_cors_preflight(client):
    response = client.options(
        "/api/jobs",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "PATCH"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_browser_and_scan_defaults(monkeypatch):
    for name in ("BROWSER_PROFILES_DIR", "SCAN_ENABLED", "CHROME_USER_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.scan_enabled is True and defaults.scan_interval_minutes == 30
    assert defaults.scan_headless is True and defaults.scan_max_page_loads == 20
    assert defaults.login_timeout_minutes == 10 and defaults.browser_channel == "chrome"
    assert defaults.browser_profiles_dir == (BACKEND_DIR.parent / ".browser-profiles").resolve()
    assert defaults.chrome_user_data_dir == (Path.home() / "Library/Application Support/Google/Chrome").resolve()
    monkeypatch.setenv("BROWSER_CHANNEL", "firefox")
    with pytest.raises(ValueError):
        Settings(_env_file=None)
