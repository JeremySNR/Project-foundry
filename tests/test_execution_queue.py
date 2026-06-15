"""execution_queue + fleet_status execution SLA: in-flight agent runs with age.

The machine-state complement to the approval queue (issue #37): turns the bare
``agents_running`` count into an actionable queue - per-run run-time age dated
from the latest AGENT_STARTED dispatch, oldest first, with an optional execution
SLA breach flag (the hung/runaway-agent signal).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import AuditEventType, FoundryAuditEvent
from foundry.memory.metrics import execution_queue, fleet_status
from foundry.schemas.common import OverallRisk, RunStatus

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


_counter = 0


def _add_run(
    session,
    *,
    status: RunStatus,
    updated_at: datetime,
    created_at: datetime | None = None,
    risk: OverallRisk | None = None,
    current_step: str | None = None,
) -> str:
    global _counter
    _counter += 1
    rid = f"r-{_counter}"
    session.add(
        FoundryRun(
            id=rid,
            linear_issue_id=f"i-{_counter}",
            linear_issue_key=f"ENG-{_counter}",
            status=status,
            trigger_type="label",
            risk_level=risk,
            current_step=current_step,
            created_at=created_at if created_at is not None else updated_at,
            updated_at=updated_at,
        )
    )
    return rid


def _add_event(
    session, run_id: str, event_type: AuditEventType, created_at: datetime
) -> None:
    global _counter
    _counter += 1
    session.add(
        FoundryAuditEvent(
            id=f"e-{_counter}",
            run_id=run_id,
            sequence=_counter,
            event_type=event_type,
            actor_type="foundry",
            created_at=created_at,
        )
    )


def test_empty_queue(session_factory) -> None:
    with session_factory() as session:
        q = execution_queue(session, now=NOW)
    assert q["count"] == 0
    assert q["oldest_running_seconds"] is None
    assert q["sla_breaches"] == 0
    assert q["runs"] == []


def test_lists_in_flight_run_dated_from_agent_started(session_factory) -> None:
    with session_factory() as session:
        rid = _add_run(
            session,
            status=RunStatus.AGENT_RUNNING,
            updated_at=NOW - timedelta(minutes=5),
            risk=OverallRisk.MEDIUM,
        )
        _add_event(
            session, rid, AuditEventType.AGENT_STARTED, NOW - timedelta(hours=2)
        )
        session.commit()
        q = execution_queue(session, now=NOW)
    assert q["count"] == 1
    entry = q["runs"][0]
    assert entry["run_id"] == rid
    assert entry["status"] == "agent_running"
    assert entry["risk_level"] == "medium"
    assert entry["running_seconds"] == 2 * 3600
    assert entry["running_since"] == (NOW - timedelta(hours=2)).isoformat()


def test_retry_redispatch_resets_clock_to_latest_dispatch(session_factory) -> None:
    """A run can be re-dispatched (a remediation retry); the run-time age is the
    *current* attempt - the latest AGENT_STARTED, not the first."""
    with session_factory() as session:
        rid = _add_run(
            session,
            status=RunStatus.AGENT_RUNNING,
            updated_at=NOW - timedelta(hours=6),
        )
        # First dispatch 6h ago, a remediation re-dispatch 20m ago. The clock is
        # the 20m, not the 6h.
        _add_event(
            session, rid, AuditEventType.AGENT_STARTED, NOW - timedelta(hours=6)
        )
        _add_event(
            session, rid, AuditEventType.AGENT_STARTED, NOW - timedelta(minutes=20)
        )
        session.commit()
        q = execution_queue(session, now=NOW)
    assert q["runs"][0]["running_seconds"] == 20 * 60


def test_falls_back_to_created_at_without_dispatch_event(session_factory) -> None:
    """Defensive: a run in AGENT_RUNNING with no recorded AGENT_STARTED dates
    from its immutable created_at, not the drift-prone updated_at."""
    with session_factory() as session:
        _add_run(
            session,
            status=RunStatus.AGENT_RUNNING,
            created_at=NOW - timedelta(hours=3),
            updated_at=NOW - timedelta(minutes=10),  # a later row touch
        )
        session.commit()
        q = execution_queue(session, now=NOW)
    assert q["runs"][0]["running_seconds"] == 3 * 3600


def test_only_agent_running_states_are_queued(session_factory) -> None:
    with session_factory() as session:
        running = _add_run(
            session,
            status=RunStatus.AGENT_RUNNING,
            updated_at=NOW - timedelta(minutes=10),
        )
        _add_event(
            session, running, AuditEventType.AGENT_STARTED, NOW - timedelta(minutes=10)
        )
        # A run parked on a human, a PR open (waiting on CI/reviewers), and a
        # finished run - none is an in-flight agent execution.
        _add_run(
            session, status=RunStatus.WAITING_APPROVAL, updated_at=NOW
        )
        _add_run(session, status=RunStatus.PR_OPEN, updated_at=NOW)
        _add_run(session, status=RunStatus.COMPLETE, updated_at=NOW)
        session.commit()
        q = execution_queue(session, now=NOW)
    assert q["count"] == 1
    assert q["runs"][0]["status"] == "agent_running"


def _seed_three(session) -> None:
    a = _add_run(
        session, status=RunStatus.AGENT_RUNNING, updated_at=NOW - timedelta(minutes=1)
    )
    _add_event(session, a, AuditEventType.AGENT_STARTED, NOW - timedelta(hours=3))
    b = _add_run(
        session, status=RunStatus.AGENT_RUNNING, updated_at=NOW - timedelta(minutes=1)
    )
    _add_event(session, b, AuditEventType.AGENT_STARTED, NOW - timedelta(hours=2))
    c = _add_run(
        session, status=RunStatus.AGENT_RUNNING, updated_at=NOW - timedelta(minutes=1)
    )
    _add_event(session, c, AuditEventType.AGENT_STARTED, NOW - timedelta(minutes=30))


def test_oldest_first_and_sla_breaches(session_factory) -> None:
    with session_factory() as session:
        _seed_three(session)
        session.commit()
        # 1h SLA: the 3h and 2h runs breach; the 30m one does not.
        q = execution_queue(session, now=NOW, sla_seconds=3600)
    running = [r["running_seconds"] for r in q["runs"]]
    assert running == [3 * 3600, 2 * 3600, 1800]  # oldest first
    assert q["oldest_running_seconds"] == 3 * 3600
    assert q["sla_seconds"] == 3600
    assert q["sla_breaches"] == 2
    assert [r["sla_breached"] for r in q["runs"]] == [True, True, False]


def test_no_sla_means_no_breach_signal(session_factory) -> None:
    with session_factory() as session:
        _seed_three(session)
        session.commit()
        q = execution_queue(session, now=NOW, sla_seconds=None)
    assert q["sla_seconds"] is None
    assert q["sla_breaches"] == 0
    assert all(r["sla_breached"] is False for r in q["runs"])
    # Oldest run-time is still reported - useful without an SLA configured.
    assert q["oldest_running_seconds"] == 3 * 3600


def test_future_dated_dispatch_is_not_negative_runtime(session_factory) -> None:
    with session_factory() as session:
        rid = _add_run(
            session,
            status=RunStatus.AGENT_RUNNING,
            updated_at=NOW,
        )
        _add_event(
            session, rid, AuditEventType.AGENT_STARTED, NOW + timedelta(minutes=5)
        )  # clock skew
        session.commit()
        q = execution_queue(session, now=NOW)
    assert q["runs"][0]["running_seconds"] == 0


def test_fleet_status_carries_execution_summary(session_factory) -> None:
    with session_factory() as session:
        _seed_three(session)
        session.commit()
        fleet = fleet_status(session, execution_sla_seconds=3600, now=NOW)
    assert fleet["agents_running"] == 3
    assert fleet["oldest_execution_seconds"] == 3 * 3600
    assert fleet["execution_sla_seconds"] == 3600
    assert fleet["executions_breaching_sla"] == 2


def test_fleet_status_without_execution_sla(session_factory) -> None:
    with session_factory() as session:
        _seed_three(session)
        session.commit()
        fleet = fleet_status(session, now=NOW)
    assert fleet["execution_sla_seconds"] is None
    assert fleet["executions_breaching_sla"] == 0
    assert fleet["oldest_execution_seconds"] == 3 * 3600


def test_fleet_execution_and_approval_slas_are_independent(session_factory) -> None:
    """The two SLAs are distinct knobs: a 2h agent run breaches a 1h execution
    SLA without touching the approval-queue summary."""
    with session_factory() as session:
        rid = _add_run(
            session,
            status=RunStatus.AGENT_RUNNING,
            updated_at=NOW - timedelta(minutes=1),
        )
        _add_event(
            session, rid, AuditEventType.AGENT_STARTED, NOW - timedelta(hours=2)
        )
        session.commit()
        fleet = fleet_status(
            session, sla_seconds=14_400, execution_sla_seconds=3600, now=NOW
        )
    assert fleet["executions_breaching_sla"] == 1
    assert fleet["execution_sla_seconds"] == 3600
    # No human-parked run, so the approval-queue summary is empty.
    assert fleet["awaiting_human"] == 0
    assert fleet["approvals_breaching_sla"] == 0
    assert fleet["approval_sla_seconds"] == 14_400
