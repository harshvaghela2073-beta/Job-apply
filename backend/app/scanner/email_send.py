"""``email_send``: emailing the hiring posts that ask for one (docs/API_CONTRACT.md, "Automatic email
and LinkedIn outreach"; decided by the candidate on 2026-10-14). Runs after ``linkedin_posts`` while
the ``auto_send_email`` switch is on.

- **Which jobs:** jobs found in the last ``PENDING_DAYS`` days (today and yesterday: posts the daily
  cap held back on 2026-10-14 were otherwise never sent) with apply type ``email``, status ``found``
  and a ``contact_email``, most important (lowest ``priority``) first. Since 2026-10-27 such posts
  are kept only when someone is hiring in Bengaluru or remote in India
  (``app.sources.linkedin.hiring``).
- **The message:** the first non-empty ``email`` template, filled in (``app.services.outreach``),
  with the resume at ``RESUME_PATH`` attached, sent through Gmail SMTP (``app.services.mailer``).
- **Limits:** at most ``EMAIL_MAX_PER_RUN`` attempts per run and ``EMAIL_DAILY_CAP`` sent per day;
  an address that was sent an email in the last ``DEDUPE_DAYS`` days is never emailed again (the job
  becomes ``skipped`` with the reason); a random ``EMAIL_WAIT_SECONDS`` pause between emails (only
  between attempts that reached the mail server).
- **Recording:** each email is an ``OutreachMessage`` (kind ``email``): ``sent`` with ``sent_at``
  (the job becomes ``applied``, "Emailed <address>"), or ``failed`` with the error when the server
  refused that message or recipient (the job becomes ``failed``).
- When no email can go out at all (wrong password, no connection), the run stops with the error
  and nothing is recorded: the jobs stay ``found`` and are tried again on a later run.
- Missing setup (no ``SMTP_APP_PASSWORD`` / ``SMTP_USER``, no resume, no email template) skips the
  run with a message saying so.
"""

import logging
import random
from datetime import timedelta

from sqlmodel import Session, col, select

from app.models import ApplyType, Job, JobStatus, OutreachKind, OutreachMessage, OutreachStatus, ScanKind, ScanStatus
from app.scanner.base import ScanContext, ScanOutcome
from app.scanner.fit import company_reason, skip_listed
from app.sources.linkedin.parse import clean_email, foreign_email
from app.services.app_settings import AUTO_SEND_EMAIL, get_flag
from app.services.mailer import Mailer, MailError, SmtpConfig, build_email
from app.services.outreach import (
    DraftProblem,
    clean_job_title,
    email_template,
    first_name,
    known_company,
    last_emailed,
    sent_today,
)
from app.timeutils import local_day_bounds, to_utc, utcnow

logger = logging.getLogger(__name__)

EMAIL_MAX_PER_RUN = 5
EMAIL_WAIT_SECONDS = (30.0, 90.0)
DEDUPE_DAYS = 30
PENDING_DAYS = 2


def pending_email_jobs(session: Session, now) -> list[Job]:
    """``email`` jobs found since yesterday with status ``found`` and an address, most important first."""
    start, _ = local_day_bounds(to_utc(now) - timedelta(days=PENDING_DAYS - 1))
    return list(
        session.exec(
            select(Job)
            .where(
                Job.apply_type == ApplyType.email,
                Job.status == JobStatus.found,
                col(Job.contact_email).is_not(None),
                col(Job.found_at) >= start,
            )
            .order_by(col(Job.priority).asc().nulls_last(), col(Job.found_at).desc(), col(Job.id))
        ).all()
    )


