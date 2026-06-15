"""delivery_by_repo: finished-run outcomes grouped by routed repo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRunOutcome
from foundry.memory.metrics import UNROUTED_REPO_LABEL, delivery_by_repo
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
    repo: str | None,
    completed_at: datetime = NOW,
    jobs_count: int = 1,
    cost_usd: float | None = None,
    time_to_merge_seconds: int | None = None,
    escalations_count: int = 0,
    ci_failures_count: int = 0,
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
            time_to_merge_seconds=time_to_merge_seconds,
            escalations_count=escalations_count,
            ci_failures_count=ci_failures_count,
            recorded_at=completed_at,
        )
    )


def _by_name(result: dict) -> dict[str, dict]:
    return {r["repo"]: r for r in result["repos"]}


def test_empty_database_has_no_repos(session_factory) -> None:
    with session_factory() as session:
        result = delivery_by_repo(session, since=NOW - timedelta(days=90))
    assert result["runs_finished"] == 0
    assert result["repos"] == []


def test_groups_outcomes_and_cost_by_repo(session_factory) -> None:
    with session_factory() as session:
        # payments-service: 2 merged (one with a retry + cost), 1 blocked.
        _add_outcome(
            session,
            outcome="merged",
            repo="payments-service",
            cost_usd=1.50,
            time_to_merge_seconds=100,
        )
        _add_outcome(
            session,
            outcome="merged",
            repo="payments-service",
            jobs_count=3,
            cost_usd=2.00,
            time_to_merge_seconds=300,
        )
        _add_outcome(session, outcome="blocked", repo="payments-service")
        # legacy-monolith: 1 merged, 1 failed; no cost ever reported.
        _add_outcome(session, outcome="merged", repo="legacy-monolith")
        _add_outcome(session, outcome="failed", repo="legacy-monolith")
        session.commit()
        result = delivery_by_repo(session, since=NOW - timedelta(days=90))

    assert result["runs_finished"] == 5
    repos = _by_name(result)

    pay = repos["payments-service"]
    assert pay["runs_finished"] == 3
    assert pay["prs_shipped"] == 2
    assert pay["blocked"] == 1
    assert pay["merge_rate"] == round(2 / 3, 3)
    # retries = sum(max(jobs_count - 1, 0)) = 0 + 2 + 0.
    assert pay["retries_consumed"] == 2
    assert pay["total_cost_usd"] == 3.50
    assert pay["time_to_merge_seconds"]["count"] == 2
    assert pay["time_to_merge_seconds"]["median"] == 100

    leg = repos["legacy-monolith"]
    assert leg["runs_finished"] == 2
    assert leg["prs_shipped"] == 1
    assert leg["failed"] == 1
    assert leg["merge_rate"] == 0.5
    # No row for this repo reported cost -> None, never a conjured $0.
    assert leg["total_cost_usd"] is None
    assert leg["time_to_merge_seconds"]["count"] == 0
    assert leg["time_to_merge_seconds"]["median"] is None


def test_unrouted_runs_bucket_under_sentinel(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="merged", repo="repo-a")
        # Never routed (NULL repo): an unroutable block and a clarification park.
        _add_outcome(session, outcome="blocked", repo=None)
        _add_outcome(session, outcome="needs_clarification", repo=None)
        session.commit()
        result = delivery_by_repo(session, since=NOW - timedelta(days=90))

    repos = _by_name(result)
    assert UNROUTED_REPO_LABEL in repos
    unrouted = repos[UNROUTED_REPO_LABEL]
    assert unrouted["runs_finished"] == 2
    assert unrouted["blocked"] == 1
    assert unrouted["needs_clarification"] == 1
    assert unrouted["prs_shipped"] == 0


def test_sorted_most_shipping_then_active_then_name(session_factory) -> None:
    with session_factory() as session:
        # zzz: 2 shipped. aaa: 2 shipped but more finished. mmm: 1 shipped.
        _add_outcome(session, outcome="merged", repo="zzz")
        _add_outcome(session, outcome="merged", repo="zzz")
        _add_outcome(session, outcome="merged", repo="aaa")
        _add_outcome(session, outcome="merged", repo="aaa")
        _add_outcome(session, outcome="blocked", repo="aaa")
        _add_outcome(session, outcome="merged", repo="mmm")
        # Two repos tied on (shipped=1, finished=1): name tie-break is ascending.
        _add_outcome(session, outcome="merged", repo="ggg")
        session.commit()
        result = delivery_by_repo(session, since=NOW - timedelta(days=90))

    order = [r["repo"] for r in result["repos"]]
    # aaa (2 shipped, 3 finished) before zzz (2 shipped, 2 finished); then the
    # 1-shipped repos by name: ggg before mmm.
    assert order == ["aaa", "zzz", "ggg", "mmm"]


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
        result = delivery_by_repo(session, since=NOW - timedelta(days=90))

    assert result["runs_finished"] == 1
    assert _by_name(result)["repo-a"]["runs_finished"] == 1
