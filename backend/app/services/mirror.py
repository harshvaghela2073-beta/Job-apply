"""The profile mirror file: a generated, read-only markdown copy of the database.

``PROFILE_PATH`` is never read by the backend. After every profile or template change the whole
file is regenerated from the ``ProfileField`` and ``Template`` tables, so it always matches the
database and git keeps its history (see "Profile mirror file" in docs/API_CONTRACT.md).

- Profile sections become ``| Field | Value |`` tables. In cells ``|`` is escaped as ``\\|`` and
  line breaks become ``<br>``.
- Templates follow, grouped by kind. Each is a ``###`` heading (its question or name), an optional
  ``Subject: ...`` line, then the full text as plain paragraphs.
- Writes are atomic (temp file in the same directory, then ``os.replace``) and serialised with a
  process-wide lock.
"""

import logging
import threading
from pathlib import Path

from sqlmodel import Session

from app.config import Settings
from app.models import Template, TemplateKind
from app.services.files import atomic_write_bytes
from app.services.profile import group_sections, list_fields
from app.services.templates import list_templates

logger = logging.getLogger(__name__)

NOTICE = (
    "> **Generated file. Do not edit by hand.** The Job-apply backend rewrites this whole file "
    "from its database after every profile or template change, so manual edits are lost. "
    "Change templates in the app's Template Library; change profile details by asking Claude (or through the API)."
)
TITLE = "# Candidate Profile"
KIND_HEADINGS = {
    TemplateKind.answer: "Application answers",
    TemplateKind.linkedin_message: "LinkedIn messages",
    TemplateKind.email: "Emails",
}

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Rendering


def _one_line(text: str) -> str:
    return " ".join(text.split())


def escape_cell(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text.replace("|", "\\|").replace("\n", "<br>")


def render_mirror(session: Session) -> str:
    blocks = [NOTICE, TITLE]

    sections = group_sections(list_fields(session))
    if not sections:
        blocks.append("_No profile fields yet._")
    for title, fields in sections:
        rows = [f"| {escape_cell(f.label)} | {escape_cell(f.value)} |" for f in fields]
        blocks.append(f"## {_one_line(title)}")
        blocks.append("\n".join(["| Field | Value |", "|---|---|", *rows]))

    by_kind: dict[TemplateKind, list[Template]] = {kind: [] for kind in TemplateKind}
    for template in list_templates(session):
        by_kind[TemplateKind(template.kind)].append(template)
    for kind, templates in by_kind.items():
        blocks.append(f"## {KIND_HEADINGS[kind]}")
        if not templates:
            blocks.append("_None yet._")
        for template in templates:
            blocks.append(f"### {_one_line(template.question)}")
            if template.subject:
                blocks.append(f"Subject: {_one_line(template.subject)}")
            text = template.answer.replace("\r\n", "\n").replace("\r", "\n").strip()
            blocks.append(text or "_No answer yet._")

    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Writing


def _atomic_write(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def write_mirror(session: Session, settings: Settings) -> None:
    """Regenerate the whole mirror file from the database. Raises if the file can't be written."""
    with _write_lock:
        # Render inside the lock so the last write always reflects the latest committed state.
        _atomic_write(settings.profile_path, render_mirror(session))


def refresh_mirror(session: Session, settings: Settings) -> bool:
    """``write_mirror``, but a failure is logged instead of raised: the database is the source of
    truth, so a failed mirror write must not fail the API request. Returns whether it worked."""
    try:
        write_mirror(session, settings)
    except Exception:
        logger.exception("Could not write the profile mirror file %s", settings.profile_path)
        return False
    return True
