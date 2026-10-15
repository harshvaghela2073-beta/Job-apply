"""Storing scan results: new items become ``Job`` rows with status ``found``; existing ones
(same platform and external id) are left alone."""

import logging
from collections.abc import Iterable

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.models import Job, JobStatus, Platform, ScanStatus
from app.scanner.base import Candidate, ScanContext, ScanOutcome, ScanReport

logger = logging.getLogger(__name__)


def dedupe(candidates: Iterable[Candidate]) -> list[Candidate]:
    """One candidate per (platform, external_id), keeping the most important (lowest priority);
    the first seen wins a tie. Order of first appearance is kept."""
    best: dict[tuple[Platform, str], Candidate] = {}
    for candidate in candidates:
        key = (Platform(candidate.platform), candidate.external_id)
        current = best.get(key)
        if current is None or candidate.priority < current.priority:
            best[key] = candidate
    return list(best.values())


def known_ids(session: Session, platform: Platform, external_ids: Iterable[str]) -> set[str]:
    ids = list(set(external_ids))
    if not ids:
        return set()
    found: set[str] = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        found.update(
            session.exec(
                select(Job.external_id).where(Job.platform == platform, col(Job.external_id).in_(chunk))
            ).all()
        )
    return found


def _to_job(candidate: Candidate) -> Job:
    return Job(
        platform=candidate.platform,
        external_id=candidate.external_id,
        title=candidate.title,
        company=candidate.company,
        location=candidate.location,
        url=candidate.url,
        apply_url=candidate.apply_url,
        form_url=candidate.form_url,
        contact_email=candidate.contact_email,
        post_url=candidate.post_url,
        short_description=candidate.short_description,
        apply_type=candidate.apply_type,
        status=JobStatus.found,
        posted_at=candidate.posted_at,
        experience=candidate.experience,
        matched_role=candidate.matched_role,
        priority=candidate.priority,
        contact_name=candidate.contact_name,
        contact_profile_url=candidate.contact_profile_url,
    )


def store_candidates(engine: Engine, candidates: Iterable[Candidate]) -> tuple[int, int]:
    """Insert the candidates that aren't stored yet. Returns ``(found, new)``: the number of
    distinct candidates and how many of them were inserted."""
    unique = dedupe(candidates)
    new = 0
    with Session(engine) as session:
        by_platform: dict[Platform, list[Candidate]] = {}
        for candidate in unique:
            by_platform.setdefault(Platform(candidate.platform), []).append(candidate)
        for platform, items in by_platform.items():
            existing = known_ids(session, platform, (c.external_id for c in items))
            for candidate in items:
                if candidate.external_id in existing:
                    continue
                session.add(_to_job(candidate))
                try:
                    session.commit()
                except IntegrityError:  # stored meanwhile by someone else: leave it alone
                    session.rollback()
                    logger.info("Job %s/%s already stored", platform, candidate.external_id)
                    continue
                new += 1
    return len(unique), new


def finish_scan(ctx: ScanContext, platform: Platform, report: ScanReport) -> ScanOutcome:
    """Store a scanner's results, update the platform's login state, and build the outcome."""
    if report.login_lost:
        ctx.connections.record_login_lost(platform, report.login_lost)
    elif report.login_ok:
        ctx.connections.record_login_ok(platform)
    found, new = store_candidates(ctx.engine, report.candidates)
    notes = list(report.notes)
    if report.failure:
        notes.insert(0, report.failure)
    return ScanOutcome(
        status=ScanStatus.failed if report.failure else ScanStatus.ok,
        found=found,
        new=new,
        message=" ".join(notes) or None,
        page_loads=report.page_loads,
    )
