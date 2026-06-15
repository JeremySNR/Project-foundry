"""review_queue + fleet_status review SLA: open PRs with review-latency age.

The review-side complement to the approval and execution queues (issue #37):
turns the bare ``prs_open`` count into an actionable queue - per-run review age
dated from the PR_OPENED event (when the PR first opened), oldest first, with an
optional review SLA breach flag (the "PRs sitting unreviewed for N hours"
signal). The product deliberately stops at a reviewed PR, so this is pure
read-only visibility - it blocks no run and merges nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import AuditEventType, FoundryAuditEvent
from foundry.memory.metrics import fleet_status, review_queue
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
            actor_type="agent",
            created_at=created_at,
        )
    )


def test_empty_queue(session_factory) -> None:
    with session_factory() as session:
        q = review_queue(session, now=NOW)
    assert q["count"] == 0
    assert q["oldest_unreviewed_seconds"] is None
    assert q["sla_breaches"] == 0
    assert q["runs"] == []


def test_lists_open_pr_dated_from_pr_opened(session_factory) -> None:
    with session_factory() as session:
        rid = _add_run(
            session,
            status=RunStatus.PR_OPEN,
            updated_at=NOW - timedelta(minutes=5),
            risk=OverallRisk.MEDIUM,
        )
        _add_event(session, rid, AuditEventType.PR_OPENED, NOW - timedelta(hours=2))
        session.commit()
        q = review_queue(session, now=NOW)
    assert q["count"] == 1
    entry = q["runs"][0]
    assert entry["run_id"] == rid
    assert entry["status"] == "pr_open"
    assert entry["risk_level"] == "medium"
    assert entry["unreviewed_seconds"] == 2 * 3600
    assert entry["pr_opened_since"] == (NOW - timedelta(hours=2)).isoformat()


def test_later_push_does_not_reset_review_clock(session_factory) -> None:
    """A PR sees PR_OPENED once; later pushes emit PR_UPDATED. The review clock is
    anchored to when the PR opened, so a re-push does not reset it - the PR has
    been awaiting review the whole time."""
    with session_factory() as session:
        rid = _add_run(
            session,
            status=RunStatus.PR_OPEN,
            updated_at=NOW - timedelta(minutes=10),
        )
        _add_event(session, rid, AuditEventType.PR_OPENED, NOW - timedelta(hours=5))
        # A push 20m ago is a PR_UPDATED, not a new PR_OPENED - ignored by the clock.
        _add_event(session, rid, AuditEventType.PR_UPDATED, NOW - timedelta(minutes=20))
        session.commit()
        q = review_queue(session, now=NOW)
    assert q["runs"][0]["unreviewed_seconds"] == 5 * 3600


def test_falls_back_to_created_at_without_pr_opened_event(session_factory) -> None:
    """Defensive: a run in PR_OPEN with no recorded PR_OPENED dates from its
    immutable created_at, not the drift-prone updated_at."""
    with session_factory() as session:
        _add_run(
            session,
            status=RunStatus.PR_OPEN,
            created_at=NOW - timedelta(hours=3),
            updated_at=NOW - timedelta(minutes=10),  # a later row touch
        )
        session.commit()
        q = review_queue(session, now=NOW)
    assert q["runs"][0]["unreviewed_seconds"] == 3 * 3600


def test_only_pr_open_states_are_queued(session_factory) -> None:
    with session_factory() as session:
        pr = _add_run(
            session,
            status=RunStatus.PR_OPEN,
            updated_at=NOW - timedelta(minutes=10),
        )
        _add_event(session, pr, AuditEventType.PR_OPENED, NOW - timedelta(minutes=10))
        # A run parked on a human (approval queue), an in-flight agent (execution
        # queue), and a finished run - none is a PR awaiting passive review.
        _add_run(session, status=RunStatus.WAITING_APPROVAL, updated_at=NOW)
        _add_run(session, status=RunStatus.REVIEW_REQUIRED, updated_at=NOW)
        _add_run(session, status=RunStatus.AGENT_RUNNING, updated_at=NOW)
        _add_run(session, status=RunStatus.COMPLETE, updated_at=NOW)
        session.commit()
        q = review_queue(session, now=NOW)
    assert q["count"] == 1
    assert q["runs"][0]["status"] == "pr_open"


def _seed_three(session) -> None:
    a = _add_run(
        session, status=RunStatus.PR_OPEN, updated_at=NOW - timedelta(minutes=1)
    )
    _add_event(session, a, AuditEventType.PR_OPENED, NOW - timedelta(hours=3))
    b = _add_run(
        session, status=RunStatus.PR_OPEN, updated_at=NOW - timedelta(minutes=1)
    )
    _add_event(session, b, AuditEventType.PR_OPENED, NOW - timedelta(hours=2))
    c = _add_run(
        session, status=RunStatus.PR_OPEN, updated_at=NOW - timedelta(minutes=1)
    )
    _add_event(session, c, AuditEventType.PR_OPENED, NOW - timedelta(minutes=30))


def test_oldest_first_and_sla_breaches(session_factory) -> None:
    with session_factory() as session:
        _seed_three(session)
        session.commit()
        # 1h SLA: the 3h and 2h PRs breach; the 30m one does not.
        q = review_queue(session, now=NOW, sla_seconds=3600)
    unreviewed = [r["unreviewed_seconds"] for r in q["runs"]]
    assert unreviewed == [3 * 3600, 2 * 3600, 1800]  # oldest first
    assert q["oldest_unreviewed_seconds"] == 3 * 3600
    assert q["sla_seconds"] == 3600
    assert q["sla_breaches"] == 2
    assert [r["sla_breached"] for r in q["runs"]] == [True, True, False]


def test_no_sla_means_no_breach_signal(session_factory) -> None:
    with session_factory() as session:
        _seed_three(session)
        session.commit()
        q = review_queue(session, now=NOW, sla_seconds=None)
    assert q["sla_seconds"] is None
    assert q["sla_breaches"] == 0
    assert all(r["sla_breached"] is False for r in q["runs"])
    # Oldest review age is still reported - useful without an SLA configured.
    assert q["oldest_unreviewed_seconds"] == 3 * 3600


def test_future_dated_pr_open_is_not_negative_review_time(session_factory) -> None:
    with session_factory() as session:
        rid = _add_run(session, status=RunStatus.PR_OPEN, updated_at=NOW)
        _add_event(
            session, rid, AuditEventType.PR_OPENED, NOW + timedelta(minutes=5)
        )  # clock skew
        session.commit()
        q = review_queue(session, now=NOW)
    assert q["runs"][0]["unreviewed_seconds"] == 0


def test_fleet_status_carries_review_summary(session_factory) -> None:
    with session_factory() as session:
        _seed_three(session)
        session.commit()
        fleet = fleet_status(session, review_sla_seconds=3600, now=NOW)
    assert fleet["prs_open"] == 3
    assert fleet["oldest_review_seconds"] == 3 * 3600
    assert fleet["review_sla_seconds"] == 3600
    assert fleet["reviews_breaching_sla"] == 2


def test_fleet_status_without_review_sla(session_factory) -> None:
    with session_factory() as session:
        _seed_three(session)
        session.commit()
        fleet = fleet_status(session, now=NOW)
    assert fleet["review_sla_seconds"] is None
    assert fleet["reviews_breaching_sla"] == 0
    assert fleet["oldest_review_seconds"] == 3 * 3600


def test_fleet_review_sla_is_independent_of_the_other_slas(session_factory) -> None:
    """The three SLAs are distinct knobs: a 2h open PR breaches a 1h review SLA
    without touching the approval- or execution-queue summaries."""
    with session_factory() as session:
        rid = _add_run(
            session,
            status=RunStatus.PR_OPEN,
            updated_at=NOW - timedelta(minutes=1),
        )
        _add_event(session, rid, AuditEventType.PR_OPENED, NOW - timedelta(hours=2))
        session.commit()
        fleet = fleet_status(
            session,
            sla_seconds=14_400,
            execution_sla_seconds=14_400,
            review_sla_seconds=3600,
            now=NOW,
        )
    assert fleet["reviews_breaching_sla"] == 1
    assert fleet["review_sla_seconds"] == 3600
    # No human-parked run and no in-flight agent, so those summaries are empty.
    assert fleet["awaiting_human"] == 0
    assert fleet["approvals_breaching_sla"] == 0
    assert fleet["agents_running"] == 0
    assert fleet["executions_breaching_sla"] == 0
