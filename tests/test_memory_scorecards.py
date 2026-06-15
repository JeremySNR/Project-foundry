"""Agent scorecards: per-provider merge rate, retries and spend from outcomes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRunOutcome
from foundry.memory.scorecards import (
    agent_scorecard_trends,
    agent_scorecards,
    scorecard_rows,
)
from foundry.schemas.common import RunStatus

NOW = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


_counter = 0


def _add_outcome(
    session,
    *,
    provider: str | None,
    outcome: str,
    work_type: str | None = "feature",
    repo: str | None = "billing-service",
    jobs_count: int = 1,
    cost_usd: float | None = None,
    completed_at: datetime | None = None,
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
            repo=repo if provider else None,
            provider=provider,
            work_type=work_type,
            trigger_type="label",
            created_at_run=NOW - timedelta(days=1),
            completed_at=completed_at or NOW,
            jobs_count=jobs_count,
            cost_usd=cost_usd,
            recorded_at=NOW,
        )
    )


def test_empty_database_has_no_providers(session_factory) -> None:
    with session_factory() as s:
        report = agent_scorecards(s)
    assert report == {"min_samples": 3, "providers": []}


def test_scorecard_aggregates_merge_rate_retries_and_cost(session_factory) -> None:
    with session_factory() as s:
        # claude_code: 3 merged + 1 blocked (with a retry) = 4 runs, 5 jobs.
        for _ in range(3):
            _add_outcome(s, provider="claude_code", outcome="merged", cost_usd=2.0)
        _add_outcome(
            s, provider="claude_code", outcome="blocked", jobs_count=2, cost_usd=2.0
        )
        # cursor: 1 merged + 1 failed = 2 runs, no reported cost.
        _add_outcome(s, provider="cursor", outcome="merged", work_type="bug")
        _add_outcome(s, provider="cursor", outcome="failed", work_type="bug")
        # A rejected-at-intake run never dispatched: must not skew any provider.
        _add_outcome(s, provider=None, outcome="rejected")
        s.commit()

        report = agent_scorecards(s)

    providers = report["providers"]
    # Most-used provider first.
    assert [p["provider"] for p in providers] == ["claude_code", "cursor"]

    cc = providers[0]
    assert cc["runs"] == 4
    assert cc["merged"] == 3
    assert cc["success_rate"] == 0.75
    # Beta-smoothed: round(100 * (3+1)/(4+2)) = 67, not 75.
    assert cc["smoothed_success"] == 67
    # 5 jobs across 4 runs => 1 retry consumed.
    assert cc["retries_consumed"] == 1
    assert cc["avg_retries"] == 0.25
    assert cc["total_cost_usd"] == 8.0
    assert cc["avg_cost_usd"] == 2.0
    assert cc["runs_with_cost"] == 4
    assert cc["meets_min_samples"] is True

    cur = providers[1]
    assert cur["runs"] == 2
    assert cur["merged"] == 1
    # No provider reported cost.
    assert cur["total_cost_usd"] is None
    assert cur["avg_cost_usd"] is None
    assert cur["runs_with_cost"] == 0
    # 2 runs < the default 3-sample floor.
    assert cur["meets_min_samples"] is False


def test_breakdowns_by_work_type_and_repo(session_factory) -> None:
    with session_factory() as s:
        _add_outcome(s, provider="cursor", outcome="merged", work_type="feature", repo="web")
        _add_outcome(s, provider="cursor", outcome="blocked", work_type="feature", repo="web")
        _add_outcome(s, provider="cursor", outcome="merged", work_type="bug", repo="api")
        s.commit()
        report = agent_scorecards(s)

    cursor = report["providers"][0]
    wt = {row["work_type"]: row for row in cursor["by_work_type"]}
    assert wt["feature"]["runs"] == 2 and wt["feature"]["merged"] == 1
    assert wt["bug"]["runs"] == 1 and wt["bug"]["merged"] == 1

    repos = {row["repo"]: row for row in cursor["by_repo"]}
    assert repos["web"]["runs"] == 2
    assert repos["api"]["runs"] == 1


def test_scorecard_rows_skip_undispatched(session_factory) -> None:
    with session_factory() as s:
        _add_outcome(s, provider="claude_code", outcome="merged")
        _add_outcome(s, provider=None, outcome="needs_clarification")
        s.commit()
        rows = scorecard_rows(s)
    # Only the dispatched (provider IS NOT NULL) row aggregates.
    assert len(rows) == 1
    assert rows[0][0] == "claude_code"


def test_since_window_filters_old_runs(session_factory) -> None:
    with session_factory() as s:
        _add_outcome(s, provider="cursor", outcome="merged", completed_at=NOW)
        _add_outcome(
            s,
            provider="cursor",
            outcome="merged",
            completed_at=NOW - timedelta(days=200),
        )
        s.commit()
        recent = agent_scorecards(s, since=NOW - timedelta(days=90))
        all_time = agent_scorecards(s)

    assert recent["providers"][0]["runs"] == 1
    assert all_time["providers"][0]["runs"] == 2


def test_min_samples_is_reported_and_configurable(session_factory) -> None:
    with session_factory() as s:
        _add_outcome(s, provider="cursor", outcome="merged")
        _add_outcome(s, provider="cursor", outcome="merged")
        s.commit()
        report = agent_scorecards(s, min_samples=2)
    assert report["min_samples"] == 2
    assert report["providers"][0]["meets_min_samples"] is True


# -- agent_scorecard_trends: the same scorecards bucketed over time ------------

# 2026-06-10 is a Wednesday, so Monday-anchored week buckets are unambiguous:
# the weeks of 06-01, 06-08, 06-15 each start on a Monday.
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_trends_empty_database(session_factory) -> None:
    with session_factory() as s:
        report = agent_scorecard_trends(s, since=EPOCH)
    assert report["bucket"] == "week"
    assert report["min_samples"] == 3
    assert report["providers"] == []
    assert report["periods"] == []


def test_trends_skip_undispatched(session_factory) -> None:
    with session_factory() as s:
        _add_outcome(
            s,
            provider=None,
            outcome="rejected",
            completed_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        s.commit()
        report = agent_scorecard_trends(s, since=EPOCH)
    # A run that never dispatched says nothing about any agent.
    assert report["providers"] == []
    assert report["periods"] == []


def test_trends_bucket_by_week_with_zero_filled_gaps(session_factory) -> None:
    wk1 = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)  # week of 06-01
    wk3 = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)  # week of 06-15
    with session_factory() as s:
        _add_outcome(s, provider="cursor", outcome="merged", completed_at=wk1)
        _add_outcome(s, provider="cursor", outcome="blocked", completed_at=wk1)
        _add_outcome(s, provider="cursor", outcome="merged", completed_at=wk3)
        s.commit()
        report = agent_scorecard_trends(s, since=EPOCH, bucket="week")

    # The shared axis spans first..last populated week, gap-filled.
    assert report["periods"] == [
        "2026-06-01T00:00:00+00:00",
        "2026-06-08T00:00:00+00:00",
        "2026-06-15T00:00:00+00:00",
    ]
    cursor = report["providers"][0]
    assert cursor["provider"] == "cursor"
    # Window totals match the all-time scorecard shape.
    assert cursor["runs"] == 3 and cursor["merged"] == 2
    series = cursor["series"]
    assert [c["period_start"] for c in series] == report["periods"]
    # Week 1: 1 of 2 merged.
    assert series[0]["runs"] == 2 and series[0]["merged"] == 1
    assert series[0]["success_rate"] == 0.5
    # The empty middle week is zero-filled, never a conjured rate or $0.
    assert series[1]["runs"] == 0
    assert series[1]["merged"] == 0
    assert series[1]["success_rate"] is None
    assert series[1]["smoothed_success"] is None
    assert series[1]["total_cost_usd"] is None
    # Week 3: 1 of 1 merged.
    assert series[2]["runs"] == 1 and series[2]["merged"] == 1


def test_trend_period_aggregates_cost_and_retries(session_factory) -> None:
    wk = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    with session_factory() as s:
        _add_outcome(
            s, provider="claude_code", outcome="merged", jobs_count=2, cost_usd=3.0,
            completed_at=wk,
        )
        _add_outcome(
            s, provider="claude_code", outcome="merged", cost_usd=1.0, completed_at=wk
        )
        s.commit()
        report = agent_scorecard_trends(s, since=EPOCH, bucket="week")

    cell = report["providers"][0]["series"][0]
    assert cell["runs"] == 2 and cell["merged"] == 2
    assert cell["retries_consumed"] == 1  # 3 jobs across 2 runs
    assert cell["total_cost_usd"] == 4.0
    # Beta-smoothed: round(100 * (2+1)/(2+2)) = 75.
    assert cell["smoothed_success"] == 75


def test_trends_providers_share_one_aligned_axis(session_factory) -> None:
    early = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)  # week of 06-01
    late = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)  # week of 06-15
    with session_factory() as s:
        _add_outcome(s, provider="cursor", outcome="merged", completed_at=early)
        _add_outcome(s, provider="claude_code", outcome="merged", completed_at=late)
        s.commit()
        report = agent_scorecard_trends(s, since=EPOCH, bucket="week")

    assert len(report["periods"]) == 3
    # Every provider's series is aligned to the same axis, even though each ran
    # in only one of the three weeks - so the sparklines line up.
    for provider in report["providers"]:
        assert [c["period_start"] for c in provider["series"]] == report["periods"]


def test_trends_day_bucket_and_bad_bucket_rejected(session_factory) -> None:
    d1 = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    d3 = datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)
    with session_factory() as s:
        _add_outcome(s, provider="cursor", outcome="merged", completed_at=d1)
        _add_outcome(s, provider="cursor", outcome="failed", completed_at=d3)
        s.commit()
        report = agent_scorecard_trends(s, since=EPOCH, bucket="day")
        with pytest.raises(ValueError):
            agent_scorecard_trends(s, since=EPOCH, bucket="month")

    assert report["periods"] == [
        "2026-06-10T00:00:00+00:00",
        "2026-06-11T00:00:00+00:00",
        "2026-06-12T00:00:00+00:00",
    ]
    series = report["providers"][0]["series"]
    assert series[0]["merged"] == 1
    assert series[1]["runs"] == 0  # gap day
    assert series[2]["runs"] == 1 and series[2]["merged"] == 0


def test_trends_since_window_filters_old_periods(session_factory) -> None:
    recent = datetime(2026, 6, 10, tzinfo=timezone.utc)
    old = datetime(2026, 1, 10, tzinfo=timezone.utc)
    with session_factory() as s:
        _add_outcome(s, provider="cursor", outcome="merged", completed_at=recent)
        _add_outcome(s, provider="cursor", outcome="merged", completed_at=old)
        s.commit()
        report = agent_scorecard_trends(
            s, since=datetime(2026, 6, 1, tzinfo=timezone.utc), bucket="week"
        )
    # Only the in-window run counts; the axis doesn't stretch back to January.
    assert report["providers"][0]["runs"] == 1
    assert report["periods"] == ["2026-06-08T00:00:00+00:00"]


def test_trends_provider_order_most_runs_first(session_factory) -> None:
    wk = datetime(2026, 6, 10, tzinfo=timezone.utc)
    with session_factory() as s:
        for _ in range(3):
            _add_outcome(s, provider="claude_code", outcome="merged", completed_at=wk)
        _add_outcome(s, provider="cursor", outcome="merged", completed_at=wk)
        s.commit()
        report = agent_scorecard_trends(s, since=EPOCH, bucket="week")
    assert [p["provider"] for p in report["providers"]] == ["claude_code", "cursor"]
    assert report["providers"][0]["meets_min_samples"] is True
    assert report["providers"][1]["meets_min_samples"] is False
