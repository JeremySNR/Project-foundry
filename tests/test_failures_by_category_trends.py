"""failures_by_category_trends: the by-reason over-time cut for the fleet
dashboard's failure surface (issue #37).

The by-category dimension of ``failure_trends`` - the way
``delivery_by_work_type_trends`` is to ``delivery_trends``. Where the org-wide
``failure_trends`` shows whether we are failing *more* overall and the
point-in-time ``failures_by_category`` roll-up shows *what* is failing most right
now, this answers the question neither can: is a *specific* failure reason
trending up or fading over time?

Reuses the same ``_failure_event_map`` / ``_FAILURE_EVENTS_BY_STATUS`` derivation
the feed, the by-category roll-up and the org-wide trend use, so the totals here
can never disagree with theirs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import AuditEventType, FoundryAuditEvent
from foundry.memory.metrics import (
    UNKNOWN_FAILURE_CATEGORY,
    failure_trends,
    failures_by_category,
    failures_by_category_trends,
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


def _cat(report: dict, name: str) -> dict:
    return next(c for c in report["categories"] if c["category"] == name)


def test_empty(session_factory) -> None:
    with session_factory() as session:
        report = failures_by_category_trends(session, since=SINCE, now=NOW)
    assert report["count"] == 0
    assert report["blocked"] == 0
    assert report["failed"] == 0
    assert report["distinct_categories"] == 0
    assert report["bucket"] == "day"
    assert report["periods"] == []
    assert report["categories"] == []


def test_groups_by_reason_with_aligned_zero_filled_series(session_factory) -> None:
    with session_factory() as session:
        # policy_denied: one on the NOW day, one three days earlier.
        _blocked(session, ago=timedelta(hours=1), category="policy_denied")
        _blocked(session, ago=timedelta(days=3, hours=1), category="policy_denied")
        # budget_exceeded: a single block on the NOW day.
        _blocked(session, ago=timedelta(hours=2), category="budget_exceeded")
        session.commit()
        report = failures_by_category_trends(
            session, since=SINCE, now=NOW, bucket="day"
        )

    assert report["count"] == 3
    assert report["blocked"] == 3
    assert report["failed"] == 0
    assert report["distinct_categories"] == 2

    # One shared axis spanning the first to the last populated day across *all*
    # categories, oldest first, so the per-category series line up column-for-col.
    assert report["periods"] == [
        "2026-06-07T00:00:00+00:00",
        "2026-06-08T00:00:00+00:00",
        "2026-06-09T00:00:00+00:00",
        "2026-06-10T00:00:00+00:00",
    ]

    policy = _cat(report, "policy_denied")
    assert policy["count"] == 2
    assert policy["blocked"] == 2
    assert policy["failed"] == 0
    # day 06-07 has one, the middle two are zero-filled, day 06-10 has one.
    assert [cell["count"] for cell in policy["series"]] == [1, 0, 0, 1]

    budget = _cat(report, "budget_exceeded")
    assert budget["count"] == 1
    # budget only appears on the last day, but its series is aligned to the same
    # 4-period axis (zero-filled on the days it had no failures).
    assert [cell["count"] for cell in budget["series"]] == [0, 0, 0, 1]


def test_blocked_failed_split_per_category(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), category="forbidden_path")
        _failed(session, ago=timedelta(hours=2), reason="forbidden_path")
        session.commit()
        report = failures_by_category_trends(session, since=SINCE, now=NOW)

    cat = _cat(report, "forbidden_path")
    assert cat["count"] == 2
    assert cat["blocked"] == 1
    assert cat["failed"] == 1
    # The single (NOW-day) period carries the same split.
    assert [c["blocked"] for c in cat["series"]] == [1]
    assert [c["failed"] for c in cat["series"]] == [1]


def test_unknown_reason_bucketed_under_sentinel(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), category=None)
        session.commit()
        report = failures_by_category_trends(session, since=SINCE, now=NOW)

    assert report["distinct_categories"] == 1
    cat = report["categories"][0]
    assert cat["category"] == UNKNOWN_FAILURE_CATEGORY
    assert cat["count"] == 1


def test_categories_ordered_most_frequent_then_recent_then_name(session_factory) -> None:
    with session_factory() as session:
        # budget_exceeded: 2 (the most frequent).
        _blocked(session, ago=timedelta(days=1), category="budget_exceeded")
        _blocked(session, ago=timedelta(days=2), category="budget_exceeded")
        # Two singletons tied on count - the more recent one sorts first.
        _blocked(session, ago=timedelta(hours=1), category="zeta")  # newest
        _blocked(session, ago=timedelta(days=4), category="alpha")  # older
        session.commit()
        report = failures_by_category_trends(session, since=SINCE, now=NOW)

    names = [c["category"] for c in report["categories"]]
    # Most-frequent first; then the more-recent singleton (zeta) before the older
    # (alpha) despite alpha sorting first by name - recency wins the tiebreak.
    assert names == ["budget_exceeded", "zeta", "alpha"]


def test_week_bucket_collapses_same_week(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), category="policy_denied")
        _blocked(session, ago=timedelta(days=2), category="policy_denied")
        session.commit()
        report = failures_by_category_trends(
            session, since=SINCE, now=NOW, bucket="week"
        )

    assert report["bucket"] == "week"
    assert report["periods"] == ["2026-06-08T00:00:00+00:00"]  # Monday of NOW's week
    cat = _cat(report, "policy_denied")
    assert [c["count"] for c in cat["series"]] == [2]


def test_window_excludes_old_failures(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), category="policy_denied")
        _blocked(session, ago=timedelta(days=40), category="policy_denied")  # too old
        session.commit()
        report = failures_by_category_trends(
            session, since=NOW - timedelta(days=7), now=NOW
        )

    assert report["count"] == 1
    cat = _cat(report, "policy_denied")
    assert cat["count"] == 1
    assert sum(c["count"] for c in cat["series"]) == 1


def test_only_failure_states_counted(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), category="policy_denied")
        # An active run carrying a stale failure-marker event must not count.
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
        report = failures_by_category_trends(session, since=SINCE, now=NOW)

    assert report["count"] == 1
    assert report["distinct_categories"] == 1


def test_totals_match_the_org_wide_trend_and_rollup(session_factory) -> None:
    # This cut must agree with the org-wide trend and the by-category roll-up it
    # refines: same runs, same window, same derivation - the totals can't drift.
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), category="policy_denied")
        _blocked(session, ago=timedelta(days=2), category="budget_exceeded")
        _failed(session, ago=timedelta(days=5), reason="agent error")
        session.commit()
        org = failure_trends(session, since=SINCE, now=NOW)
        rollup = failures_by_category(session, since=SINCE, now=NOW)
        cut = failures_by_category_trends(session, since=SINCE, now=NOW)

    assert cut["count"] == org["count"] == rollup["count"] == 3
    assert cut["blocked"] == org["blocked"] == rollup["blocked"]
    assert cut["failed"] == org["failed"] == rollup["failed"]
    assert cut["distinct_categories"] == rollup["distinct_categories"]
    # Per-category window totals match the point-in-time roll-up's counts.
    rollup_counts = {c["category"]: c["count"] for c in rollup["categories"]}
    assert {c["category"]: c["count"] for c in cut["categories"]} == rollup_counts
    # Every category's series sums to its window count.
    for cat in cut["categories"]:
        assert sum(cell["count"] for cell in cat["series"]) == cat["count"]


def test_bad_bucket_rejected(session_factory) -> None:
    with session_factory() as session:
        with pytest.raises(ValueError):
            failures_by_category_trends(session, since=SINCE, now=NOW, bucket="month")
