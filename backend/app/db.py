"""Database engine creation and table setup."""

import logging
from pathlib import Path

from sqlalchemy import Engine, event, inspect
from sqlalchemy.schema import CreateColumn
from sqlmodel import SQLModel, create_engine

from app import models  # noqa: F401  (registers the tables on SQLModel.metadata)

logger = logging.getLogger(__name__)


def _is_sqlite(engine: Engine) -> bool:
    return engine.url.get_backend_name() == "sqlite"


def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    # The LinkedIn and Naukri scans run at the same time (2026-10-15): with WAL, readers don't block
    # the writer (no-op for an in-memory database).
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def create_db_engine(database_url: str) -> Engine:
    connect_args = {}
    if database_url.startswith("sqlite"):
        # FastAPI runs sync endpoints in a thread pool.
        connect_args["check_same_thread"] = False
        # Two scans may write at once: wait for the other writer instead of "database is locked".
        connect_args["timeout"] = 30
    engine = create_engine(database_url, connect_args=connect_args)
    if _is_sqlite(engine):
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def add_missing_columns(engine: Engine) -> list[str]:
    """Add model columns that an existing table doesn't have yet. Returns ``["table.column", ...]``.

    ``create_all`` creates missing tables but never alters existing ones. This covers the one schema
    change that is always safe: adding a **nullable** column with no default (existing rows get
    NULL). Anything else (a NOT NULL column, a type change, a dropped column) is logged and left
    alone; that needs a real migration.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    statements: list[tuple[str, str]] = []
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            if not column.nullable or column.primary_key or column.server_default is not None:
                logger.warning(
                    "Column %s.%s is missing but can't be added automatically (not nullable).",
                    table.name,
                    column.name,
                )
                continue
            ddl = str(CreateColumn(column).compile(dialect=engine.dialect))
            statements.append((f"{table.name}.{column.name}", f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}'))
    if statements:
        with engine.begin() as connection:
            for _, statement in statements:
                connection.exec_driver_sql(statement)
        logger.info("Added columns: %s", ", ".join(name for name, _ in statements))
    return [name for name, _ in statements]


def init_db(engine: Engine) -> None:
    """Create the SQLite directory if needed, create any missing tables, then add any missing
    nullable columns to existing tables (see ``add_missing_columns``)."""
    if _is_sqlite(engine):
        database = engine.url.database
        if database and database != ":memory:" and not database.startswith("file:"):
            Path(database).parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    add_missing_columns(engine)
