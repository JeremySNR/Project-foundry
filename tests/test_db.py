"""Data model tests against an in-memory SQLite database."""

from __future__ import annotations

import pytest

from foundry.audit import build_artifact, build_audit_event
from foundry.db import (
    ArtifactType,
    AuditEventType,
    FoundryRun,
    create_all,
    init_schema,
    make_engine,
    make_session_factory,
)
from foundry.schemas import TicketAnalysis
from foundry.schemas.common import OverallRisk, RunStatus


@pytest.fixture
def session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        yield s


def test_init_schema_creates_tables_on_sqlite() -> None:
    """SQLite dev/test DBs have no migration step, so init_schema bootstraps them."""
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_schema(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        # Querying a mapped table only succeeds if the schema was created.
        assert s.query(FoundryRun).count() == 0


def test_init_schema_skips_non_sqlite(monkeypatch) -> None:
    """On Postgres, Alembic is the single schema owner; init_schema must not
    run create_all (that would create tables without stamping alembic_version,
    stranding a later `alembic upgrade head`)."""
    calls = []
    monkeypatch.setattr(
        "foundry.db.base.create_all", lambda engine: calls.append(engine)
    )

    class _FakeDialect:
        name = "postgresql"

    class _FakeEngine:
        dialect = _FakeDialect()

    init_schema(_FakeEngine())
    assert calls == []


def test_run_persists_with_artifacts_and_events(
    session, ready_analysis: TicketAnalysis
) -> None:
    run = FoundryRun(
        id="run-1",
        linear_issue_id="issue-uuid",
        linear_issue_key="LIN-123",
        status=RunStatus.ANALYSING,
        trigger_type="label",
        risk_level=OverallRisk.LOW,
    )
    run.artifacts.append(
        build_artifact(
            run_id="run-1",
            artifact_type=ArtifactType.TICKET_ANALYSIS,
            content=ready_analysis,
        )
    )
    run.audit_events.append(
        build_audit_event(
            run_id="run-1",
            event_type=AuditEventType.RUN_STARTED,
            actor_type="foundry",
        )
    )
    session.add(run)
    session.commit()

    fetched = session.get(FoundryRun, "run-1")
    assert fetched.linear_issue_key == "LIN-123"
    assert len(fetched.artifacts) == 1
    assert fetched.artifacts[0].artifact_type is ArtifactType.TICKET_ANALYSIS
    assert len(fetched.audit_events) == 1


def test_cascade_delete_removes_children(session) -> None:
    run = FoundryRun(
        id="run-2",
        linear_issue_id="i2",
        linear_issue_key="LIN-2",
        trigger_type="comment_command",
    )
    run.audit_events.append(
        build_audit_event(
            run_id="run-2",
            event_type=AuditEventType.RUN_STARTED,
            actor_type="foundry",
        )
    )
    session.add(run)
    session.commit()

    session.delete(run)
    session.commit()

    from foundry.db import FoundryAuditEvent

    assert session.query(FoundryAuditEvent).count() == 0


def test_run_outcome_roundtrip_and_upsert(session) -> None:
    from datetime import datetime, timezone

    from foundry.db.models import FoundryRunOutcome

    run = FoundryRun(
        id="run-3",
        linear_issue_id="i3",
        linear_issue_key="ENG-3",
        status=RunStatus.COMPLETE,
        trigger_type="label",
    )
    session.add(run)
    now = datetime.now(timezone.utc)
    session.add(
        FoundryRunOutcome(
            run_id="run-3",
            linear_issue_id="i3",
            issue_key_prefix="ENG",
            outcome="merged",
            repo="acme/billing-service",
            trigger_type="label",
            created_at_run=now,
            completed_at=now,
            jobs_count=2,
        )
    )
    session.commit()

    fetched = session.get(FoundryRunOutcome, "run-3")
    assert fetched.outcome == "merged"
    assert fetched.jobs_count == 2
    assert fetched.block_justified is None

    # Upsert by primary key: merge replaces, never duplicates.
    session.merge(
        FoundryRunOutcome(
            run_id="run-3",
            linear_issue_id="i3",
            issue_key_prefix="ENG",
            outcome="blocked",
            blocked_reason_category="human_stopped",
            trigger_type="label",
            created_at_run=now,
            jobs_count=2,
        )
    )
    session.commit()
    assert session.query(FoundryRunOutcome).count() == 1
    assert session.get(FoundryRunOutcome, "run-3").outcome == "blocked"
