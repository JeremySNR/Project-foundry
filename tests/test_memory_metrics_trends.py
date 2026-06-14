"""delivery_trends: run outcomes bucketed over time for the fleet trend view."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRunOutcome
from foundry.memory.metrics import delivery_trends
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
            trigger_type="label",
            created_at_run=completed_at - timedelta(hours=1),
            completed_at=completed_at,
            jobs_count=jobs_count,
            cost_usd=cost_usd,
            recorded_at=completed_at,
        )
    )


def test_empty_database_has_no_periods(session_factory) -> None:
    with session_factory() as session:
        trends = delivery_trends(session, since=NOW - timedelta(days=90))
    assert trends["bucket"] == "week"
    assert trends["periods"] == []


def test_rejects_unknown_bucket(session_factory) -> None:
    with session_factory() as session:
        with pytest.raises(ValueError):
            delivery_trends(session, since=NOW - timedelta(days=90), bucket="month")


def test_buckets_outcomes_by_week_and_aggregates(session_factory) -> None:
    with session_factory() as session:
        # Two merges + one block this week; one merge two weeks ago.
        _add_outcome(session, outcome="merged", completed_at=NOW, cost_usd=1.50)
        _add_outcome(
            session, outcome="merged", completed_at=NOW, jobs_count=3, cost_usd=2.00
        )
        _add_outcome(session, outcome="blocked", completed_at=NOW)
        _add_outcome(
            session, outcome="merged", completed_at=NOW - timedelta(days=14), cost_usd=0.5
        )
        session.commit()
        trends = delivery_trends(
            session, since=NOW - timedelta(days=90), bucket="week"
        )

    periods = trends["periods"]
    # Weeks start Monday. NOW is Wed 2026-06-10 -> week of Mon 2026-06-08.
    # Two weeks earlier -> Mon 2026-05-25. Gap-fill yields the week between them.
    assert [p["period_start"] for p in periods] == [
        "2026-05-25T00:00:00+00:00",
        "2026-06-01T00:00:00+00:00",
        "2026-06-08T00:00:00+00:00",
    ]
    earliest, gap, latest = periods
    assert earliest["prs_shipped"] == 1
    assert earliest["total_cost_usd"] == 0.5

    # The empty week between is zero-filled so a sparkline reads continuously,
    # and cost stays None (never a conjured $0) when nothing reported cost.
    assert gap["runs_finished"] == 0
    assert gap["prs_shipped"] == 0
    assert gap["total_cost_usd"] is None

    assert latest["runs_finished"] == 3
    assert latest["prs_shipped"] == 2
    assert latest["blocked"] == 1
    # retries = sum(max(jobs_count - 1, 0)) = 0 + 2 + 0.
    assert latest["retries_consumed"] == 2
    assert latest["total_cost_usd"] == 3.50


def test_day_bucket_separates_consecutive_days(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="merged", completed_at=NOW)
        _add_outcome(session, outcome="blocked", completed_at=NOW - timedelta(days=1))
        session.commit()
        trends = delivery_trends(session, since=NOW - timedelta(days=7), bucket="day")

    assert trends["bucket"] == "day"
    periods = trends["periods"]
    assert [p["period_start"] for p in periods] == [
        "2026-06-09T00:00:00+00:00",
        "2026-06-10T00:00:00+00:00",
    ]
    assert periods[0]["blocked"] == 1
    assert periods[1]["prs_shipped"] == 1


def test_naive_completion_times_treated_as_utc(session_factory) -> None:
    # SQLite hands timestamps back naive; the bucketer must read them as UTC
    # rather than shifting by the process-local zone.
    naive = NOW.replace(tzinfo=None)
    with session_factory() as session:
        _add_outcome(session, outcome="merged", completed_at=naive)
        session.commit()
        trends = delivery_trends(session, since=NOW - timedelta(days=7), bucket="day")
    assert trends["periods"][-1]["period_start"] == "2026-06-10T00:00:00+00:00"
