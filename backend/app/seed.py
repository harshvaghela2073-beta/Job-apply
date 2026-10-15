"""Load sample data for UI development.

Usage (from backend/):
    python -m app.seed           # seed only if there are no jobs, outreach messages or questions
    python -m app.seed --reset   # remove the sample rows, then seed again
    python -m app.seed --clear   # remove the sample rows and don't seed

Sample data is recognised by its markers, and only sample rows are ever deleted:

- Sample jobs: the company name starts with "[Sample] " (case-sensitive).
- Sample outreach messages and unanswered questions: linked (``job_id``) to a sample job. Every
  row the seed creates is linked. Older versions of the seed created three outreach messages with
  no job; those are recognised by their exact sample recipient profile URLs
  (``https://www.linkedin.com/in/sample-<name>``) together with ``job_id IS NULL``.

The seed refuses to run (exit code 1) when the database has any job that is not sample data, so
sample rows are never mixed into real data; ``--clear`` still works then. Seeding never touches
the profile (``ProfileField``) or Template Library (``Template``) tables, or the profile file.
"""

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Engine, and_, delete, func, or_
from sqlmodel import Session, col, select

from app.config import get_settings
from app.db import create_db_engine, init_db
from app.models import (
    ApplyType as A,
    Job,
    JobStatus as S,
    OutreachKind,
    OutreachMessage,
    OutreachStatus,
    Platform as P,
    UnansweredQuestion,
)
from app.timeutils import local_day_bounds, local_month_bounds, utcnow

SAMPLE_PREFIX = "[Sample] "
# Recipients of the sample outreach messages; their profile URLs are "sample-<name>".
SAMPLE_RECIPIENTS = (
    "Ananya Iyer",
    "Rahul Menon",
    "Rohit Verma",
    "Sneha Kulkarni",
    "Arjun Nair",
    "Kavya Reddy",
    "Vikram Singh",
)


class SeedRefused(RuntimeError):
    """The database holds real (non-sample) jobs, so sample data must not be added."""


class _Clock:
    """Timestamps relative to the moment of seeding, so "today" data is always today."""

    def __init__(self) -> None:
        self.now = utcnow()
        self.day_start, _ = local_day_bounds(self.now)
        self.month_start, _ = local_month_bounds(self.now)

    def today(self, fraction: float) -> datetime:
        """A moment today: ``fraction`` of the way from local midnight to now."""
        return self.day_start + (self.now - self.day_start) * fraction

    def this_month(self, fraction: float) -> datetime:
        return self.month_start + (self.now - self.month_start) * fraction

    def days_ago(self, days: int, hours: float = 0) -> datetime:
        return self.now - timedelta(days=days, hours=hours)


@dataclass
class _JobSpec:
    platform: P
    external_id: str
    title: str
    company: str
    location: str
    apply_type: A
    status: S
    description: str
    found: str  # "today:<fraction>" or "<days ago>"
    applied: str | None = None
    reason: str | None = None
    extra: dict = field(default_factory=dict)


def _slug(text: str) -> str:
    keep = "".join(c.lower() if c.isalnum() else "-" for c in text.replace(SAMPLE_PREFIX, ""))
    return "-".join(part for part in keep.split("-") if part)


def sample_profile_url(name: str) -> str:
    return f"https://www.linkedin.com/in/sample-{_slug(name)}"


SAMPLE_PROFILE_URLS = tuple(sample_profile_url(name) for name in SAMPLE_RECIPIENTS)


