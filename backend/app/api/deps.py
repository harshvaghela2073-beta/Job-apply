"""Shared FastAPI dependencies."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy import func
from sqlmodel import Session, select
from sqlmodel.sql.expression import SelectOfScalar

from app.config import Settings


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
SessionDep = Annotated[Session, Depends(get_session)]


@dataclass
class Pagination:
    limit: int
    offset: int


def get_pagination(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


PaginationDep = Annotated[Pagination, Depends(get_pagination)]


def paginate(session: Session, stmt: SelectOfScalar, page: Pagination, *order_by) -> tuple[list, int]:
    """Run ``stmt`` with ordering and pagination; return (items, total before pagination)."""
    total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
    items = session.exec(stmt.order_by(*order_by).offset(page.offset).limit(page.limit)).all()
    return list(items), total
