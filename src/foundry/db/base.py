"""SQLAlchemy declarative base and a tiny engine/session helper.

The foundation uses SQLite by default so the data model is exercisable in tests
without Postgres. Production uses Postgres via ``FOUNDRY_DATABASE_URL``.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    url = url or os.environ.get("FOUNDRY_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            # Share one connection so every session sees the same in-memory DB
            # (each new SQLite :memory: connection is otherwise a fresh database).
            kwargs["poolclass"] = StaticPool
    return create_engine(url, **kwargs)


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def create_all(engine) -> None:
    # Import models so they are registered on the metadata before create_all.
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
