"""delivery_by_work_type: finished-run outcomes grouped by work type."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRunOutcome
from foundry.memory.metrics import (
    UNCLASSIFIED_WORK_TYPE_LABEL,
    delivery_by_work_type,
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
    work_type: str | None,
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
            work_type=work_type,
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
    return {r["work_type"]: r for r in result["work_types"]}


def test_empty_database_has_no_work_types(session_factory) -> None:
    with session_factory() as session:
        result = delivery_by_work_type(session, since=NOW - timedelta(days=90))
    assert result["runs_finished"] == 0
    assert result["work_types"] == []


def test_groups_outcomes_and_cost_by_work_type(session_factory) -> None:
    with session_factory() as session:
        # feature: 2 merged (one with a retry + cost), 1 blocked.
        _add_outcome(
            session,
            outcome="merged",
            work_type="feature",
            cost_usd=1.50,
            time_to_merge_seconds=100,
        )
        _add_outcome(
            session,
            outcome="merged",
            work_type="feature",
            jobs_count=3,
            cost_usd=2.00,
            time_to_merge_seconds=300,
        )
        _add_outcome(session, outcome="blocked", work_type="feature")
        # bug: 1 merged, 1 failed; no cost ever reported.
        _add_outcome(session, outcome="merged", work_type="bug")
        _add_outcome(session, outcome="failed", work_type="bug")
        session.commit()
        result = delivery_by_work_type(session, since=NOW - timedelta(days=90))

    assert result["runs_finished"] == 5
    types = _by_name(result)

    feat = types["feature"]
    assert feat["runs_finished"] == 3
    assert feat["prs_shipped"] == 2
    assert feat["blocked"] == 1
    assert feat["merge_rate"] == round(2 / 3, 3)
    # retries = sum(max(jobs_count - 1, 0)) = 0 + 2 + 0.
    assert feat["retries_consumed"] == 2
    assert feat["total_cost_usd"] == 3.50
    assert feat["time_to_merge_seconds"]["count"] == 2
    assert feat["time_to_merge_seconds"]["median"] == 100

    bug = types["bug"]
    assert bug["runs_finished"] == 2
    assert bug["prs_shipped"] == 1
    assert bug["failed"] == 1
    assert bug["merge_rate"] == 0.5
    # No row for this type reported cost -> None, never a conjured $0.
    assert bug["total_cost_usd"] is None
    assert bug["time_to_merge_seconds"]["count"] == 0
    assert bug["time_to_merge_seconds"]["median"] is None


def test_unclassified_runs_bucket_under_sentinel(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="merged", work_type="feature")
        # Never classified (NULL work_type): a rejected-at-intake park and a block.
        _add_outcome(session, outcome="blocked", work_type=None)
        _add_outcome(session, outcome="rejected", work_type=None)
        session.commit()
        result = delivery_by_work_type(session, since=NOW - timedelta(days=90))

    types = _by_name(result)
    assert UNCLASSIFIED_WORK_TYPE_LABEL in types
    unclassified = types[UNCLASSIFIED_WORK_TYPE_LABEL]
    assert unclassified["runs_finished"] == 2
    assert unclassified["blocked"] == 1
    assert unclassified["rejected"] == 1
    assert unclassified["prs_shipped"] == 0


def test_sorted_most_shipping_then_active_then_name(session_factory) -> None:
    with session_factory() as session:
        # tech_debt: 2 shipped. bug: 2 shipped but more finished. incident: 1.
        _add_outcome(session, outcome="merged", work_type="tech_debt")
        _add_outcome(session, outcome="merged", work_type="tech_debt")
        _add_outcome(session, outcome="merged", work_type="bug")
        _add_outcome(session, outcome="merged", work_type="bug")
        _add_outcome(session, outcome="blocked", work_type="bug")
        _add_outcome(session, outcome="merged", work_type="incident")
        # Two types tied on (shipped=1, finished=1): name tie-break is ascending.
        _add_outcome(session, outcome="merged", work_type="feature")
        session.commit()
        result = delivery_by_work_type(session, since=NOW - timedelta(days=90))

    order = [r["work_type"] for r in result["work_types"]]
    # bug (2 shipped, 3 finished) before tech_debt (2 shipped, 2 finished); then
    # the 1-shipped types by name: feature before incident.
    assert order == ["bug", "tech_debt", "feature", "incident"]


def test_window_excludes_older_runs(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="merged", work_type="bug", completed_at=NOW)
        _add_outcome(
            session,
            outcome="merged",
            work_type="bug",
            completed_at=NOW - timedelta(days=120),
        )
        session.commit()
        result = delivery_by_work_type(session, since=NOW - timedelta(days=90))

    assert result["runs_finished"] == 1
    assert _by_name(result)["bug"]["runs_finished"] == 1
