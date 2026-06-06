"""SQLAlchemy declarative base and a tiny engine/session helper.

The foundation uses SQLite by default so the data model is exercisable in tests
without Postgres. Production uses Postgres via ``FOUNDRY_DATABASE_URL``.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    url = url or os.environ.get("FOUNDRY_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, future=True, connect_args=connect_args)


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def create_all(engine) -> None:
    # Import models so they are registered on the metadata before create_all.
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