def _job_specs() -> list[_JobSpec]:
    li, nk = P.linkedin, P.naukri
    return [
        # LinkedIn - Easy Apply
        _JobSpec(li, "4100000001", "Senior Backend Engineer (Python)", "[Sample] Nimbus Payments",
                 "Bengaluru (Hybrid)", A.easy_apply, S.applied,
                 "Python/FastAPI, 5-8 yrs, UPI payments platform.", "1", "today:0.85"),
        _JobSpec(li, "4100000002", "Software Engineer II - Platform", "[Sample] Konkan Cloud",
                 "Pune (Hybrid)", A.easy_apply, S.applied,
                 "Go/Python microservices on AWS, 3-6 yrs.", "today:0.2", "today:0.55"),
        _JobSpec(li, "4100000003", "Backend Developer - Django", "[Sample] Banyan EdTech",
                 "Remote (India)", A.easy_apply, S.applied,
                 "Django + PostgreSQL, 3-5 yrs, ed-tech at scale.", "2", "1"),
        _JobSpec(li, "4100000004", "SDE 2 - Payments", "[Sample] Lotus Fintech",
                 "Gurugram (Onsite)", A.easy_apply, S.applied,
                 "Java/Kotlin or Python, 4-7 yrs, card payments.", "5", "4"),
        _JobSpec(li, "4100000005", "Python Developer - Data Platform", "[Sample] Indus Analytics",
                 "Hyderabad (Hybrid)", A.easy_apply, S.queued,
                 "Airflow, Spark and Python; 3-6 yrs.", "today:0.6"),
        _JobSpec(li, "4100000006", "Backend Engineer - Logistics", "[Sample] Bharat Freightworks",
                 "Bengaluru (Onsite)", A.easy_apply, S.failed,
                 "Node.js/Python, route optimisation, 2-5 yrs.", "1",
                 reason="Easy Apply dialog closed unexpectedly after the resume step."),
        _JobSpec(li, "4100000007", "Senior Software Engineer - Streaming", "[Sample] Masala Games",
                 "Bengaluru (Hybrid)", A.easy_apply, S.needs_manual,
                 "Kafka, Flink, Python; 5+ yrs, real-time leaderboards.", "today:0.4",
                 reason="Easy Apply asked questions with no template answer."),
        _JobSpec(li, "4100000008", "Staff Engineer - Search", "[Sample] Rangoli Retail",
                 "Mumbai (Hybrid)", A.easy_apply, S.found,
                 "Elasticsearch, Python, 8+ yrs, e-commerce search relevance.", "today:0.9"),
        _JobSpec(li, "4100000009", "Engineering Manager - Backend", "[Sample] Ashoka Security",
                 "Noida (Onsite)", A.easy_apply, S.skipped,
                 "People manager, 10+ yrs, security products.", "3",
                 reason="Excluded: requires 10+ years of experience."),
        # LinkedIn - company website
        _JobSpec(li, "4100000010", "Senior Software Engineer - APIs", "[Sample] Saffron Health",
                 "Bengaluru (Hybrid)", A.company_site, S.needs_manual,
                 "FastAPI, PostgreSQL, HL7/FHIR integrations; 5-8 yrs.", "today:0.7",
                 reason="Applies on the company careers site."),
        _JobSpec(li, "4100000011", "Backend Engineer - Core Banking", "[Sample] Coral Insurtech",
                 "Chennai (Onsite)", A.company_site, S.needs_manual,
                 "Python/Java, ledger systems, 4-7 yrs.", "1",
                 reason="Applies on the company careers site."),
        _JobSpec(li, "4100000012", "Platform Engineer", "[Sample] Deccan SaaS",
                 "Hyderabad (Remote)", A.company_site, S.found,
                 "Kubernetes, Terraform, Python tooling; 3-6 yrs.", "today:0.95"),
        _JobSpec(li, "4100000013", "Software Engineer - Integrations", "[Sample] Tiffin Tech",
                 "Pune (Hybrid)", A.company_site, S.applied,
                 "REST/webhooks, Python, 2-5 yrs, food-delivery partner APIs.", "4", "2"),
        _JobSpec(li, "4100000014", "Senior Java Developer", "[Sample] Himalaya Infra",
                 "Gurugram (Onsite)", A.company_site, S.skipped,
                 "Java/Spring only, 6-9 yrs.", "6",
                 reason="Java-only stack; not a match."),
        # LinkedIn - Google Form (from posts)
        _JobSpec(li, "post-7100000001", "Backend Developer (Python) - Hiring Drive", "[Sample] Nilgiri AI",
                 "Chennai (Hybrid)", A.google_form, S.needs_manual,
                 "Post: hiring Python backend devs, 2-5 yrs, fill the form to apply.", "today:0.5",
                 extra={"form": True}),
        _JobSpec(li, "post-7100000002", "SDE 1/2 - Referral Drive", "[Sample] Peacock Media",
                 "Mumbai (Onsite)", A.google_form, S.needs_manual,
                 "Post: referral drive for SDE 1/2, OTT streaming backend.", "1",
                 extra={"form": True}),
        _JobSpec(li, "post-7100000003", "Full-stack Engineer (Python + React)", "[Sample] Cauvery Robotics",
                 "Bengaluru (Onsite)", A.google_form, S.found,
                 "Post: small robotics team, Python + React dashboards.", "today:0.8",
                 extra={"form": True}),
        _JobSpec(li, "post-7100000004", "API Engineer - Contract to Hire", "[Sample] Jugaad Devtools",
                 "Remote (India)", A.google_form, S.applied,
                 "Post: 6-month C2H, Python API development.", "2", "today:0.35",
                 extra={"form": True}),
        # LinkedIn - email the hiring person (from posts)
        _JobSpec(li, "post-7100000005", "Senior Python Engineer", "[Sample] Vriksha Agritech",
                 "Bengaluru (Hybrid)", A.email, S.needs_manual,
                 "Post: 'DM or email me your resume', Python + GIS, 5+ yrs.", "today:0.3",
                 extra={"email": "ananya.iyer@vriksha-agritech.example"}),
        _JobSpec(li, "post-7100000006", "Backend Lead - Fintech", "[Sample] Chai Labs",
                 "Hyderabad (Hybrid)", A.email, S.found,
                 "Post: lead a 4-person backend team, Python/Go, 7+ yrs.", "1",
                 extra={"email": "rahul.menon@chai-labs.example"}),
        _JobSpec(li, "post-7100000007", "Software Engineer - Backend", "[Sample] Monsoon Commerce",
                 "Remote (India)", A.email, S.applied,
                 "Post: early-stage D2C startup, Python/Django, 2-4 yrs.", "4", "3",
                 extra={"email": "neha.kapoor@monsoon-commerce.example"}),
        # Naukri - one-click apply
        _JobSpec(nk, "131025001234", "Python Developer", "[Sample] Ganga Data Systems",
                 "Noida", A.one_click, S.applied,
                 "Python, Flask, MySQL; 3-6 yrs.", "1", "today:0.75"),
        _JobSpec(nk, "131025001235", "Senior Software Engineer - Backend", "[Sample] Mithai Mobility",
                 "Bengaluru", A.one_click, S.applied,
                 "Python/Go, ride-hailing dispatch, 5-8 yrs.", "today:0.1", "today:0.45"),
        _JobSpec(nk, "111025004410", "Backend Developer - Node/Python", "[Sample] Pune Pixel Studio",
                 "Pune", A.one_click, S.applied,
                 "Node.js or Python, 2-5 yrs, media pipelines.", "3", "2"),
        _JobSpec(nk, "131025001236", "Django Developer", "[Sample] Hyderabad Health Cloud",
                 "Hyderabad", A.one_click, S.queued,
                 "Django REST Framework, Celery; 3-5 yrs.", "today:0.65"),
        _JobSpec(nk, "121025002201", "Software Engineer - Microservices", "[Sample] Kesari Logistics",
                 "Mumbai", A.one_click, S.failed,
                 "Python/Java microservices, 3-6 yrs.", "1",
                 reason="Naukri showed 'Something went wrong' on submit."),
        _JobSpec(nk, "131025001237", "Lead Python Engineer", "[Sample] Sahyadri Systems",
                 "Pune", A.one_click, S.found,
                 "Python, AWS, team lead, 7-10 yrs.", "today:0.92"),
        _JobSpec(nk, "101025009876", "Junior Python Developer", "[Sample] Tulsi Infotech",
                 "Ahmedabad", A.one_click, S.skipped,
                 "Python, 0-2 yrs.", "3",
                 reason="Below the minimum experience level in the search preferences."),
        _JobSpec(nk, "131025001238", "Backend Engineer - Python", "[Sample] Kaveri Cloudworks",
                 "Pune", A.one_click, S.needs_manual,
                 "Python, PostgreSQL, 3-6 yrs; recruiter questionnaire.", "today:0.5",
                 reason="Recruiter questionnaire has a question with no template answer."),
        # Naukri - company website
        _JobSpec(nk, "131025001239", "Senior Backend Engineer", "[Sample] Narmada Capital Tech",
                 "Mumbai", A.company_site, S.needs_manual,
                 "Python, trading systems, 5-8 yrs.", "today:0.55",
                 reason="Redirects to the company careers site."),
        _JobSpec(nk, "121025002202", "Platform Engineer - Kubernetes", "[Sample] Godavari Semiconductors",
                 "Hyderabad", A.company_site, S.needs_manual,
                 "Kubernetes, Go/Python, 4-7 yrs.", "1",
                 reason="Redirects to the company careers site."),
        _JobSpec(nk, "091025003303", "Python Engineer - Risk", "[Sample] Lotus Fintech",
                 "Bengaluru", A.company_site, S.applied,
                 "Python, pandas, credit-risk models; 3-6 yrs.", "6", "5"),
        _JobSpec(nk, "111025004411", "SDE - Embedded Systems", "[Sample] Ashoka Security",
                 "Noida", A.company_site, S.skipped,
                 "Embedded C, RTOS, 3-6 yrs.", "2",
                 reason="Embedded C role; not a match."),
    ]


