"""failure_queue: the failure-side triage feed for the fleet dashboard.

The complement to the three in-flight queues (approval/execution/review, issue
#37): turns the bare blocked/execution-failed counts into an actionable incident
feed - per-run age dated from the failure event, with the reason read from that
event's audit metadata, **newest first**, bounded to a recent window (a failed
run is terminal and never drains, so the queue is a recency-ordered feed, not an
all-time list).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import AuditEventType, FoundryAuditEvent
from foundry.memory.metrics import failure_queue
from foundry.schemas.common import OverallRisk, RunStatus

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
# A generous default window for tests that don't care about the boundary.
SINCE = NOW - timedelta(days=30)


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
    session,
    run_id: str,
    event_type: AuditEventType,
    created_at: datetime,
    *,
    metadata_json: str | None = None,
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
            metadata_json=metadata_json,
            created_at=created_at,
        )
    )


def test_empty_queue(session_factory) -> None:
    with session_factory() as session:
        q = failure_queue(session, since=SINCE, now=NOW)
    assert q["count"] == 0
    assert q["runs"] == []
    assert q["newest_failure_seconds"] is None
    assert q["oldest_failure_seconds"] is None
    assert q["blocked"] == 0
    assert q["failed"] == 0


def test_lists_blocked_run_dated_and_reasoned_from_event(session_factory) -> None:
    with session_factory() as session:
        rid = _add_run(
            session,
            status=RunStatus.BLOCKED,
            updated_at=NOW - timedelta(minutes=5),
            risk=OverallRisk.HIGH,
            current_step="blocked",
        )
        _add_event(
            session,
            rid,
            AuditEventType.RUN_BLOCKED,
            NOW - timedelta(hours=2),
            metadata_json='{"category": "policy_denied"}',
        )
        session.commit()
        q = failure_queue(session, since=SINCE, now=NOW)
    assert q["count"] == 1
    entry = q["runs"][0]
    assert entry["run_id"] == rid
    assert entry["status"] == "blocked"
    assert entry["risk_level"] == "high"
    assert entry["reason"] == "policy_denied"
    assert entry["failed_seconds"] == 2 * 3600
    assert entry["failed_since"] == (NOW - timedelta(hours=2)).isoformat()
    assert q["blocked"] == 1
    assert q["failed"] == 0


def test_execution_failed_dated_from_agent_failed_event(session_factory) -> None:
    with session_factory() as session:
        rid = _add_run(
            session,
            status=RunStatus.EXECUTION_FAILED,
            updated_at=NOW - timedelta(minutes=1),
        )
        _add_event(
            session,
            rid,
            AuditEventType.AGENT_FAILED,
            NOW - timedelta(minutes=30),
            metadata_json='{"reason": "agent error"}',
        )
        session.commit()
        q = failure_queue(session, since=SINCE, now=NOW)
    assert q["count"] == 1
    entry = q["runs"][0]
    assert entry["status"] == "execution_failed"
    assert entry["reason"] == "agent error"
    assert entry["failed_seconds"] == 30 * 60
    assert q["blocked"] == 0
    assert q["failed"] == 1


def test_newest_first_ordering_and_summary(session_factory) -> None:
    with session_factory() as session:
        old = _add_run(
            session, status=RunStatus.BLOCKED, updated_at=NOW - timedelta(minutes=1)
        )
        _add_event(session, old, AuditEventType.RUN_BLOCKED, NOW - timedelta(hours=10))
        mid = _add_run(
            session,
            status=RunStatus.EXECUTION_FAILED,
            updated_at=NOW - timedelta(minutes=1),
        )
        _add_event(session, mid, AuditEventType.AGENT_FAILED, NOW - timedelta(hours=3))
        new = _add_run(
            session, status=RunStatus.BLOCKED, updated_at=NOW - timedelta(minutes=1)
        )
        _add_event(session, new, AuditEventType.RUN_BLOCKED, NOW - timedelta(minutes=15))
        session.commit()
        q = failure_queue(session, since=SINCE, now=NOW)
    ages = [r["failed_seconds"] for r in q["runs"]]
    assert ages == [15 * 60, 3 * 3600, 10 * 3600]  # newest first
    assert q["newest_failure_seconds"] == 15 * 60
    assert q["oldest_failure_seconds"] == 10 * 3600
    assert q["blocked"] == 2
    assert q["failed"] == 1


def test_window_excludes_old_failures(session_factory) -> None:
    """A run that failed before the ``since`` boundary is an old incident, not
    part of the live feed - the queue is bounded, unlike the all-time run board."""
    with session_factory() as session:
        recent = _add_run(
            session, status=RunStatus.BLOCKED, updated_at=NOW - timedelta(minutes=1)
        )
        _add_event(session, recent, AuditEventType.RUN_BLOCKED, NOW - timedelta(days=2))
        stale = _add_run(
            session, status=RunStatus.EXECUTION_FAILED, updated_at=NOW
        )
        _add_event(session, stale, AuditEventType.AGENT_FAILED, NOW - timedelta(days=20))
        session.commit()
        q = failure_queue(session, since=NOW - timedelta(days=7), now=NOW)
    assert q["count"] == 1
    assert q["runs"][0]["run_id"] == recent


def test_only_failure_states_are_queued(session_factory) -> None:
    with session_factory() as session:
        blocked = _add_run(
            session, status=RunStatus.BLOCKED, updated_at=NOW - timedelta(minutes=1)
        )
        _add_event(session, blocked, AuditEventType.RUN_BLOCKED, NOW - timedelta(hours=1))
        # Not failures: a parked run, an in-flight agent, an open PR, a merge, a
        # rejection (a deliberate human "no", not an incident), a re-triggerable
        # needs-clarification.
        _add_run(session, status=RunStatus.WAITING_APPROVAL, updated_at=NOW)
        _add_run(session, status=RunStatus.AGENT_RUNNING, updated_at=NOW)
        _add_run(session, status=RunStatus.PR_OPEN, updated_at=NOW)
        _add_run(session, status=RunStatus.COMPLETE, updated_at=NOW)
        _add_run(session, status=RunStatus.REJECTED, updated_at=NOW)
        _add_run(session, status=RunStatus.NEEDS_CLARIFICATION, updated_at=NOW)
        session.commit()
        q = failure_queue(session, since=SINCE, now=NOW)
    assert q["count"] == 1
    assert q["runs"][0]["status"] == "blocked"


def test_marker_scoped_to_status(session_factory) -> None:
    """A stray AGENT_FAILED left on a run that *later* got BLOCKED must not date
    (or reason) the block: only RUN_BLOCKED marks entry into BLOCKED."""
    with session_factory() as session:
        rid = _add_run(
            session, status=RunStatus.BLOCKED, updated_at=NOW - timedelta(minutes=1)
        )
        # A failed re-dispatch earlier in the run's life...
        _add_event(
            session,
            rid,
            AuditEventType.AGENT_FAILED,
            NOW - timedelta(hours=9),
            metadata_json='{"reason": "agent error"}',
        )
        # ...then the human stopped it, which is what put it in BLOCKED.
        _add_event(
            session,
            rid,
            AuditEventType.RUN_BLOCKED,
            NOW - timedelta(hours=1),
            metadata_json='{"category": "human_stopped"}',
        )
        session.commit()
        q = failure_queue(session, since=SINCE, now=NOW)
    entry = q["runs"][0]
    assert entry["failed_seconds"] == 3600  # the block, not the 9h-ago failure
    assert entry["reason"] == "human_stopped"


def test_latest_marker_wins(session_factory) -> None:
    """A run can be blocked, escalated/retried, blocked again; the *latest*
    RUN_BLOCKED is the current entry into the state."""
    with session_factory() as session:
        rid = _add_run(
            session, status=RunStatus.BLOCKED, updated_at=NOW - timedelta(minutes=1)
        )
        _add_event(session, rid, AuditEventType.RUN_BLOCKED, NOW - timedelta(hours=5))
        _add_event(
            session,
            rid,
            AuditEventType.RUN_BLOCKED,
            NOW - timedelta(minutes=20),
            metadata_json='{"category": "forbidden_path"}',
        )
        session.commit()
        q = failure_queue(session, since=SINCE, now=NOW)
    entry = q["runs"][0]
    assert entry["failed_seconds"] == 20 * 60
    assert entry["reason"] == "forbidden_path"


def test_falls_back_to_created_at_without_event(session_factory) -> None:
    """Defensive: a failed run with no recorded marker event dates from its
    immutable created_at (not the drift-prone updated_at) with an unknown reason."""
    with session_factory() as session:
        _add_run(
            session,
            status=RunStatus.EXECUTION_FAILED,
            created_at=NOW - timedelta(hours=4),
            updated_at=NOW - timedelta(minutes=2),  # a later row touch
        )
        session.commit()
        q = failure_queue(session, since=SINCE, now=NOW)
    entry = q["runs"][0]
    assert entry["failed_seconds"] == 4 * 3600
    assert entry["reason"] is None


def test_future_dated_failure_is_not_negative(session_factory) -> None:
    with session_factory() as session:
        rid = _add_run(session, status=RunStatus.BLOCKED, updated_at=NOW)
        _add_event(
            session, rid, AuditEventType.RUN_BLOCKED, NOW + timedelta(minutes=5)
        )  # clock skew
        session.commit()
        q = failure_queue(session, since=SINCE, now=NOW)
    assert q["runs"][0]["failed_seconds"] == 0


def test_malformed_metadata_yields_no_reason(session_factory) -> None:
    """A read-only reporting path must never raise on a malformed metadata row."""
    with session_factory() as session:
        a = _add_run(session, status=RunStatus.BLOCKED, updated_at=NOW)
        _add_event(
            session, a, AuditEventType.RUN_BLOCKED, NOW - timedelta(minutes=5),
            metadata_json="not json{",
        )
        b = _add_run(session, status=RunStatus.BLOCKED, updated_at=NOW)
        _add_event(
            session, b, AuditEventType.RUN_BLOCKED, NOW - timedelta(minutes=6),
            metadata_json="[1, 2, 3]",  # valid json, but not an object
        )
        c = _add_run(session, status=RunStatus.BLOCKED, updated_at=NOW)
        _add_event(
            session, c, AuditEventType.RUN_BLOCKED, NOW - timedelta(minutes=7),
            metadata_json=None,
        )
        session.commit()
        q = failure_queue(session, since=SINCE, now=NOW)
    assert q["count"] == 3
    assert all(r["reason"] is None for r in q["runs"])
