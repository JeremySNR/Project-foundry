"""Data model tests against an in-memory SQLite database."""

from __future__ import annotations

import pytest

from foundry.audit import build_artifact, build_audit_event
from foundry.db import (
    ArtifactType,
    AuditEventType,
    FoundryRun,
    create_all,
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