def _when(clock: _Clock, spec: str) -> datetime:
    if spec.startswith("today:"):
        return clock.today(float(spec.split(":", 1)[1]))
    return clock.days_ago(int(spec), hours=2)


def _build_job(clock: _Clock, spec: _JobSpec) -> Job:
    slug = _slug(spec.company)
    applied_at = _when(clock, spec.applied) if spec.applied else None
    found_at = _when(clock, spec.found)
    if applied_at is not None and found_at > applied_at:
        found_at = applied_at - timedelta(minutes=30)
    if spec.platform == P.linkedin:
        if spec.external_id.startswith("post-"):
            url = f"https://www.linkedin.com/posts/sample-{slug}-{spec.external_id}"
        else:
            url = f"https://www.linkedin.com/jobs/view/{spec.external_id}"
    else:
        url = f"https://www.naukri.com/job-listings-sample-{_slug(spec.title)}-{spec.external_id}"
    job = Job(
        platform=spec.platform,
        external_id=spec.external_id,
        title=spec.title,
        company=spec.company,
        location=spec.location,
        url=url,
        short_description=spec.description,
        apply_type=spec.apply_type,
        status=spec.status,
        status_reason=spec.reason,
        found_at=found_at,
        applied_at=applied_at,
        updated_at=applied_at or found_at,
    )
    if spec.apply_type == A.company_site:
        job.apply_url = f"https://careers.{slug}.example/jobs/{spec.external_id[-4:]}"
    if spec.extra.get("form"):
        job.form_url = f"https://docs.google.com/forms/d/e/SAMPLE-{slug}/viewform"
    if spec.extra.get("email"):
        job.contact_email = spec.extra["email"]
    if spec.external_id.startswith("post-"):
        job.post_url = url
    return job


