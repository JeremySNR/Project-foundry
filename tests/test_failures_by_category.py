"""failures_by_category: the aggregate triage cut for the fleet dashboard.

The roll-up complement to ``failure_queue`` (issue #37): where that feed lists
every recent incident newest-first, this groups the same recently-failed runs by
reason so the *systemic* blocker is visible - counts per category (with a
blocked/failed split and the newest/oldest age span), most-frequent first.

Reuses the same ``_failure_event_map`` / ``_FAILURE_EVENTS_BY_STATUS`` derivation
the feed uses, so the count here and the feed there can never disagree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import AuditEventType, FoundryAuditEvent
from foundry.memory.metrics import (
    UNKNOWN_FAILURE_CATEGORY,
    failure_queue,
    failures_by_category,
)
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
    created_at: datetime,
    risk: OverallRisk | None = None,
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
            created_at=created_at,
            updated_at=created_at,
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


def _blocked(session, *, ago: timedelta, category: str | None) -> str:
    rid = _add_run(session, status=RunStatus.BLOCKED, created_at=NOW - ago)
    meta = f'{{"category": "{category}"}}' if category is not None else None
    _add_event(
        session, rid, AuditEventType.RUN_BLOCKED, NOW - ago, metadata_json=meta
    )
    return rid


def _failed(session, *, ago: timedelta, reason: str | None) -> str:
    rid = _add_run(session, status=RunStatus.EXECUTION_FAILED, created_at=NOW - ago)
    meta = f'{{"reason": "{reason}"}}' if reason is not None else None
    _add_event(
        session, rid, AuditEventType.AGENT_FAILED, NOW - ago, metadata_json=meta
    )
    return rid


def test_empty(session_factory) -> None:
    with session_factory() as session:
        report = failures_by_category(session, since=SINCE, now=NOW)
    assert report["count"] == 0
    assert report["blocked"] == 0
    assert report["failed"] == 0
    assert report["distinct_categories"] == 0
    assert report["categories"] == []


def test_groups_by_reason_most_frequent_first(session_factory) -> None:
    with session_factory() as session:
        # 3 policy_denied, 1 budget_exceeded.
        _blocked(session, ago=timedelta(hours=1), category="policy_denied")
        _blocked(session, ago=timedelta(hours=2), category="policy_denied")
        _blocked(session, ago=timedelta(hours=5), category="policy_denied")
        _blocked(session, ago=timedelta(hours=3), category="budget_exceeded")
        session.commit()
        report = failures_by_category(session, since=SINCE, now=NOW)

    assert report["count"] == 4
    assert report["blocked"] == 4
    assert report["failed"] == 0
    assert report["distinct_categories"] == 2
    cats = report["categories"]
    assert [c["category"] for c in cats] == ["policy_denied", "budget_exceeded"]

    top = cats[0]
    assert top["count"] == 3
    assert top["blocked"] == 3
    assert top["failed"] == 0
    # newest of the three is the 1h-ago one, oldest is the 5h-ago one.
    assert top["newest_failure_seconds"] == 1 * 3600
    assert top["oldest_failure_seconds"] == 5 * 3600
    assert top["last_failure"] == (NOW - timedelta(hours=1)).isoformat()


def test_blocked_and_failed_split_within_a_category(session_factory) -> None:
    # The category key comes from category/reason, not the run status, so a
    # blocked run and an execution-failed run can share a reason.
    with session_factory() as session:
        _blocked(session, ago=timedelta(minutes=10), category="timeout")
        _failed(session, ago=timedelta(minutes=20), reason="timeout")
        session.commit()
        report = failures_by_category(session, since=SINCE, now=NOW)

    assert report["distinct_categories"] == 1
    cat = report["categories"][0]
    assert cat["category"] == "timeout"
    assert cat["count"] == 2
    assert cat["blocked"] == 1
    assert cat["failed"] == 1
    assert report["blocked"] == 1
    assert report["failed"] == 1


def test_unknown_reason_bucketed_under_sentinel(session_factory) -> None:
    with session_factory() as session:
        # No metadata at all, and malformed metadata, both land in (unknown).
        _blocked(session, ago=timedelta(minutes=5), category=None)
        rid = _add_run(
            session, status=RunStatus.EXECUTION_FAILED, created_at=NOW - timedelta(minutes=6)
        )
        _add_event(
            session,
            rid,
            AuditEventType.AGENT_FAILED,
            NOW - timedelta(minutes=6),
            metadata_json="not json{",
        )
        session.commit()
        report = failures_by_category(session, since=SINCE, now=NOW)

    assert report["distinct_categories"] == 1
    cat = report["categories"][0]
    assert cat["category"] == UNKNOWN_FAILURE_CATEGORY
    assert cat["count"] == 2


def test_window_excludes_old_failures(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), category="policy_denied")
        _blocked(session, ago=timedelta(days=40), category="policy_denied")  # too old
        session.commit()
        report = failures_by_category(session, since=NOW - timedelta(days=7), now=NOW)

    assert report["count"] == 1
    assert report["categories"][0]["count"] == 1


def test_only_failure_states_counted(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), category="policy_denied")
        # An active run with a (stale) failure-marker event must not be counted.
        live = _add_run(
            session, status=RunStatus.AGENT_RUNNING, created_at=NOW - timedelta(hours=1)
        )
        _add_event(
            session,
            live,
            AuditEventType.AGENT_FAILED,
            NOW - timedelta(hours=1),
            metadata_json='{"reason": "transient"}',
        )
        session.commit()
        report = failures_by_category(session, since=SINCE, now=NOW)

    assert report["count"] == 1
    assert [c["category"] for c in report["categories"]] == ["policy_denied"]


def test_totals_match_the_feed(session_factory) -> None:
    # The roll-up must agree with the per-run feed it complements: same runs,
    # same window, same derivation - so the counts can never drift.
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), category="policy_denied")
        _blocked(session, ago=timedelta(hours=2), category="policy_denied")
        _failed(session, ago=timedelta(hours=3), reason="agent error")
        session.commit()
        feed = failure_queue(session, since=SINCE, now=NOW)
        report = failures_by_category(session, since=SINCE, now=NOW)

    assert report["count"] == feed["count"]
    assert report["blocked"] == feed["blocked"]
    assert report["failed"] == feed["failed"]
    assert sum(c["count"] for c in report["categories"]) == feed["count"]


def test_tie_break_by_most_recent_then_name(session_factory) -> None:
    # Two categories with equal counts: the one whose newest failure is more
    # recent sorts first; a further tie falls back to category name.
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=5), category="alpha")
        _blocked(session, ago=timedelta(minutes=30), category="beta")  # more recent
        session.commit()
        report = failures_by_category(session, since=SINCE, now=NOW)

    assert [c["category"] for c in report["categories"]] == ["beta", "alpha"]
