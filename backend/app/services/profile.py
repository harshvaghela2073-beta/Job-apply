"""The candidate profile: ``ProfileField`` rows in the database (see docs/API_CONTRACT.md)."""

from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models import ProfileField
from app.schemas import (
    ProfileFieldRead,
    ProfileFieldWrite,
    ProfileRead,
    ProfileSectionRead,
    ProfileSummaryRead,
    ResumeRead,
)

RESUME_URL = "/api/profile/resume"

# Summary values that come straight from the field with the same key.
SUMMARY_KEYS = (
    "name",
    "email",
    "phone",
    "location",
    "current_company",
    "designation",
    "total_experience",
    "current_ctc",
    "graduation",
    "linkedin_url",
    "naukri_url",
)
SKILLS_KEY = "skills"


def list_fields(session: Session) -> list[ProfileField]:
    """Every field, by position (then id, so equal positions keep insertion order)."""
    return list(
        session.exec(select(ProfileField).order_by(col(ProfileField.position), col(ProfileField.id)))
    )


def group_sections(fields: Iterable[ProfileField]) -> list[tuple[str, list[ProfileField]]]:
    """Group fields by section. Sections are ordered by their lowest position (first seen wins a
    tie); fields keep the order they were given in, which should be by position."""
    sections: dict[str, list[ProfileField]] = {}
    for field in fields:
        sections.setdefault(field.section, []).append(field)
    return sorted(sections.items(), key=lambda item: min(f.position for f in item[1]))


def split_skills(value: str | None) -> list[str]:
    if not value:
        return []
    return [skill.strip() for skill in value.split(",") if skill.strip()]


def resume_file(resume_path: Path) -> Path | None:
    return resume_path if resume_path.is_file() else None


def build_profile(session: Session, resume_path: Path) -> ProfileRead:
    fields = list_fields(session)
    values = {field.key: field.value.strip() or None for field in fields}
    resume = resume_file(resume_path)
    summary = ProfileSummaryRead(
        **{key: values.get(key) for key in SUMMARY_KEYS},
        skills=split_skills(values.get(SKILLS_KEY)),
        resume=ResumeRead(filename=resume.name, url=RESUME_URL) if resume else None,
    )
    sections = [
        ProfileSectionRead(
            title=title,
            fields=[ProfileFieldRead.model_validate(field) for field in section_fields],
        )
        for title, section_fields in group_sections(fields)
    ]
    return ProfileRead(summary=summary, sections=sections)


def upsert_fields(session: Session, items: list[ProfileFieldWrite]) -> None:
    """Insert or update each field by key, in order, and commit.

    A position that is left out keeps an existing field's position; a new field gets the highest
    position so far (including fields earlier in ``items``) plus one.
    """
    keys = {item.key for item in items}
    existing = {
        field.key: field
        for field in session.exec(select(ProfileField).where(col(ProfileField.key).in_(keys)))
    }
    next_position = (session.exec(select(func.max(ProfileField.position))).one() or 0) + 1
    for item in items:
        field = existing.get(item.key)
        if field is None:
            field = ProfileField(key=item.key, label=item.label, section=item.section)
            field.position = item.position if item.position is not None else next_position
            existing[item.key] = field
        elif item.position is not None:
            field.position = item.position
        field.label = item.label
        field.section = item.section
        field.value = item.value
        next_position = max(next_position, field.position + 1)
        session.add(field)
    session.commit()


def delete_field(session: Session, key: str) -> bool:
    field = session.exec(select(ProfileField).where(ProfileField.key == key)).first()
    if field is None:
        return False
    session.delete(field)
    session.commit()
    return True
