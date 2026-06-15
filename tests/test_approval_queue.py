"""approval_queue + fleet_status SLA: the human-approval queue with wait age.

Turns the bare ``awaiting_human`` count into an actionable queue (issue #37):
per-run wait age dated from when the human was first asked, oldest first, with
an optional SLA breach flag.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import AuditEventType, FoundryAuditEvent
from foundry.memory.metrics import approval_queue, fleet_status
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
            created_at=updated_at,
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
        q = approval_queue(session, now=NOW)
    assert q["count"] == 0
    assert q["oldest_wait_seconds"] is None
    assert q["sla_breaches"] == 0
    assert q["runs"] == []


def test_queue_dates_wait_from_audit_trail_not_updated_at(session_factory) -> None:
    """An N-of-M partial sign-off advances ``updated_at`` while the run stays
    parked - the wait clock must run from APPROVAL_REQUESTED, not be reset."""
    with session_factory() as session:
        # Asked 3h ago; a partial approval touched the row 1h ago. The clock is
        # the 3h, not the 1h.
        rid = _add_run(
            session,
            status=RunStatus.WAITING_APPROVAL,
            updated_at=NOW - timedelta(hours=1),
            risk=OverallRisk.MEDIUM,
        )
        _add_event(
            session, rid, AuditEventType.APPROVAL_REQUESTED, NOW - timedelta(hours=3)
        )
        session.commit()

        q = approval_queue(session, now=NOW)

    assert q["count"] == 1
    entry = q["runs"][0]
    assert entry["run_id"] == rid
    assert entry["status"] == "waiting_approval"
    assert entry["risk_level"] == "medium"
    assert entry["waiting_seconds"] == 3 * 3600
    assert entry["waiting_since"] == (NOW - timedelta(hours=3)).isoformat()


def test_review_required_dates_from_risk_escalated(session_factory) -> None:
    with session_factory() as session:
        rid = _add_run(
            session,
            status=RunStatus.REVIEW_REQUIRED,
            updated_at=NOW - timedelta(hours=5),
        )
        _add_event(
            session, rid, AuditEventType.RISK_ESCALATED, NOW - timedelta(minutes=30)
        )
        session.commit()
        q = approval_queue(session, now=NOW)
    assert q["runs"][0]["waiting_seconds"] == 1800


def test_falls_back_to_updated_at_without_wait_event(session_factory) -> None:
    """needs_clarification (and remediation-denied review) have no wait-start
    event - the run's last-touch time is when it entered the parked state."""
    with session_factory() as session:
        _add_run(
            session,
            status=RunStatus.NEEDS_CLARIFICATION,
            updated_at=NOW - timedelta(hours=2),
        )
        session.commit()
        q = approval_queue(session, now=NOW)
    assert q["count"] == 1
    assert q["runs"][0]["waiting_seconds"] == 2 * 3600


def test_only_human_wait_states_are_queued(session_factory) -> None:
    with session_factory() as session:
        _add_run(
            session,
            status=RunStatus.WAITING_APPROVAL,
            updated_at=NOW - timedelta(minutes=10),
        )
        # An agent is running and a run finished - neither is parked on a human.
        _add_run(
            session, status=RunStatus.AGENT_RUNNING, updated_at=NOW - timedelta(hours=9)
        )
        _add_run(
            session, status=RunStatus.COMPLETE, updated_at=NOW - timedelta(hours=9)
        )
        session.commit()
        q = approval_queue(session, now=NOW)
    assert q["count"] == 1
    assert q["runs"][0]["status"] == "waiting_approval"


def _seed_three(session) -> None:
    a = _add_run(
        session, status=RunStatus.WAITING_APPROVAL, updated_at=NOW - timedelta(hours=1)
    )
    _add_event(session, a, AuditEventType.APPROVAL_REQUESTED, NOW - timedelta(hours=3))
    _add_run(
        session, status=RunStatus.NEEDS_CLARIFICATION, updated_at=NOW - timedelta(hours=2)
    )
    b = _add_run(
        session, status=RunStatus.REVIEW_REQUIRED, updated_at=NOW - timedelta(hours=9)
    )
    _add_event(session, b, AuditEventType.RISK_ESCALATED, NOW - timedelta(minutes=30))


def test_oldest_first_and_sla_breaches(session_factory) -> None:
    with session_factory() as session:
        _seed_three(session)
        session.commit()
        # 1h SLA: the 3h and 2h waits breach; the 30m one does not.
        q = approval_queue(session, now=NOW, sla_seconds=3600)

    waits = [r["waiting_seconds"] for r in q["runs"]]
    assert waits == [3 * 3600, 2 * 3600, 1800]  # oldest first
    assert q["oldest_wait_seconds"] == 3 * 3600
    assert q["sla_seconds"] == 3600
    assert q["sla_breaches"] == 2
    assert [r["sla_breached"] for r in q["runs"]] == [True, True, False]


def test_no_sla_means_no_breach_signal(session_factory) -> None:
    with session_factory() as session:
        _seed_three(session)
        session.commit()
        q = approval_queue(session, now=NOW, sla_seconds=None)
    assert q["sla_seconds"] is None
    assert q["sla_breaches"] == 0
    assert all(r["sla_breached"] is False for r in q["runs"])
    # Oldest wait is still reported - it is useful without an SLA configured.
    assert q["oldest_wait_seconds"] == 3 * 3600


def test_future_dated_row_is_not_negative_wait(session_factory) -> None:
    with session_factory() as session:
        _add_run(
            session,
            status=RunStatus.WAITING_APPROVAL,
            updated_at=NOW + timedelta(minutes=5),  # clock skew
        )
        session.commit()
        q = approval_queue(session, now=NOW)
    assert q["runs"][0]["waiting_seconds"] == 0


def test_fleet_status_carries_queue_summary(session_factory) -> None:
    with session_factory() as session:
        _seed_three(session)
        session.commit()
        fleet = fleet_status(session, sla_seconds=3600, now=NOW)
    assert fleet["awaiting_human"] == 3
    assert fleet["oldest_wait_seconds"] == 3 * 3600
    assert fleet["approval_sla_seconds"] == 3600
    assert fleet["approvals_breaching_sla"] == 2


def test_fleet_status_without_sla(session_factory) -> None:
    with session_factory() as session:
        _seed_three(session)
        session.commit()
        fleet = fleet_status(session, now=NOW)
    assert fleet["approval_sla_seconds"] is None
    assert fleet["approvals_breaching_sla"] == 0
    assert fleet["oldest_wait_seconds"] == 3 * 3600
