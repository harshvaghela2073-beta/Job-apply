"""Replacing the database with an uploaded SQLite file (``PUT /api/admin/database``).

The upload is written to a temp file next to the database and checked: a SQLite file that passes
``PRAGMA integrity_check`` and has this app's ``job``, ``template`` and ``profile_field`` tables.
It is then copied *into* the live database with SQLite's backup API. That is one write transaction:
the engine's pooled connections (and any other reader) see either the old or the new data, the
file is never swapped under an open connection, and no stale ``-wal`` / ``-shm`` file can end up
paired with the new content. Afterwards missing tables and columns are added (``init_db``).

The ``connection`` rows are kept from the server's own database: they describe the logins saved in
this server's browser profiles, not the uploaded data.
"""

import logging
import sqlite3
from contextlib import closing, suppress
from pathlib import Path

from sqlalchemy import Engine, func
from sqlmodel import Session, select

from app.db import init_db
from app.models import Connection, Job, ProfileField, Template

logger = logging.getLogger(__name__)

REQUIRED_TABLES = ("job", "template", "profile_field")
SQLITE_HEADER = b"SQLite format 3\x00"


class InvalidDatabase(ValueError):
    """The uploaded file can't be used; the message says why (shown to the user)."""


class NotAFileDatabase(RuntimeError):
    pass


def sqlite_file(engine: Engine) -> Path:
    """The live database file. Raises ``NotAFileDatabase`` for in-memory or non-SQLite databases."""
    database = engine.url.database if engine.url.get_backend_name() == "sqlite" else None
    if not database or database == ":memory:" or database.startswith("file:"):
        raise NotAFileDatabase("Only a SQLite database file can be replaced.")
    return Path(database)


def _page_size(path: Path) -> int:
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        return int(connection.execute("PRAGMA page_size").fetchone()[0])


def validate_upload(path: Path, page_size: int) -> None:
    """Raise ``InvalidDatabase`` unless ``path`` is an intact database with this app's tables.

    The temp file is then turned into a rollback-journal file with the live database's page size:
    copying into a WAL database with the backup API needs equal page sizes.
    """
    with path.open("rb") as fh:
        if fh.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            raise InvalidDatabase("That isn't a SQLite database file.")
    try:
        with closing(sqlite3.connect(path, isolation_level=None)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            result = [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]
            if result != ["ok"]:
                raise InvalidDatabase(f"The database file is damaged ({'; '.join(result[:3])}).")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            missing = [table for table in REQUIRED_TABLES if table not in tables]
            if missing:
                raise InvalidDatabase(
                    f"That database doesn't have this app's tables (missing: {', '.join(missing)})."
                )
            journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
            current = int(connection.execute("PRAGMA page_size").fetchone()[0])
            if journal != "delete" or current != page_size:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute(f"PRAGMA page_size={int(page_size)}")
                connection.execute("VACUUM")
    except sqlite3.DatabaseError as error:
        raise InvalidDatabase(f"That isn't a usable SQLite database ({error}).") from error


def _copy_into(upload: Path, target: Path) -> None:
    with closing(sqlite3.connect(upload)) as source, closing(sqlite3.connect(target, timeout=30)) as live:
        source.backup(live)
        with suppress(sqlite3.Error):  # shrink the WAL the copy went through
            live.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def replace_database(engine: Engine, upload: Path) -> None:
    """Check ``upload`` and make it the live database (see the module docstring). The caller keeps
    scans from starting meanwhile."""
    target = sqlite_file(engine)
    validate_upload(upload, _page_size(target))
    with Session(engine) as session:
        logins = [row.model_dump() for row in session.exec(select(Connection)).all()]
    _copy_into(upload, target)
    engine.dispose()  # not required (the copy is visible to open connections too): fresh connections from here on
    init_db(engine)
    with Session(engine) as session:
        for row in session.exec(select(Connection)).all():
            session.delete(row)
        session.flush()
        for login in logins:
            session.add(Connection(**login))
        session.commit()
    logger.info("The database was replaced by an upload")


def counts(engine: Engine) -> dict[str, int]:
    with Session(engine) as session:
        return {
            "jobs": session.exec(select(func.count()).select_from(Job)).one(),
            "templates": session.exec(select(func.count()).select_from(Template)).one(),
            "profile_fields": session.exec(select(func.count()).select_from(ProfileField)).one(),
        }
