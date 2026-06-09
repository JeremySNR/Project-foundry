"""SQLAlchemy declarative base and a tiny engine/session helper.

The foundation uses SQLite by default so the data model is exercisable in tests
without Postgres. Production uses Postgres via ``FOUNDRY_DATABASE_URL``.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def _assign_audit_sequences(session, flush_context, instances) -> None:
    """Give new audit events their monotonic per-run sequence numbers.

    The audit trail promises a guaranteed order independent of timestamp ties;
    that only holds if something actually assigns the numbers. Done here, at
    flush time, so every code path that adds an event gets it for free.
    """
    from .models import FoundryAuditEvent

    new_events = [obj for obj in session.new if isinstance(obj, FoundryAuditEvent)]
    if not new_events:
        return
    by_run: dict[str, list[FoundryAuditEvent]] = {}
    for evt in new_events:
        by_run.setdefault(evt.run_id, []).append(evt)
    for run_id, events in by_run.items():
        current = session.execute(
            select(func.max(FoundryAuditEvent.sequence)).where(
                FoundryAuditEvent.run_id == run_id
            )
        ).scalar()
        next_seq = (current + 1) if current is not None else 0
        for evt in events:
            evt.sequence = next_seq
            next_seq += 1


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
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    event.listen(factory, "before_flush", _assign_audit_sequences)
    return factory


def create_all(engine) -> None:
    # Import models so they are registered on the metadata before create_all.
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
