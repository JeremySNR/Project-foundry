"""delivery_by_work_type_trends: per-work-type finished-run outcomes bucketed
over time.

The work-type dimension of ``delivery_trends`` (the way ``delivery_by_work_type``
is to ``delivery_metrics``, and ``delivery_by_repo_trends`` is to
``delivery_by_repo``): each work type carries a zero-filled series on one shared
time axis plus its window totals, so a sparkline shows whether a given kind of
work is shipping more reliably or stalling.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRunOutcome
from foundry.memory.metrics import (
    UNCLASSIFIED_WORK_TYPE_LABEL,
    delivery_by_work_type_trends,
)
from foundry.schemas.common import RunStatus

# A Wednesday, so day/week boundaries are unambiguous in assertions.
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


_counter = 0


def _add_outcome(
    session,
    *,
    outcome: str,
    work_type: str | None,
    completed_at: datetime,
    jobs_count: int = 1,
    cost_usd: float | None = None,
):
    """Insert a run + its derived outcome row directly (FK-safe)."""
    global _counter
    _counter += 1
    rid = f"r-{_counter}"
    session.add(
        FoundryRun(
            id=rid,
            linear_issue_id=f"i-{_counter}",
            linear_issue_key=f"ENG-{_counter}",
            status=RunStatus.COMPLETE,
            trigger_type="label",
        )
    )
    session.add(
        FoundryRunOutcome(
            run_id=rid,
            linear_issue_id=f"i-{_counter}",
            issue_key_prefix="ENG",
            outcome=outcome,
            work_type=work_type,
            trigger_type="label",
            created_at_run=completed_at - timedelta(hours=1),
            completed_at=completed_at,
            jobs_count=jobs_count,
            cost_usd=cost_usd,
            recorded_at=completed_at,
        )
    )


def _by_name(result: dict) -> dict[str, dict]:
    return {r["work_type"]: r for r in result["work_types"]}


def test_empty_database_has_no_types_or_periods(session_factory) -> None:
    with session_factory() as session:
        result = delivery_by_work_type_trends(session, since=NOW - timedelta(days=90))
    assert result["bucket"] == "week"
    assert result["periods"] == []
    assert result["work_types"] == []


def test_rejects_unknown_bucket(session_factory) -> None:
    with session_factory() as session:
        with pytest.raises(ValueError):
            delivery_by_work_type_trends(
                session, since=NOW - timedelta(days=90), bucket="month"
            )


def test_per_type_series_share_one_zero_filled_axis(session_factory) -> None:
    with session_factory() as session:
        # feature: 2 merged this week (one with a retry + cost), 1 blocked.
        _add_outcome(
            session,
            outcome="merged",
            work_type="feature",
            completed_at=NOW,
            cost_usd=1.50,
        )
        _add_outcome(
            session,
            outcome="merged",
            work_type="feature",
            completed_at=NOW,
            jobs_count=3,
            cost_usd=2.00,
        )
        _add_outcome(
            session, outcome="blocked", work_type="feature", completed_at=NOW
        )
        # bug: one merge two weeks earlier, nothing since; no cost reported.
        _add_outcome(
            session,
            outcome="merged",
            work_type="bug",
            completed_at=NOW - timedelta(days=14),
        )
        session.commit()
        result = delivery_by_work_type_trends(
            session, since=NOW - timedelta(days=90), bucket="week"
        )

    # Weeks start Monday. NOW is Wed 2026-06-10 -> week of Mon 2026-06-08; two
    # weeks earlier -> Mon 2026-05-25. The gap week is filled, and the axis spans
    # the full range across *both* types.
    assert result["periods"] == [
        "2026-05-25T00:00:00+00:00",
        "2026-06-01T00:00:00+00:00",
        "2026-06-08T00:00:00+00:00",
    ]

    types = _by_name(result)
    feat = types["feature"]
    bug = types["bug"]
    # Every type's series is aligned to the shared axis, column-for-column.
    assert [c["period_start"] for c in feat["series"]] == result["periods"]
    assert [c["period_start"] for c in bug["series"]] == result["periods"]

    # feature window totals.
    assert feat["runs_finished"] == 3
    assert feat["prs_shipped"] == 2
    assert feat["blocked"] == 1
    assert feat["merge_rate"] == round(2 / 3, 3)
    assert feat["retries_consumed"] == 2  # sum(max(jobs_count - 1, 0)) = 0 + 2 + 0
    assert feat["total_cost_usd"] == 3.50
    # feature activity is all in the latest week; the earlier weeks zero-fill.
    feat_latest = feat["series"][-1]
    assert feat_latest["prs_shipped"] == 2
    assert feat_latest["blocked"] == 1
    assert feat_latest["total_cost_usd"] == 3.50
    assert feat["series"][0]["runs_finished"] == 0
    assert feat["series"][0]["total_cost_usd"] is None  # never a conjured $0

    # bug shipped only in the first week; later weeks zero-fill.
    assert bug["prs_shipped"] == 1
    assert bug["total_cost_usd"] is None  # no row reported cost
    assert bug["series"][0]["prs_shipped"] == 1
    assert bug["series"][-1]["runs_finished"] == 0


def test_unclassified_runs_bucket_under_sentinel(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(
            session, outcome="merged", work_type="feature", completed_at=NOW
        )
        _add_outcome(session, outcome="blocked", work_type=None, completed_at=NOW)
        session.commit()
        result = delivery_by_work_type_trends(
            session, since=NOW - timedelta(days=90), bucket="day"
        )

    types = _by_name(result)
    assert UNCLASSIFIED_WORK_TYPE_LABEL in types
    unclassified = types[UNCLASSIFIED_WORK_TYPE_LABEL]
    assert unclassified["runs_finished"] == 1
    assert unclassified["blocked"] == 1
    assert unclassified["prs_shipped"] == 0


def test_sorted_most_shipping_then_active_then_name(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="merged", work_type="zzz", completed_at=NOW)
        _add_outcome(session, outcome="merged", work_type="zzz", completed_at=NOW)
        _add_outcome(session, outcome="merged", work_type="aaa", completed_at=NOW)
        _add_outcome(session, outcome="merged", work_type="aaa", completed_at=NOW)
        _add_outcome(session, outcome="blocked", work_type="aaa", completed_at=NOW)
        _add_outcome(session, outcome="merged", work_type="ggg", completed_at=NOW)
        _add_outcome(session, outcome="merged", work_type="mmm", completed_at=NOW)
        session.commit()
        result = delivery_by_work_type_trends(session, since=NOW - timedelta(days=90))

    order = [r["work_type"] for r in result["work_types"]]
    # aaa (2 shipped, 3 finished) before zzz (2 shipped, 2 finished); then the
    # 1-shipped types by name: ggg before mmm.
    assert order == ["aaa", "zzz", "ggg", "mmm"]


def test_day_bucket_separates_consecutive_days(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(
            session, outcome="merged", work_type="feature", completed_at=NOW
        )
        _add_outcome(
            session,
            outcome="blocked",
            work_type="feature",
            completed_at=NOW - timedelta(days=1),
        )
        session.commit()
        result = delivery_by_work_type_trends(
            session, since=NOW - timedelta(days=7), bucket="day"
        )

    assert result["bucket"] == "day"
    assert result["periods"] == [
        "2026-06-09T00:00:00+00:00",
        "2026-06-10T00:00:00+00:00",
    ]
    series = _by_name(result)["feature"]["series"]
    assert series[0]["blocked"] == 1
    assert series[1]["prs_shipped"] == 1


def test_window_excludes_older_runs(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(
            session, outcome="merged", work_type="feature", completed_at=NOW
        )
        _add_outcome(
            session,
            outcome="merged",
            work_type="feature",
            completed_at=NOW - timedelta(days=120),
        )
        session.commit()
        result = delivery_by_work_type_trends(session, since=NOW - timedelta(days=90))

    feature = _by_name(result)["feature"]
    assert feature["runs_finished"] == 1  # the 120-day-old run is outside the window