def _outreach(clock: _Clock, jobs: dict[str, Job]) -> list[OutreachMessage]:
    """Sample outreach. Every message is linked to a sample job, so ``--clear`` removes it."""
    vriksha = jobs["post-7100000005"]
    chai = jobs["post-7100000006"]
    konkan = jobs["4100000002"]
    saffron = jobs["4100000010"]
    tiffin = jobs["4100000013"]
    nimbus = jobs["4100000001"]
    masala = jobs["4100000007"]
    return [
        OutreachMessage(
            kind=OutreachKind.email,
            job_id=vriksha.id,
            recipient_name="Ananya Iyer",
            recipient_profile_url=sample_profile_url("Ananya Iyer"),
            recipient_email=vriksha.contact_email,
            subject=f"Application: {vriksha.title}",
            body=(
                "Hi Ananya,\n\n"
                f"I saw your LinkedIn post about the {vriksha.title} role at {vriksha.company} "
                "and would like to be considered. My resume is attached.\n\n"
                "[Sample draft - real drafts are built from the Template Library.]\n\n"
                "Best regards,\n[Candidate name]"
            ),
            status=OutreachStatus.draft,
            created_at=clock.today(0.32),
        ),
        OutreachMessage(
            kind=OutreachKind.email,
            job_id=chai.id,
            recipient_name="Rahul Menon",
            recipient_profile_url=sample_profile_url("Rahul Menon"),
            recipient_email=chai.contact_email,
            subject=f"Application: {chai.title}",
            body=(
                "Hi Rahul,\n\n"
                f"Your post mentioned an opening for a {chai.title} at {chai.company}. "
                "I'd love to talk about it; my resume is attached.\n\n"
                "Best regards,\n[Candidate name]"
            ),
            status=OutreachStatus.approved,
            created_at=clock.days_ago(1),
            approved_at=clock.today(0.2),
        ),
        OutreachMessage(
            kind=OutreachKind.connection_note,
            job_id=konkan.id,
            recipient_name="Rohit Verma",
            recipient_profile_url=sample_profile_url("Rohit Verma"),
            body=(
                f"Hi Rohit, I recently applied for the {konkan.title} role at {konkan.company} "
                "and would be glad to connect. Happy to share more about my background if useful."
            ),
            status=OutreachStatus.draft,
            created_at=clock.today(0.6),
        ),
        OutreachMessage(
            kind=OutreachKind.connection_note,
            job_id=tiffin.id,
            recipient_name="Sneha Kulkarni",
            recipient_profile_url=sample_profile_url("Sneha Kulkarni"),
            body=(
                f"Hi Sneha, I noticed you recruit for backend roles at {tiffin.company} in Pune. "
                "I'm exploring new opportunities and would be glad to connect."
            ),
            status=OutreachStatus.sent,
            created_at=clock.this_month(0.3),
            approved_at=clock.this_month(0.35),
            sent_at=clock.this_month(0.4),
        ),
        OutreachMessage(
            kind=OutreachKind.connection_note,
            job_id=nimbus.id,
            recipient_name="Arjun Nair",
            recipient_profile_url=sample_profile_url("Arjun Nair"),
            body=(
                f"Hi Arjun, I'm interested in backend roles at {nimbus.company} in Bengaluru "
                "and would be glad to connect."
            ),
            status=OutreachStatus.rejected,
            created_at=clock.days_ago(2),
        ),
        OutreachMessage(
            kind=OutreachKind.inmail,
            job_id=saffron.id,
            recipient_name="Kavya Reddy",
            recipient_profile_url=sample_profile_url("Kavya Reddy"),
            body=(
                "Hi Kavya,\n\n"
                f"I'm interested in the {saffron.title} opening at {saffron.company} and plan to "
                "apply on your careers site. Could you share who the hiring manager is, or "
                "whether a referral would help?\n\nThanks,\n[Candidate name]"
            ),
            status=OutreachStatus.draft,
            created_at=clock.today(0.72),
        ),
        OutreachMessage(
            kind=OutreachKind.inmail,
            job_id=masala.id,
            recipient_name="Vikram Singh",
            recipient_profile_url=sample_profile_url("Vikram Singh"),
            body=(
                "Hi Vikram,\n\nI'm a backend engineer exploring new roles and noticed your team "
                f"at {masala.company} is growing. Would you be open to a short chat?"
                "\n\nThanks,\n[Candidate name]"
            ),
            status=OutreachStatus.sent,
            created_at=clock.this_month(0.5),
            approved_at=clock.this_month(0.55),
            sent_at=clock.this_month(0.6),
        ),
    ]