class EmailSender:
    """Runs after the hiring-post scan: emails today's posts that ask for one, while the switch is on."""

    kind = ScanKind.email_send

    def __init__(self, wait_range=EMAIL_WAIT_SECONDS, rng=random.uniform, transport_factory=None, sleep=None):
        self.wait_range = wait_range
        self.rng = rng
        self.transport_factory = transport_factory  # None: real SMTP (app.services.mailer.default_transport)
        # ``sleep(seconds, stop_event) -> bool`` (False when stopped); default: wait on the stop event.
        self.sleep = sleep
        self.pauses: list[float] = []  # the pauses taken in the last run (seconds)

    def _pause(self, ctx: ScanContext) -> bool:
        seconds = self.rng(*self.wait_range)
        self.pauses.append(seconds)
        if self.sleep is not None:
            return self.sleep(seconds, ctx.stop) and not ctx.stop.is_set()
        return not ctx.stop.wait(seconds)

    def run(self, ctx: ScanContext) -> ScanOutcome:
        self.pauses = []
        settings = ctx.settings
        with Session(ctx.engine) as session:
            if not get_flag(session, AUTO_SEND_EMAIL):
                return ScanOutcome(status=ScanStatus.skipped, message="Automatic emails are off.")
        config = SmtpConfig.from_settings(settings)
        problem = config.problem()
        if problem:
            return ScanOutcome(status=ScanStatus.skipped, message=f"{problem}, so no emails were sent.")
        if not settings.resume_path.is_file():
            return ScanOutcome(
                status=ScanStatus.skipped, message="The resume file (RESUME_PATH) is missing, so no emails were sent."
            )
        with Session(ctx.engine) as session:
            template = email_template(session)
            if template is None:
                return ScanOutcome(
                    status=ScanStatus.skipped,
                    message="There is no email template in the Template Library, so no emails were sent.",
                )
            cap = settings.email_daily_cap
            already_sent = sent_today(session, OutreachKind.email, ctx.now)
            if already_sent >= cap:
                return ScanOutcome(status=ScanStatus.skipped, message=f"Today's limit of {cap} emails is reached.")
            job_ids = [job.id for job in pending_email_jobs(session, ctx.now)]
        if not job_ids:
            return ScanOutcome(status=ScanStatus.ok, message="No new hiring-post emails to send.")

        mailer = Mailer(config, self.transport_factory)
        since = to_utc(ctx.now) - timedelta(days=DEDUPE_DAYS)
        counts = {"sent": 0, "failed": 0, "duplicate": 0, "skip_list": 0, "unusable": 0}
        attempts = 0
        failure = None
        for job_id in job_ids:
            if ctx.stop.is_set() or attempts >= EMAIL_MAX_PER_RUN or already_sent + counts["sent"] >= cap:
                break
            with Session(ctx.engine) as session:
                job = session.get(Job, job_id)
                if job is None or job.status != JobStatus.found or not job.contact_email:
                    continue
                # Saved before the skip list existed (2026-10-15): never email a skip-listed company.
                listed = skip_listed([job.company], ctx.prefs.skip_companies)
                if listed:
                    job.status = JobStatus.skipped
                    job.status_reason = f"Not emailed: {company_reason(listed)}"
                    session.add(job)
                    session.commit()
                    counts["skip_list"] += 1
                    continue
                # Saved before the address and location checks (2026-10-23): a malformed address or a
                # company abroad (when the post names no Bengaluru location) isn't emailed.
                address = clean_email(job.contact_email)
                why = None
                if address is None:
                    why = f"{job.contact_email} isn't a usable email address."
                elif foreign_email(address) and "bengaluru" not in (job.location or "").lower():
                    why = f"{address} belongs to a company abroad."
                if why:
                    job.status = JobStatus.skipped
                    job.status_reason = f"Not emailed: {why}"
                    session.add(job)
                    session.commit()
                    counts["unusable"] += 1
                    continue
                previous = last_emailed(session, address, since)
                if previous is not None:
                    job.status = JobStatus.skipped
                    job.status_reason = f"Not emailed: {address} was already emailed on {to_utc(previous):%Y-%m-%d}."
                    session.add(job)
                    session.commit()
                    counts["duplicate"] += 1
                    continue
                try:
                    message = build_email(
                        template,
                        config=config,
                        to=address,
                        resume_path=settings.resume_path,
                        job_title=job.title,
                        company=known_company(job.company, job.contact_name),
                        name=first_name(job.contact_name),
                    )
                except (DraftProblem, OSError) as error:
                    failure = f"{error}; nothing was sent."  # the template (or resume) is the problem for every job
                    break
                subject = str(message["Subject"])
                body = message.get_body(("plain",)).get_content().strip()
                recipient = job.contact_name or address
                title = clean_job_title(job.title)
            if attempts and not self._pause(ctx):
                break
            attempts += 1
            try:
                mailer.send(message)
            except MailError as error:
                if error.connection:
                    failure = error.text
                    logger.warning("Email to %s for %s not sent: %s", address, title, error.text)
                    break
                self._record(ctx, job_id, address, recipient, subject, body, error=error.text)
                counts["failed"] += 1
                logger.warning("Email to %s for %s failed: %s", address, title, error.text)
            else:
                self._record(ctx, job_id, address, recipient, subject, body)
                counts["sent"] += 1
                logger.info("Emailed %s for %s", address, title)

        parts = [f"Emailed {counts['sent']}"]
        if counts["duplicate"]:
            parts.append(f"{counts['duplicate']} skipped (address already emailed in the last {DEDUPE_DAYS} days)")
        if counts["skip_list"]:
            parts.append(f"{counts['skip_list']} skipped (on your skip list)")
        if counts["unusable"]:
            parts.append(f"{counts['unusable']} skipped (abroad or not a real address)")
        if counts["failed"]:
            parts.append(f"{counts['failed']} refused by the mail server")
        message_text = ", ".join(parts) + "."
        if failure:
            message_text += f" Stopped: {failure}"
        status = ScanStatus.failed if failure and not counts["sent"] and not counts["failed"] else ScanStatus.ok
        return ScanOutcome(status=status, found=attempts + counts["duplicate"], new=counts["sent"], message=message_text)

    @staticmethod
    def _record(ctx: ScanContext, job_id: int, address: str, recipient: str, subject: str, body: str, *, error=None) -> None:
        now = utcnow()
        with Session(ctx.engine) as session:
            session.add(
                OutreachMessage(
                    kind=OutreachKind.email,
                    job_id=job_id,
                    recipient_name=recipient,
                    recipient_email=address,
                    subject=subject,
                    body=body,
                    status=OutreachStatus.failed if error else OutreachStatus.sent,
                    error=error,
                    sent_at=None if error else now,
                )
            )
            job = session.get(Job, job_id)
            if job is not None:
                if error:
                    job.status = JobStatus.failed
                    job.status_reason = f"Email to {address} failed: {error}"[:1000]
                else:
                    job.status = JobStatus.applied
                    job.applied_at = now
                    job.status_reason = f"Emailed {address}"
                session.add(job)
            session.commit()
