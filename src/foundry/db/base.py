"""SQLAlchemy declarative base and a tiny engine/session helper.

The foundation uses SQLite by default so the data model is exercisable in tests
without Postgres. Production uses Postgres via ``FOUNDRY_DATABASE_URL``.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, sessionmaker, with_loader_criteria
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def _assign_audit_sequences(session, flush_context, instances) -> None:
    """Give new audit events their per-run sequence *and* chain hash.

    The audit trail promises a guaranteed order independent of timestamp ties;
    that only holds if something actually assigns the numbers. Done here, at
    flush time, so every code path that adds an event gets it for free.

    Each new event is also linked into a tamper-evident hash chain: its
    ``content_hash`` commits to the previous event's hash for the run, so
    dropping, reordering, or editing any row is detectable on verification
    (issue #36). The link function lives in ``foundry.audit.events`` so the
    write side here and the read side in ``compliance.evidence`` share one
    definition and cannot drift. Imported lazily to avoid an import cycle
    (``audit.events`` imports the ORM models, which import this module).
    """
    from foundry.audit.events import AUDIT_CHAIN_GENESIS, audit_event_chain_hash

    from .models import FoundryAuditEvent

    new_events = [obj for obj in session.new if isinstance(obj, FoundryAuditEvent)]
    if not new_events:
        return
    by_run: dict[str, list[FoundryAuditEvent]] = {}
    for evt in new_events:
        by_run.setdefault(evt.run_id, []).append(evt)
    for run_id, events in by_run.items():
        # The current chain tip: the highest-sequence event already persisted for
        # this run, with its hash. One query gives both the next sequence and the
        # hash to chain off.
        tip = session.execute(
            select(FoundryAuditEvent.sequence, FoundryAuditEvent.content_hash)
            .where(FoundryAuditEvent.run_id == run_id)
            .order_by(FoundryAuditEvent.sequence.desc())
            .limit(1)
        ).first()
        if tip is None:
            next_seq = 0
            prev_hash = AUDIT_CHAIN_GENESIS
        else:
            next_seq = tip.sequence + 1
            # A legacy tip (written before the chain existed) has no hash; start a
            # fresh chain from genesis rather than retroactively rewriting history.
            prev_hash = tip.content_hash or AUDIT_CHAIN_GENESIS
        for evt in events:
            evt.sequence = next_seq
            evt.content_hash = audit_event_chain_hash(prev_hash, evt)
            prev_hash = evt.content_hash
            next_seq += 1


def _stamp_tenant_org(session, flush_context, instances) -> None:
    """Stamp the active org onto new tenant-scoped rows (issue #156).

    Every row in a tenant-scoped table carries an ``org_id``. The value comes
    from the current tenant context (``db.tenant``), which a request handler
    binds from the *authenticated principal* (invariant #5) and which defaults
    to the single default org otherwise — so a single-tenant deployment stamps
    everything under that one org and is byte-for-byte unchanged. Done at flush
    time so every write path gets it for free, before the column's own default
    would apply. Imported lazily to avoid an import cycle (``models`` imports
    this module).
    """
    from foundry.db.tenant import current_org_id

    from .models import TENANT_SCOPED_MODELS

    org = current_org_id()
    for obj in session.new:
        if isinstance(obj, TENANT_SCOPED_MODELS) and getattr(obj, "org_id", None) is None:
            obj.org_id = org


def _apply_tenant_filter(orm_execute_state) -> None:
    """Filter every ORM SELECT to the current org (issue #156).

    A ``with_loader_criteria`` per tenant-scoped entity restricts the statement
    to ``org_id == current_org_id()`` wherever that entity appears — top-level,
    a relationship load, or an aliased join — so a unit of work scoped to one
    org can never read another org's rows. It is applied to SELECTs *and* to
    ORM-enabled UPDATE/DELETE statements, so a query-scoped bulk write can't
    cross the org boundary either (an ORM object update/delete is already safe,
    since the object could only have been loaded in-org). Column refreshes of an
    already-loaded object are skipped (applying criteria there is invalid and
    the object is already org-scoped). With the default org in scope
    (single-tenant) the predicate matches every row, so behaviour is unchanged.
    Imported lazily to avoid an import cycle.
    """
    if orm_execute_state.is_column_load:
        return
    if not (
        orm_execute_state.is_select
        or orm_execute_state.is_update
        or orm_execute_state.is_delete
    ):
        return

    from foundry.db.tenant import current_org_id

    from .models import TENANT_SCOPED_MODELS

    org = current_org_id()
    orm_execute_state.statement = orm_execute_state.statement.options(
        *(
            with_loader_criteria(model, model.org_id == org, include_aliases=True)
            for model in TENANT_SCOPED_MODELS
        )
    )


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
    # Tenant org stamped before sequences/hashes so a new row already carries its
    # org when the audit chain is computed (issue #156).
    event.listen(factory, "before_flush", _stamp_tenant_org)
    event.listen(factory, "before_flush", _assign_audit_sequences)
    event.listen(factory, "do_orm_execute", _apply_tenant_filter)
    return factory


def create_all(engine) -> None:
    # Import models so they are registered on the metadata before create_all.
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)


def init_schema(engine) -> None:
    """Create the schema for the dev/test SQLite backend; Alembic owns the rest.

    SQLite development and test databases have no migration step, so ``create_all``
    is the schema owner there. On other backends (Postgres in production) Alembic
    migrations are the *single* owner: running ``create_all`` would create the
    tables without stamping ``alembic_version``, stranding a later
    ``alembic upgrade head`` (it would start at base and fail on the existing
    tables). So we skip it on non-SQLite and rely on ``alembic upgrade head``
    instead — the Docker entrypoint runs it on startup, and ``make migrate``
    runs it by hand. This is the one-owner-per-backend resolution of the
    ``create_all`` vs Alembic conflict.
    """
    if engine.dialect.name == "sqlite":
        create_all(engine)
