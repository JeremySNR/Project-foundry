"""failures_by_repo_trends: the by-repo over-time cut for the fleet dashboard's
failure surface (issue #37).

The by-repo dimension of ``failure_trends`` - the way
``failures_by_category_trends`` is to it by *reason* and ``delivery_by_repo_trends``
is to ``delivery_trends``. Where the org-wide ``failure_trends`` shows whether we
are failing *more* overall and the point-in-time ``failures_by_repo`` roll-up shows
*which repo* is failing most right now, this answers the question neither can: is a
*specific* repo's failure rate trending up or fading over time?

Reuses the same ``_failure_event_map`` / ``_FAILURE_EVENTS_BY_STATUS`` and
``_run_repo_map`` derivations the feed, the by-repo roll-up and the org-wide trend
use, so the totals here can never disagree with theirs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import AuditEventType, FoundryAgentJob, FoundryAuditEvent
from foundry.memory.metrics import (
    UNROUTED_REPO_LABEL,
    failure_trends,
    failures_by_repo,
    failures_by_repo_trends,
)
from foundry.schemas.common import OverallRisk, RunStatus

# A Wednesday, so day and (Monday-anchored) week buckets are easy to reason about.
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
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


def _blocked(session, *, ago: timedelta, repo: str | None) -> str:
    rid = _add_run(session, status=RunStatus.BLOCKED, created_at=NOW - ago, repo=repo)
    _add_event(
        session,
        rid,
        AuditEventType.RUN_BLOCKED,
        NOW - ago,
        metadata_json='{"category": "policy_denied"}',
    )
    return rid


def _failed(session, *, ago: timedelta, repo: str | None) -> str:
    rid = _add_run(
        session, status=RunStatus.EXECUTION_FAILED, created_at=NOW - ago, repo=repo
    )
    _add_event(
        session,
        rid,
        AuditEventType.AGENT_FAILED,
        NOW - ago,
        metadata_json='{"reason": "agent error"}',
    )
    return rid


def _repo(report: dict, name: str) -> dict:
    return next(r for r in report["repos"] if r["repo"] == name)


def test_empty(session_factory) -> None:
    with session_factory() as session:
        report = failures_by_repo_trends(session, since=SINCE, now=NOW)
    assert report["count"] == 0
    assert report["blocked"] == 0
    assert report["failed"] == 0
    assert report["distinct_repos"] == 0
    assert report["bucket"] == "day"
    assert report["periods"] == []
    assert report["repos"] == []


def test_groups_by_repo_with_aligned_zero_filled_series(session_factory) -> None:
    with session_factory() as session:
        # web: one on the NOW day, one three days earlier.
        _blocked(session, ago=timedelta(hours=1), repo="web")
        _blocked(session, ago=timedelta(days=3, hours=1), repo="web")
        # api: a single block on the NOW day.
        _blocked(session, ago=timedelta(hours=2), repo="api")
        session.commit()
        report = failures_by_repo_trends(session, since=SINCE, now=NOW, bucket="day")

    assert report["count"] == 3
    assert report["blocked"] == 3
    assert report["failed"] == 0
    assert report["distinct_repos"] == 2

    # One shared axis spanning the first to the last populated day across *all*
    # repos, oldest first, so the per-repo series line up column-for-column.
    assert report["periods"] == [
        "2026-06-07T00:00:00+00:00",
        "2026-06-08T00:00:00+00:00",
        "2026-06-09T00:00:00+00:00",
        "2026-06-10T00:00:00+00:00",
    ]

    web = _repo(report, "web")
    assert web["count"] == 2
    assert web["blocked"] == 2
    assert web["failed"] == 0
    # day 06-07 has one, the middle two are zero-filled, day 06-10 has one.
    assert [cell["count"] for cell in web["series"]] == [1, 0, 0, 1]

    api = _repo(report, "api")
    assert api["count"] == 1
    # api only appears on the last day, but its series is aligned to the same
    # 4-period axis (zero-filled on the days it had no failures).
    assert [cell["count"] for cell in api["series"]] == [0, 0, 0, 1]


def test_blocked_failed_split_per_repo(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), repo="web")
        _failed(session, ago=timedelta(hours=2), repo="web")
        session.commit()
        report = failures_by_repo_trends(session, since=SINCE, now=NOW)

    repo = _repo(report, "web")
    assert repo["count"] == 2
    assert repo["blocked"] == 1
    assert repo["failed"] == 1
    # The single (NOW-day) period carries the same split.
    assert [c["blocked"] for c in repo["series"]] == [1]
    assert [c["failed"] for c in repo["series"]] == [1]


def test_unrouted_run_bucketed_under_sentinel(session_factory) -> None:
    with session_factory() as session:
        # A run blocked at the gate before any agent dispatched - no job, no repo.
        _blocked(session, ago=timedelta(hours=1), repo=None)
        session.commit()
        report = failures_by_repo_trends(session, since=SINCE, now=NOW)

    assert report["distinct_repos"] == 1
    repo = report["repos"][0]
    assert repo["repo"] == UNROUTED_REPO_LABEL
    assert repo["count"] == 1


def test_repos_ordered_most_frequent_then_recent_then_name(session_factory) -> None:
    with session_factory() as session:
        # api: 2 (the most frequent).
        _blocked(session, ago=timedelta(days=1), repo="api")
        _blocked(session, ago=timedelta(days=2), repo="api")
        # Two singletons tied on count - the more recent one sorts first.
        _blocked(session, ago=timedelta(hours=1), repo="zeta")  # newest
        _blocked(session, ago=timedelta(days=4), repo="alpha")  # older
        session.commit()
        report = failures_by_repo_trends(session, since=SINCE, now=NOW)

    names = [r["repo"] for r in report["repos"]]
    # Most-frequent first; then the more-recent singleton (zeta) before the older
    # (alpha) despite alpha sorting first by name - recency wins the tiebreak.
    assert names == ["api", "zeta", "alpha"]


def test_week_bucket_collapses_same_week(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), repo="web")
        _blocked(session, ago=timedelta(days=2), repo="web")
        session.commit()
        report = failures_by_repo_trends(session, since=SINCE, now=NOW, bucket="week")

    assert report["bucket"] == "week"
    assert report["periods"] == ["2026-06-08T00:00:00+00:00"]  # Monday of NOW's week
    repo = _repo(report, "web")
    assert [c["count"] for c in repo["series"]] == [2]


def test_window_excludes_old_failures(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), repo="web")
        _blocked(session, ago=timedelta(days=40), repo="web")  # too old
        session.commit()
        report = failures_by_repo_trends(session, since=NOW - timedelta(days=7), now=NOW)

    assert report["count"] == 1
    repo = _repo(report, "web")
    assert repo["count"] == 1
    assert sum(c["count"] for c in repo["series"]) == 1


def test_only_failure_states_counted(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), repo="web")
        # An active run carrying a stale failure-marker event must not count.
        live = _add_run(
            session,
            status=RunStatus.AGENT_RUNNING,
            created_at=NOW - timedelta(hours=1),
            repo="web",
        )
        _add_event(
            session,
            live,
            AuditEventType.AGENT_FAILED,
            NOW - timedelta(hours=1),
            metadata_json='{"reason": "transient"}',
        )
        session.commit()
        report = failures_by_repo_trends(session, since=SINCE, now=NOW)

    assert report["count"] == 1
    assert report["distinct_repos"] == 1


def test_totals_match_the_org_wide_trend_and_rollup(session_factory) -> None:
    # This cut must agree with the org-wide trend and the by-repo roll-up it
    # refines: same runs, same window, same derivation - the totals can't drift.
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), repo="web")
        _blocked(session, ago=timedelta(days=2), repo="api")
        _failed(session, ago=timedelta(days=5), repo=None)
        session.commit()
        org = failure_trends(session, since=SINCE, now=NOW)
        rollup = failures_by_repo(session, since=SINCE, now=NOW)
        cut = failures_by_repo_trends(session, since=SINCE, now=NOW)

    assert cut["count"] == org["count"] == rollup["count"] == 3
    assert cut["blocked"] == org["blocked"] == rollup["blocked"]
    assert cut["failed"] == org["failed"] == rollup["failed"]
    assert cut["distinct_repos"] == rollup["distinct_repos"]
    # Per-repo window totals match the point-in-time roll-up's counts.
    rollup_counts = {r["repo"]: r["count"] for r in rollup["repos"]}
    assert {r["repo"]: r["count"] for r in cut["repos"]} == rollup_counts
    # Every repo's series sums to its window count.
    for repo in cut["repos"]:
        assert sum(cell["count"] for cell in repo["series"]) == repo["count"]


def test_bad_bucket_rejected(session_factory) -> None:
    with session_factory() as session:
        with pytest.raises(ValueError):
            failures_by_repo_trends(session, since=SINCE, now=NOW, bucket="month")
