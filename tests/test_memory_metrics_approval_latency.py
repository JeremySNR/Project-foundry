"""time_to_approval_seconds: the approval-latency distribution on the delivery
metrics.

In a system whose whole product is the human approval gate, "once work is
ready, how long does a human take to sign it off?" is the governance-bottleneck
signal. The live approval queue (``GET /metrics/approvals``) answers it for runs
parked *right now*; this is the historical complement, computed from the
``approved_at`` / ``created_at_run`` timestamps already stored on every finished
run's outcome row - so it needs no new column or write path, mirroring how
``time_to_merge_seconds`` is derived from the same rows.

Defined only for runs that reached approval (``approved_at`` set); a run blocked
or rejected before any human signed off contributes nothing, the way an unmerged
run contributes nothing to ``time_to_merge_seconds``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRunOutcome
from foundry.memory.metrics import (
    UNCLASSIFIED_WORK_TYPE_LABEL,
    UNROUTED_REPO_LABEL,
    delivery_by_repo,
    delivery_by_work_type,
    delivery_metrics,
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
    approval_seconds: int | None = None,
):
    """Insert a run + its derived outcome row directly (FK-safe).

    ``approval_seconds`` sets ``approved_at = created_at_run + N`` so the
    approval latency is exactly N; ``None`` leaves ``approved_at`` unset (a run
    that never reached approval).
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
            approved_at=(
                created_at_run + timedelta(seconds=approval_seconds)
                if approval_seconds is not None
                else None
            ),
            completed_at=completed_at,
            jobs_count=1,
            recorded_at=completed_at,
        )
    )
    session.commit()


def test_no_approvals_yields_empty_distribution(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="blocked", approval_seconds=None)
        report = delivery_metrics(session, since=NOW - timedelta(days=1))

    tta = report["time_to_approval_seconds"]
    assert tta == {"count": 0, "median": None, "p90": None}


def test_distribution_over_approved_runs(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="merged", approval_seconds=3600)
        _add_outcome(session, outcome="merged", approval_seconds=7200)
        report = delivery_metrics(session, since=NOW - timedelta(days=1))

    tta = report["time_to_approval_seconds"]
    assert tta["count"] == 2
    # _percentile([3600, 7200], 0.5) -> index 0; 0.9 -> index 1.
    assert tta["median"] == 3600
    assert tta["p90"] == 7200


def test_excludes_unapproved_but_includes_approved_then_blocked(session_factory) -> None:
    """A run blocked *after* a human approved it still has an approval latency;
    one blocked at intake (never approved) does not."""
    with session_factory() as session:
        _add_outcome(session, outcome="merged", approval_seconds=1800)
        _add_outcome(session, outcome="blocked", approval_seconds=600)  # approved, then blocked
        _add_outcome(session, outcome="blocked", approval_seconds=None)  # never approved
        report = delivery_metrics(session, since=NOW - timedelta(days=1))

    assert report["time_to_approval_seconds"]["count"] == 2


def test_negative_clock_skew_floored_at_zero(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="merged", approval_seconds=-100)
        report = delivery_metrics(session, since=NOW - timedelta(days=1))

    assert report["time_to_approval_seconds"] == {"count": 1, "median": 0, "p90": 0}


def test_by_repo_carries_per_repo_approval_latency(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="merged", repo="payments", approval_seconds=3600)
        _add_outcome(session, outcome="merged", repo="payments", approval_seconds=1800)
        _add_outcome(session, outcome="merged", repo="web", approval_seconds=60)
        _add_outcome(session, outcome="blocked", repo=None, approval_seconds=None)
        report = delivery_by_repo(session, since=NOW - timedelta(days=1))

    by_repo = {r["repo"]: r["time_to_approval_seconds"] for r in report["repos"]}
    assert by_repo["payments"]["count"] == 2
    assert by_repo["payments"]["median"] == 1800
    assert by_repo["web"] == {"count": 1, "median": 60, "p90": 60}
    # The unrouted, never-approved block has no approval latency.
    assert by_repo[UNROUTED_REPO_LABEL] == {"count": 0, "median": None, "p90": None}


def test_by_work_type_carries_per_type_approval_latency(session_factory) -> None:
    with session_factory() as session:
        _add_outcome(session, outcome="merged", work_type="bug", approval_seconds=120)
        _add_outcome(session, outcome="merged", work_type="bug", approval_seconds=240)
        _add_outcome(session, outcome="merged", work_type="feature", approval_seconds=900)
        _add_outcome(session, outcome="blocked", work_type=None, approval_seconds=None)
        report = delivery_by_work_type(session, since=NOW - timedelta(days=1))

    by_type = {t["work_type"]: t["time_to_approval_seconds"] for t in report["work_types"]}
    assert by_type["bug"]["count"] == 2
    assert by_type["bug"]["median"] == 120
    assert by_type["feature"] == {"count": 1, "median": 900, "p90": 900}
    assert by_type[UNCLASSIFIED_WORK_TYPE_LABEL]["count"] == 0
