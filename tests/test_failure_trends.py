"""failure_trends: the over-time cut for the fleet dashboard's failure surface.

The direction-of-travel complement to ``failure_queue`` (the recency feed) and
``failures_by_category`` (the point-in-time roll-up by reason, issue #37): where
those answer "what is failing right now", this buckets the same recently-failed
runs by *when they failed* onto one zero-filled time axis, so a spiking failure
rate is visible at a glance - what ``delivery_trends`` is to the delivery side.

Reuses the same ``_failure_event_map`` / ``_FAILURE_EVENTS_BY_STATUS`` derivation
the feed and the by-category roll-up use, so the totals here can never disagree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import AuditEventType, FoundryAuditEvent
from foundry.memory.metrics import (
    failure_queue,
    failure_trends,
    failures_by_category,
)
from foundry.schemas.common import OverallRisk, RunStatus

# A Wednesday, so day and (Monday-anchored) week buckets are easy to reason about.
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
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


def _blocked(session, *, ago: timedelta, category: str | None = "policy_denied") -> str:
    rid = _add_run(session, status=RunStatus.BLOCKED, created_at=NOW - ago)
    meta = f'{{"category": "{category}"}}' if category is not None else None
    _add_event(session, rid, AuditEventType.RUN_BLOCKED, NOW - ago, metadata_json=meta)
    return rid


def _failed(session, *, ago: timedelta, reason: str | None = "agent error") -> str:
    rid = _add_run(session, status=RunStatus.EXECUTION_FAILED, created_at=NOW - ago)
    meta = f'{{"reason": "{reason}"}}' if reason is not None else None
    _add_event(session, rid, AuditEventType.AGENT_FAILED, NOW - ago, metadata_json=meta)
    return rid


def test_empty(session_factory) -> None:
    with session_factory() as session:
        report = failure_trends(session, since=SINCE, now=NOW)
    assert report["count"] == 0
    assert report["blocked"] == 0
    assert report["failed"] == 0
    assert report["bucket"] == "day"
    assert report["periods"] == []


def test_buckets_by_day_with_blocked_failed_split(session_factory) -> None:
    with session_factory() as session:
        # Two failures on the NOW day (one blocked, one execution-failed)...
        _blocked(session, ago=timedelta(hours=1))
        _failed(session, ago=timedelta(hours=3))
        # ...and one blocked the day before.
        _blocked(session, ago=timedelta(days=1, hours=2))
        session.commit()
        report = failure_trends(session, since=SINCE, now=NOW, bucket="day")

    assert report["count"] == 3
    assert report["blocked"] == 2
    assert report["failed"] == 1

    periods = report["periods"]
    # The axis spans the first to the last populated day, oldest first.
    assert [p["period_start"][:10] for p in periods] == ["2026-06-09", "2026-06-10"]
    assert periods[0] == {
        "period_start": "2026-06-09T00:00:00+00:00",
        "count": 1,
        "blocked": 1,
        "failed": 0,
    }
    assert periods[1] == {
        "period_start": "2026-06-10T00:00:00+00:00",
        "count": 2,
        "blocked": 1,
        "failed": 1,
    }


def test_empty_periods_are_zero_filled(session_factory) -> None:
    # A failure on day 0 and another three days later: the two empty days between
    # are zero-filled so the series reads as continuous (a sparkline gap, not a
    # missing point).
    with session_factory() as session:
        _blocked(session, ago=timedelta(days=3, hours=1))
        _failed(session, ago=timedelta(hours=1))
        session.commit()
        report = failure_trends(session, since=SINCE, now=NOW, bucket="day")

    counts = [p["count"] for p in report["periods"]]
    assert counts == [1, 0, 0, 1]  # day0, gap, gap, day3 - the axis is continuous


def test_week_bucket_collapses_same_week(session_factory) -> None:
    with session_factory() as session:
        # Three failures all in the same (Monday-anchored) week as NOW.
        _blocked(session, ago=timedelta(hours=1))
        _blocked(session, ago=timedelta(days=1))
        _failed(session, ago=timedelta(days=2))
        session.commit()
        report = failure_trends(session, since=SINCE, now=NOW, bucket="week")

    assert report["bucket"] == "week"
    assert len(report["periods"]) == 1
    period = report["periods"][0]
    assert period["period_start"][:10] == "2026-06-08"  # Monday of NOW's week
    assert period["count"] == 3
    assert period["blocked"] == 2
    assert period["failed"] == 1


def test_window_excludes_old_failures(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1))
        _blocked(session, ago=timedelta(days=40))  # older than the window
        session.commit()
        report = failure_trends(session, since=NOW - timedelta(days=7), now=NOW)

    assert report["count"] == 1
    assert sum(p["count"] for p in report["periods"]) == 1


def test_only_failure_states_counted(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1))
        # An active run with a stale failure-marker event must not be counted.
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
        report = failure_trends(session, since=SINCE, now=NOW)

    assert report["count"] == 1


def test_unknown_reason_runs_still_counted(session_factory) -> None:
    # The trend counts failures regardless of whether a reason parses - a run with
    # no/garbled metadata is still a failure on the timeline.
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), category=None)
        session.commit()
        report = failure_trends(session, since=SINCE, now=NOW)

    assert report["count"] == 1
    assert report["blocked"] == 1


def test_totals_match_the_feed_and_rollup(session_factory) -> None:
    # The trend must agree with the per-run feed and the by-category roll-up it
    # sits beside: same runs, same window, same derivation - the totals can't drift.
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1))
        _blocked(session, ago=timedelta(days=2))
        _failed(session, ago=timedelta(days=5))
        session.commit()
        feed = failure_queue(session, since=SINCE, now=NOW)
        rollup = failures_by_category(session, since=SINCE, now=NOW)
        trend = failure_trends(session, since=SINCE, now=NOW)

    assert trend["count"] == feed["count"] == rollup["count"] == 3
    assert trend["blocked"] == feed["blocked"] == rollup["blocked"]
    assert trend["failed"] == feed["failed"] == rollup["failed"]
    assert sum(p["count"] for p in trend["periods"]) == feed["count"]


def test_bad_bucket_rejected(session_factory) -> None:
    with session_factory() as session:
        with pytest.raises(ValueError):
            failure_trends(session, since=SINCE, now=NOW, bucket="month")
