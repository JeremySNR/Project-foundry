"""delivery_by_repo_trends: per-repo finished-run outcomes bucketed over time.

The repo dimension of ``delivery_trends`` (the way ``delivery_by_repo`` is to
``delivery_metrics``): each repo carries a zero-filled series on one shared
time axis plus its window totals, so a sparkline shows whether a given repo is
shipping more or stalling.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRunOutcome
from foundry.memory.metrics import UNROUTED_REPO_LABEL, delivery_by_repo_trends
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
    repo: str | None,
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
            repo=repo,
            trigger_type="label",
            created_at_run=completed_at - timedelta(hours=1),
            completed_at=completed_at,
            jobs_count=jobs_count,
            cost_usd=cost_usd,
            recorded_at=completed_at,
        )
    )


def _by_name(result: dict) -> dict[str, dict]:
    return {r["repo"]: r for r in result["repos"]}


def test_empty_database_has_no_repos_or_periods(session_factory) -> None:
    with session_factory() as session:
        result = delivery_by_repo_trends(session, since=NOW - timedelta(days=90))
    assert result["bucket"] == "week"
    assert result["periods"] == []
    assert result["repos"] == []


def test_rejects_unknown_bucket(session_factory) -> None:
    with session_factory() as session:
        with pytest.raises(ValueError):
            delivery_by_repo_trends(
                session, since=NOW - timedelta(days=90), bucket="month"
            )


def test_per_repo_series_share_one_zero_filled_axis(session_factory) -> None:
    with session_factory() as session:
        # payments: 2 merged this week (one with a retry + cost), 1 blocked.
        _add_outcome(
            session, outcome="merged", repo="payments", completed_at=NOW, cost_usd=1.50
        )
        _add_outcome(
            session,
            outcome="merged",
            repo="payments",
            completed_at=NOW,
            jobs_count=3,
            cost_usd=2.00,
        )
        _add_outcome(session, outcome="blocked", repo="payments", completed_at=NOW)
        # legacy: one merge two weeks earlier, nothing since; no cost reported.
        _add_outcome(
            session,
            outcome="merged",
            repo="legacy",
            completed_at=NOW - timedelta(days=14),
        )
        session.commit()
        result = delivery_by_repo_trends(
            session, since=NOW - timedelta(days=90), bucket="week"
        )

    # Weeks start Monday. NOW is Wed 2026-06-10 -> week of Mon 2026-06-08; two
    # weeks earlier -> Mon 2026-05-25. The gap week is filled, and the axis spans
    # the full range across *both* repos.
    assert result["periods"] == [
        "2026-05-25T00:00:00+00:00",
        "2026-06-01T00:00:00+00:00",
        "2026-06-08T00:00:00+00:00",
    ]

    repos = _by_name(result)
    pay = repos["payments"]
    leg = repos["legacy"]
    # Every repo's series is aligned to the shared axis, column-for-column.
    assert [c["period_start"] for c in pay["series"]] == result["periods"]
    assert [c["period_start"] for c in leg["series"]] == result["periods"]

    # payments window totals.
    assert pay["runs_finished"] == 3
    assert pay["prs_shipped"] == 2
    assert pay["blocked"] == 1
    assert pay["merge_rate"] == round(2 / 3, 3)
    assert pay["retries_consumed"] == 2  # sum(max(jobs_count - 1, 0)) = 0 + 2 + 0
    assert pay["total_cost_usd"] == 3.50
    # payments' activity is all in the latest week; the earlier weeks zero-fill.
    pay_latest = pay["series"][-1]
    assert pay_latest["prs_shipped"] == 2
    assert pay_latest["blocked"] == 1
    assert pay_latest["total_cost_usd"] == 3.50
    assert pay["series"][0]["runs_finished"] == 0
    assert pay["series"][0]["total_cost_usd"] is None  # never a conjured $0

    # legacy shipped only in the first week; later weeks zero-fill.
    assert leg["prs_shipped"] == 1
    assert leg["total_cost_usd"] is None  # no row reported cost
    assert leg["series"][0]["prs_shipped"] == 1
    assert leg["series"][-1]["runs_finished"] == 0


def test_unrouted_runs_bucket_under_sentinel(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="merged", repo="repo-a", completed_at=NOW)
        _add_outcome(session, outcome="blocked", repo=None, completed_at=NOW)
        session.commit()
        result = delivery_by_repo_trends(
            session, since=NOW - timedelta(days=90), bucket="day"
        )

    repos = _by_name(result)
    assert UNROUTED_REPO_LABEL in repos
    unrouted = repos[UNROUTED_REPO_LABEL]
    assert unrouted["runs_finished"] == 1
    assert unrouted["blocked"] == 1
    assert unrouted["prs_shipped"] == 0


def test_sorted_most_shipping_then_active_then_name(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="merged", repo="zzz", completed_at=NOW)
        _add_outcome(session, outcome="merged", repo="zzz", completed_at=NOW)
        _add_outcome(session, outcome="merged", repo="aaa", completed_at=NOW)
        _add_outcome(session, outcome="merged", repo="aaa", completed_at=NOW)
        _add_outcome(session, outcome="blocked", repo="aaa", completed_at=NOW)
        _add_outcome(session, outcome="merged", repo="ggg", completed_at=NOW)
        _add_outcome(session, outcome="merged", repo="mmm", completed_at=NOW)
        session.commit()
        result = delivery_by_repo_trends(session, since=NOW - timedelta(days=90))

    order = [r["repo"] for r in result["repos"]]
    # aaa (2 shipped, 3 finished) before zzz (2 shipped, 2 finished); then the
    # 1-shipped repos by name: ggg before mmm.
    assert order == ["aaa", "zzz", "ggg", "mmm"]


def test_day_bucket_separates_consecutive_days(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="merged", repo="repo-a", completed_at=NOW)
        _add_outcome(
            session,
            outcome="blocked",
            repo="repo-a",
            completed_at=NOW - timedelta(days=1),
        )
        session.commit()
        result = delivery_by_repo_trends(
            session, since=NOW - timedelta(days=7), bucket="day"
        )

    assert result["bucket"] == "day"
    assert result["periods"] == [
        "2026-06-09T00:00:00+00:00",
        "2026-06-10T00:00:00+00:00",
    ]
    series = _by_name(result)["repo-a"]["series"]
    assert series[0]["blocked"] == 1
    assert series[1]["prs_shipped"] == 1


def test_window_excludes_older_runs(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="merged", repo="repo-a", completed_at=NOW)
        _add_outcome(
            session,
            outcome="merged",
            repo="repo-a",
            completed_at=NOW - timedelta(days=120),
        )
        session.commit()
        result = delivery_by_repo_trends(session, since=NOW - timedelta(days=90))

    repo_a = _by_name(result)["repo-a"]
    assert repo_a["runs_finished"] == 1  # the 120-day-old run is outside the window
