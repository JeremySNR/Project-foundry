"""Agent scorecards: per-provider merge rate, retries and spend from outcomes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRunOutcome
from foundry.memory.scorecards import agent_scorecards, scorecard_rows
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
