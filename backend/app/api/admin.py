"""Uploading the resume and the database to a deployed backend (docs/API_CONTRACT.md, "Deployment:
login, pushing logins and data"). Both take the file as the raw request body (no multipart)."""

import os
import tempfile
from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session

from app.api.deps import SettingsDep
from app.config import Settings
from app.models import Platform
from app.scheduler.runner import ScansRunning
from app.schemas import DatabaseUploadRead, ResumeUploadRead
from app.services.app_settings import run_interval
from app.services.database import InvalidDatabase, NotAFileDatabase, counts, replace_database, sqlite_file
from app.services.files import atomic_write_bytes
from app.services.mirror import refresh_mirror

router = APIRouter(prefix="/admin", tags=["admin"])

RESUME_MAX_BYTES = 10 * 1024 * 1024
DATABASE_MAX_BYTES = 100 * 1024 * 1024
PDF_TYPES = {"application/pdf"}
DATABASE_TYPES = {"application/octet-stream", "application/vnd.sqlite3", "application/x-sqlite3"}
SCAN_RUNNING = "A scan is running. Try again when it has finished."


def _require_type(request: Request, allowed: set[str]) -> None:
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type not in allowed:
        raise HTTPException(status_code=415, detail=f"Send the file with Content-Type {sorted(allowed)[0]}.")


def _too_large(limit: int) -> HTTPException:
    return HTTPException(status_code=413, detail=f"The file is larger than {limit // (1024 * 1024)} MB.")


async def _body_chunks(request: Request, limit: int):
    """The raw body in chunks; 413 as soon as it passes ``limit`` bytes (Content-Length first)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise _too_large(limit)
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise _too_large(limit)
        if chunk:
            yield chunk


async def read_body(request: Request, limit: int) -> bytes:
    return b"".join([chunk async for chunk in _body_chunks(request, limit)])


@router.put("/resume", response_model=ResumeUploadRead)
async def upload_resume(request: Request, settings: SettingsDep) -> ResumeUploadRead:
    """Save the PDF in the body to ``RESUME_PATH`` (atomically: the old file stays until the new one
    is complete)."""
    _require_type(request, PDF_TYPES)
    data = await read_body(request, RESUME_MAX_BYTES)
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=422, detail="That isn't a PDF file.")
    await run_in_threadpool(atomic_write_bytes, settings.resume_path, data)
    return ResumeUploadRead(bytes=len(data))


@router.put("/database", response_model=DatabaseUploadRead)
async def upload_database(request: Request, settings: SettingsDep) -> DatabaseUploadRead:
    """Replace the database with the SQLite file in the body. 409 while a scan is running."""
    _require_type(request, DATABASE_TYPES)
    state = request.app.state
    try:
        target = sqlite_file(state.engine)
    except NotAFileDatabase as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if state.scans.running:  # before reading up to 100 MB; checked again while holding the scans
        raise HTTPException(status_code=409, detail=SCAN_RUNNING)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.upload-", suffix=".tmp")
    upload = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            async for chunk in _body_chunks(request, DATABASE_MAX_BYTES):
                fh.write(chunk)
        return await run_in_threadpool(_replace, request, upload, settings)
    finally:
        for path in (upload, *(upload.with_name(upload.name + suffix) for suffix in ("-journal", "-wal", "-shm"))):
            with suppress(FileNotFoundError):
                path.unlink()


def _replace(request: Request, upload: Path, settings: Settings) -> DatabaseUploadRead:
    state = request.app.state
    try:
        with state.scans.hold():
            replace_database(state.engine, upload)
            state.scans.startup()  # runs still "running" in the upload were cut off
            state.connections.startup()
            with Session(state.engine) as session:  # the uploaded scan intervals
                for platform in Platform:
                    state.scheduler.set_interval(platform, run_interval(session, platform, settings.scan_interval_minutes))
    except ScansRunning as error:
        raise HTTPException(status_code=409, detail=SCAN_RUNNING) from error
    except InvalidDatabase as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    with Session(state.engine) as session:
        refresh_mirror(session, settings)
    return DatabaseUploadRead(**counts(state.engine))
