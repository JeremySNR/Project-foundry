"""failures_by_repo: the repo-axis triage cut for the fleet dashboard.

The repo-grouped complement to ``failures_by_category`` (issue #37): where that
roll-up groups recently-failed runs by *reason*, this groups the same runs by
their *routed repo* - the on-call's "is one repo the systemic blocker?" question,
the failure-side mirror of ``delivery_by_repo``. Counts per repo (with a
blocked/failed split and the newest/oldest age span), most-frequent first.

Reuses the same ``_failure_event_map`` / ``_FAILURE_EVENTS_BY_STATUS`` derivation
the feed and the by-category roll-up use, so the totals here can never disagree
with theirs. Runs that never routed bucket under the ``(unrouted)`` sentinel,
exactly as in the delivery cut.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import AuditEventType, FoundryAgentJob, FoundryAuditEvent
from foundry.memory.metrics import (
    UNROUTED_REPO_LABEL,
    failure_queue,
    failures_by_repo,
)
from foundry.schemas.common import OverallRisk, RunStatus

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
# A generous default window for tests that don't care about the boundary.
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
    repo: str | None = None,
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
    # FoundryRun has no repo column; the repo lives on the agent job ("where the
    # work landed"). Attach a dispatched job so the run is routed - a run with no
    # job is correctly counted as unrouted, matching record_outcome's derivation.
    if repo is not None:
        _counter += 1
        session.add(
            FoundryAgentJob(
                id=f"j-{_counter}",
                run_id=rid,
                provider="fake",
                repo=repo,
                started_at=created_at,
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


def _blocked(session, *, ago: timedelta, repo: str | None, reason: str = "policy_denied") -> str:
    rid = _add_run(session, status=RunStatus.BLOCKED, created_at=NOW - ago, repo=repo)
    _add_event(
        session,
        rid,
        AuditEventType.RUN_BLOCKED,
        NOW - ago,
        metadata_json=f'{{"category": "{reason}"}}',
    )
    return rid


def _failed(session, *, ago: timedelta, repo: str | None, reason: str = "agent error") -> str:
    rid = _add_run(
        session, status=RunStatus.EXECUTION_FAILED, created_at=NOW - ago, repo=repo
    )
    _add_event(
        session,
        rid,
        AuditEventType.AGENT_FAILED,
        NOW - ago,
        metadata_json=f'{{"reason": "{reason}"}}',
    )
    return rid


def test_empty(session_factory) -> None:
    with session_factory() as session:
        report = failures_by_repo(session, since=SINCE, now=NOW)
    assert report["count"] == 0
    assert report["blocked"] == 0
    assert report["failed"] == 0
    assert report["distinct_repos"] == 0
    assert report["repos"] == []


def test_groups_by_repo_most_frequent_first(session_factory) -> None:
    with session_factory() as session:
        # 3 failures in org/api, 1 in org/web.
        _blocked(session, ago=timedelta(hours=1), repo="org/api")
        _blocked(session, ago=timedelta(hours=2), repo="org/api")
        _failed(session, ago=timedelta(hours=5), repo="org/api")
        _blocked(session, ago=timedelta(hours=3), repo="org/web")
        session.commit()
        report = failures_by_repo(session, since=SINCE, now=NOW)

    assert report["count"] == 4
    assert report["blocked"] == 3
    assert report["failed"] == 1
    assert report["distinct_repos"] == 2
    repos = report["repos"]
    assert [r["repo"] for r in repos] == ["org/api", "org/web"]

    top = repos[0]
    assert top["count"] == 3
    assert top["blocked"] == 2
    assert top["failed"] == 1
    # newest of the three is the 1h-ago one, oldest is the 5h-ago one.
    assert top["newest_failure_seconds"] == 1 * 3600
    assert top["oldest_failure_seconds"] == 5 * 3600
    assert top["last_failure"] == (NOW - timedelta(hours=1)).isoformat()


def test_unrouted_runs_bucketed_under_sentinel(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(minutes=5), repo=None)
        _failed(session, ago=timedelta(minutes=6), repo=None)
        session.commit()
        report = failures_by_repo(session, since=SINCE, now=NOW)

    assert report["distinct_repos"] == 1
    repo = report["repos"][0]
    assert repo["repo"] == UNROUTED_REPO_LABEL
    assert repo["count"] == 2
    assert repo["blocked"] == 1
    assert repo["failed"] == 1


def test_blocked_and_failed_split_within_a_repo(session_factory) -> None:
    # The repo key comes from FoundryRun.repo, not the run status, so a blocked
    # run and an execution-failed run can share a repo.
    with session_factory() as session:
        _blocked(session, ago=timedelta(minutes=10), repo="org/api")
        _failed(session, ago=timedelta(minutes=20), repo="org/api")
        session.commit()
        report = failures_by_repo(session, since=SINCE, now=NOW)

    assert report["distinct_repos"] == 1
    repo = report["repos"][0]
    assert repo["repo"] == "org/api"
    assert repo["count"] == 2
    assert repo["blocked"] == 1
    assert repo["failed"] == 1


def test_window_excludes_old_failures(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), repo="org/api")
        _blocked(session, ago=timedelta(days=40), repo="org/api")  # too old
        session.commit()
        report = failures_by_repo(session, since=NOW - timedelta(days=7), now=NOW)

    assert report["count"] == 1
    assert report["repos"][0]["count"] == 1


def test_only_failure_states_counted(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), repo="org/api")
        # An active run with a (stale) failure-marker event must not be counted.
        live = _add_run(
            session,
            status=RunStatus.AGENT_RUNNING,
            created_at=NOW - timedelta(hours=1),
            repo="org/web",
        )
        _add_event(
            session,
            live,
            AuditEventType.AGENT_FAILED,
            NOW - timedelta(hours=1),
            metadata_json='{"reason": "transient"}',
        )
        session.commit()
        report = failures_by_repo(session, since=SINCE, now=NOW)

    assert report["count"] == 1
    assert [r["repo"] for r in report["repos"]] == ["org/api"]


def test_totals_match_the_feed(session_factory) -> None:
    # The roll-up must agree with the per-run feed it complements: same runs,
    # same window, same derivation - so the counts can never drift.
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), repo="org/api")
        _blocked(session, ago=timedelta(hours=2), repo="org/api")
        _failed(session, ago=timedelta(hours=3), repo="org/web")
        session.commit()
        feed = failure_queue(session, since=SINCE, now=NOW)
        report = failures_by_repo(session, since=SINCE, now=NOW)

    assert report["count"] == feed["count"]
    assert report["blocked"] == feed["blocked"]
    assert report["failed"] == feed["failed"]
    assert sum(r["count"] for r in report["repos"]) == feed["count"]


def test_tie_break_by_most_recent_then_name(session_factory) -> None:
    # Two repos with equal counts: the one whose newest failure is more recent
    # sorts first; a further tie falls back to repo name.
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=5), repo="org/alpha")
        _blocked(session, ago=timedelta(minutes=30), repo="org/beta")  # more recent
        session.commit()
        report = failures_by_repo(session, since=SINCE, now=NOW)

    assert [r["repo"] for r in report["repos"]] == ["org/beta", "org/alpha"]


def test_totals_match_by_category(session_factory) -> None:
    # by-repo and by-category are two cuts of the same recently-failed set, so
    # their window totals (count / blocked / failed) must always agree.
    from foundry.memory.metrics import failures_by_category

    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), repo="org/api", reason="policy_denied")
        _failed(session, ago=timedelta(hours=2), repo="org/web", reason="agent error")
        _blocked(session, ago=timedelta(hours=3), repo=None, reason="budget_exceeded")
        session.commit()
        by_repo = failures_by_repo(session, since=SINCE, now=NOW)
        by_cat = failures_by_category(session, since=SINCE, now=NOW)

    assert by_repo["count"] == by_cat["count"]
    assert by_repo["blocked"] == by_cat["blocked"]
    assert by_repo["failed"] == by_cat["failed"]