def _questions(clock: _Clock, jobs: dict[str, Job]) -> list[UnansweredQuestion]:
    """Sample questions. Every question is linked to a sample job, so ``--clear`` removes it."""
    return [
        UnansweredQuestion(
            question="Are you open to working from the Pune office 3 days a week?",
            platform=P.naukri,
            job_id=jobs["131025001238"].id,
            seen_at=clock.today(0.5),
        ),
        UnansweredQuestion(
            question="What is your current notice period (in days)?",
            platform=P.linkedin,
            job_id=jobs["4100000007"].id,
            seen_at=clock.today(0.4),
        ),
        UnansweredQuestion(
            question="How many years of hands-on experience do you have with Apache Kafka?",
            platform=P.linkedin,
            job_id=jobs["4100000007"].id,
            seen_at=clock.today(0.41),
        ),
    ]


def _is_sample_job():
    # substr + "=" is case-sensitive, unlike LIKE in SQLite.
    return func.substr(Job.company, 1, len(SAMPLE_PREFIX)) == SAMPLE_PREFIX


def count_real_jobs(session: Session) -> int:
    """Jobs whose company doesn't start with "[Sample] "."""
    return session.exec(select(func.count()).select_from(Job).where(~_is_sample_job())).one()


def has_data(session: Session) -> bool:
    for model in (Job, OutreachMessage, UnansweredQuestion):
        if session.exec(select(func.count()).select_from(model)).one():
            return True
    return False


