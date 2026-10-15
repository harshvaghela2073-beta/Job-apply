"""Sending email through Gmail SMTP (STARTTLS on port 587) with the resume attached.

Settings: ``SMTP_HOST``, ``SMTP_PORT``, ``SMTP_USER``, ``SMTP_FROM_NAME`` and ``SMTP_APP_PASSWORD``
(a Gmail app password; spaces are ignored) from ``backend/.env``. The password is never logged,
returned or included in an error message.

``Mailer`` takes a transport factory ``(host, port, timeout) -> smtplib.SMTP``-like object, so tests
use a fake; the real one is ``default_transport``.
"""

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from app.config import Settings
from app.models import Template
from app.services.outreach import clean_job_title, fill_placeholders, ready_text

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 30.0
TEST_SUBJECT_PREFIX = "[TEST] "
TEST_JOB_TITLE = "Software Engineer"
DEFAULT_SUBJECT = "Application for [Job title]"
# Marks failures caused by this computer's connection (``app.scheduler.runner.LOCAL_PROBLEM_MARKERS``).
UNREACHABLE = "Couldn't reach the mail server"


class MailError(RuntimeError):
    """Sending failed. ``connection`` is True when no email could go out at all (login, connection,
    TLS, sender refused), False when only this message or recipient was refused."""

    def __init__(self, text: str, *, connection: bool):
        super().__init__(text)
        self.text = text
        self.connection = connection


class Transport(Protocol):
    def ehlo(self, name: str = "") -> object: ...

    def starttls(self, *, context: ssl.SSLContext | None = None) -> object: ...

    def login(self, user: str, password: str) -> object: ...

    def send_message(self, msg: EmailMessage) -> dict: ...

    def quit(self) -> object: ...

    def close(self) -> None: ...


def default_transport(host: str, port: int, timeout: float) -> Transport:
    return smtplib.SMTP(host, port, timeout=timeout)


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    from_name: str = ""
    password: SecretStr = field(default_factory=lambda: SecretStr(""), repr=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> "SmtpConfig":
        return cls(
            host=settings.smtp_host,
            port=settings.smtp_port,
            user=settings.smtp_user,
            from_name=settings.smtp_from_name,
            password=settings.smtp_app_password,
        )

    def problem(self) -> str | None:
        """What's missing before anything can be sent, or None."""
        if not self.password.get_secret_value():
            return "SMTP_APP_PASSWORD isn't set in backend/.env"
        if not self.user:
            return "SMTP_USER isn't set in backend/.env"
        return None

    @property
    def sender(self) -> str:
        return formataddr((self.from_name, self.user)) if self.from_name else self.user


def _response_text(error: smtplib.SMTPResponseException) -> str:
    message = error.smtp_error
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="replace")
    return f"{error.smtp_code} {' '.join(str(message).split())}".strip()


class Mailer:
    def __init__(self, config: SmtpConfig, transport_factory=None):
        self.config = config
        self.transport_factory = transport_factory

    def _scrub(self, text: str) -> str:
        """Never let the password into a message, whatever the server or library echoed."""
        password = self.config.password.get_secret_value()
        if password:
            grouped = " ".join(password[i : i + 4] for i in range(0, len(password), 4))
            for secret in (password, grouped):
                text = text.replace(secret, "********")
        return text

    def _error(self, text: str, *, connection: bool) -> MailError:
        return MailError(self._scrub(text)[:500], connection=connection)

    def send(self, message: EmailMessage) -> None:
        """Log in (STARTTLS) and send ``message``. Raises ``MailError``."""
        config = self.config
        factory = self.transport_factory or default_transport
        where = f"{config.host}:{config.port}"
        try:
            smtp = factory(config.host, config.port, SMTP_TIMEOUT_SECONDS)
        except smtplib.SMTPResponseException as error:
            raise self._error(f"The mail server {where} refused the connection: {_response_text(error)}", connection=True) from None
        except (OSError, smtplib.SMTPException) as error:
            raise self._error(f"{UNREACHABLE} {where}: {error or type(error).__name__}", connection=True) from None
        try:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(config.user, config.password.get_secret_value())
            refused = smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as error:
            raise self._error(
                f"The mail server didn't accept SMTP_USER and SMTP_APP_PASSWORD: {_response_text(error)}", connection=True
            ) from None
        except smtplib.SMTPRecipientsRefused as error:
            details = "; ".join(
                f"{to}: {code} {msg.decode('utf-8', errors='replace') if isinstance(msg, bytes) else msg}"
                for to, (code, msg) in error.recipients.items()
            )
            raise self._error(f"The mail server refused the recipient ({details})", connection=False) from None
        except smtplib.SMTPSenderRefused as error:
            raise self._error(f"The mail server refused the sender {config.user}: {_response_text(error)}", connection=True) from None
        except smtplib.SMTPDataError as error:
            raise self._error(f"The mail server rejected the email: {_response_text(error)}", connection=False) from None
        except smtplib.SMTPResponseException as error:
            raise self._error(f"The mail server said: {_response_text(error)}", connection=True) from None
        except smtplib.SMTPException as error:
            raise self._error(f"Sending failed: {error or type(error).__name__}", connection=True) from None
        except OSError as error:
            raise self._error(f"{UNREACHABLE} {where}: {error or type(error).__name__}", connection=True) from None
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001 - the connection may already be gone
                try:
                    smtp.close()
                except Exception:  # noqa: BLE001
                    pass
        if refused:
            raise self._error(f"The mail server refused: {', '.join(refused)}", connection=False)


def build_email(
    template: Template,
    *,
    config: SmtpConfig,
    to: str,
    resume_path: Path,
    job_title: str,
    company: str | None = None,
    name: str | None = None,
    test: bool = False,
) -> EmailMessage:
    """The email for one job, from ``template``, with the resume attached. ``name`` is the first
    name. Raises ``DraftProblem`` when placeholders are left, ``FileNotFoundError`` without a resume."""
    title = clean_job_title(job_title)
    values = {"job_title": title, "company": company, "name": name}
    subject = ready_text(fill_placeholders(template.subject or DEFAULT_SUBJECT, **values), "email subject")
    body = ready_text(fill_placeholders(template.answer, **values), "email template")
    if test:
        subject = TEST_SUBJECT_PREFIX + subject
    resume = Path(resume_path)
    data = resume.read_bytes()
    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = to
    message["Subject"] = " ".join(subject.split())
    message["Date"] = formatdate(localtime=True)
    domain = config.user.rsplit("@", 1)[-1] if "@" in config.user else None
    message["Message-ID"] = make_msgid(domain=domain)
    message.set_content(body.strip() + "\n")
    message.add_attachment(data, maintype="application", subtype="pdf", filename=resume.name)
    return message
