"""time_to_merge_seconds on the delivery *trend* cuts: the over-time
"is shipping getting slower week over week?" view.

The point-in-time delivery cuts (``delivery_metrics`` / ``delivery_by_repo`` /
``delivery_by_work_type``) already carry a ``time_to_merge_seconds`` distribution
beside ``time_to_approval_seconds``, and the trend cuts (``delivery_trends`` /
``delivery_by_repo_trends`` / ``delivery_by_work_type_trends``) gained the
over-time *approval* latency in #143 - but not the symmetric *merge* latency.
This closes that asymmetry: every trend period now carries both distributions,
derived at read time from the ``time_to_merge_seconds`` already stored on every
finished-run outcome row (no new column / write path).

Defined only for merged runs (``time_to_merge_seconds`` set); a run that never
merged contributes to the period's run/ship counts but nothing to its merge
distribution - the same exclusion the point-in-time cut applies.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRunOutcome
from foundry.memory.metrics import (
    delivery_by_repo_trends,
    delivery_by_work_type_trends,
    delivery_trends,
)
from foundry.schemas.common import RunStatus

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
    repo: str | None = "svc",
    work_type: str | None = "feature",
    completed_at: datetime = NOW,
    merge_seconds: int | None = None,
):
    """Insert a run + its derived outcome row directly (FK-safe).

    ``merge_seconds`` sets ``time_to_merge_seconds`` so the merge latency is
    exactly N; ``None`` leaves it unset (a run that never merged).
    """
    global _counter
    _counter += 1
    rid = f"r-{_counter}"
    created_at_run = completed_at - timedelta(hours=2)
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
            work_type=work_type,
            trigger_type="label",
            created_at_run=created_at_run,
            completed_at=completed_at,
            time_to_merge_seconds=merge_seconds,
            jobs_count=1,
            recorded_at=completed_at,
        )
    )
    session.commit()


def test_trends_carry_per_period_merge_latency(session_factory) -> None:
    """delivery_trends buckets merge latency over time, the over-time complement
    of the point-in-time distribution: two merges this week, one merge two weeks
    ago, and a zero-filled gap week between them."""
    with session_factory() as session:
        _add_outcome(session, outcome="merged", completed_at=NOW, merge_seconds=3600)
        _add_outcome(session, outcome="merged", completed_at=NOW, merge_seconds=7200)
        _add_outcome(
            session,
            outcome="merged",
            completed_at=NOW - timedelta(days=14),
            merge_seconds=600,
        )
        report = delivery_trends(session, since=NOW - timedelta(days=90), bucket="week")

    periods = report["periods"]
    # Weeks start Monday. NOW is Wed 2026-06-10 -> week of Mon 2026-06-08;
    # two weeks earlier -> Mon 2026-05-25; the gap week is filled.
    assert [p["period_start"] for p in periods] == [
        "2026-05-25T00:00:00+00:00",
        "2026-06-01T00:00:00+00:00",
        "2026-06-08T00:00:00+00:00",
    ]
    earliest, gap, latest = periods
    assert earliest["time_to_merge_seconds"] == {"count": 1, "median": 600, "p90": 600}
    # The zero-filled gap week reports an empty distribution, never a conjured 0.
    assert gap["time_to_merge_seconds"] == {"count": 0, "median": None, "p90": None}
    # _percentile([3600, 7200], 0.5) -> index 0; 0.9 -> index 1.
    assert latest["time_to_merge_seconds"] == {"count": 2, "median": 3600, "p90": 7200}


def test_trend_period_excludes_unmerged_runs(session_factory) -> None:
    """A run that never merged contributes to the period's run/ship counts but
    not its merge-latency distribution - the same exclusion as the point-in-time
    cut, and the merge-side mirror of the approval-latency exclusion."""
    with session_factory() as session:
        _add_outcome(session, outcome="merged", completed_at=NOW, merge_seconds=1800)
        _add_outcome(session, outcome="blocked", completed_at=NOW, merge_seconds=None)
        report = delivery_trends(session, since=NOW - timedelta(days=7), bucket="week")

    latest = report["periods"][-1]
    assert latest["runs_finished"] == 2
    assert latest["time_to_merge_seconds"] == {"count": 1, "median": 1800, "p90": 1800}


def test_by_repo_trends_carry_per_period_merge_latency(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(
            session, outcome="merged", repo="payments", completed_at=NOW, merge_seconds=3600
        )
        _add_outcome(
            session, outcome="merged", repo="web", completed_at=NOW, merge_seconds=60
        )
        report = delivery_by_repo_trends(
            session, since=NOW - timedelta(days=7), bucket="week"
        )

    by_repo = {r["repo"]: r for r in report["repos"]}
    assert by_repo["payments"]["series"][-1]["time_to_merge_seconds"] == {
        "count": 1,
        "median": 3600,
        "p90": 3600,
    }
    assert by_repo["web"]["series"][-1]["time_to_merge_seconds"]["median"] == 60


def test_by_work_type_trends_carry_per_period_merge_latency(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(
            session, outcome="merged", work_type="bug", completed_at=NOW, merge_seconds=120
        )
        _add_outcome(
            session,
            outcome="merged",
            work_type="feature",
            completed_at=NOW,
            merge_seconds=900,
        )
        report = delivery_by_work_type_trends(
            session, since=NOW - timedelta(days=7), bucket="week"
        )

    by_type = {t["work_type"]: t for t in report["work_types"]}
    assert by_type["bug"]["series"][-1]["time_to_merge_seconds"] == {
        "count": 1,
        "median": 120,
        "p90": 120,
    }
    assert by_type["feature"]["series"][-1]["time_to_merge_seconds"]["median"] == 900