def clear_samples(session: Session) -> dict[str, int]:
    """Delete sample rows only (see the module docstring for the markers) and commit.

    Linked outreach and questions go first: deleting a job would otherwise set their ``job_id``
    to NULL. Profile fields and templates are never touched. Returns the deleted row counts.
    """
    sample_job_ids = select(Job.id).where(_is_sample_job())
    outreach = session.exec(
        delete(OutreachMessage).where(
            or_(
                col(OutreachMessage.job_id).in_(sample_job_ids),
                and_(
                    col(OutreachMessage.job_id).is_(None),
                    col(OutreachMessage.recipient_profile_url).in_(SAMPLE_PROFILE_URLS),
                ),
            )
        )
    ).rowcount
    questions = session.exec(
        delete(UnansweredQuestion).where(col(UnansweredQuestion.job_id).in_(sample_job_ids))
    ).rowcount
    jobs = session.exec(delete(Job).where(_is_sample_job())).rowcount
    session.commit()
    return {"jobs": jobs, "outreach": outreach, "questions": questions}


def _refuse_if_real_jobs(session: Session) -> None:
    real = count_real_jobs(session)
    if real:
        raise SeedRefused(
            f"The database has {real} job(s) whose company doesn't start with "
            f"{SAMPLE_PREFIX!r}, so it holds real data. Sample data is only loaded into a "
            "database without real jobs. (--clear still removes the sample rows.)"
        )


def seed(engine: Engine, reset: bool = False) -> dict[str, int] | None:
    """Seed sample data. Returns row counts, or None if skipped because data already exists.

    ``reset`` removes the existing sample rows first. Raises ``SeedRefused`` if the database has
    any non-sample job.
    """
    init_db(engine)
    with Session(engine) as session:
        _refuse_if_real_jobs(session)
        if reset:
            clear_samples(session)
        elif has_data(session):
            return None
        clock = _Clock()
        jobs = [_build_job(clock, spec) for spec in _job_specs()]
        session.add_all(jobs)
        session.flush()  # assigns ids for the foreign keys below
        by_external_id = {job.external_id: job for job in jobs}
        outreach = _outreach(clock, by_external_id)
        questions = _questions(clock, by_external_id)
        session.add_all(outreach + questions)
        session.commit()
        return {"jobs": len(jobs), "outreach": len(outreach), "questions": len(questions)}


def clear(engine: Engine) -> dict[str, int]:
    """Remove the sample rows without seeding. Returns the deleted row counts."""
    init_db(engine)
    with Session(engine) as session:
        return clear_samples(session)


def _counts_text(counts: dict[str, int]) -> str:
    return (
        f"{counts['jobs']} jobs, {counts['outreach']} outreach messages and "
        f"{counts['questions']} unanswered questions"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.seed", description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reset", action="store_true", help="remove the sample rows, then seed")
    mode.add_argument("--clear", action="store_true", help="remove the sample rows without seeding")
    args = parser.parse_args(argv)

    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    target = engine.url.render_as_string(hide_password=True)
    try:
        if args.clear:
            print(f"Removed {_counts_text(clear(engine))} of sample data from {target}.")
            return 0
        counts = seed(engine, reset=args.reset)
    except SeedRefused as exc:
        print(f"Refusing to seed {target}: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    if counts is None:
        print(f"Database already has data; nothing seeded ({target}). Use --reset to reload the sample data.")
    else:
        print(f"Seeded {_counts_text(counts)} into {target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
