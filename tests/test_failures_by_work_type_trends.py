"""failures_by_work_type_trends: the by-work-type over-time cut for the fleet
dashboard's failure surface (issue #37).

The by-work-type dimension of ``failure_trends`` - the way
``failures_by_repo_trends`` is to it by *repo* and ``delivery_by_work_type_trends``
is to ``delivery_trends``. Where the org-wide ``failure_trends`` shows whether we
are failing *more* overall and the point-in-time ``failures_by_work_type`` roll-up
shows *which kind of work* is failing most right now, this answers the question
neither can: is a *specific* work type's failure rate trending up or fading over
time - are bugs failing more while features ship?

Reuses the same ``_failure_event_map`` / ``_FAILURE_EVENTS_BY_STATUS`` and
``_run_work_type_map`` derivations the feed, the by-work-type roll-up and the
org-wide trend use, so the totals here can never disagree with theirs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import (
    ArtifactType,
    AuditEventType,
    FoundryArtifact,
    FoundryAuditEvent,
)
from foundry.memory.metrics import (
    UNCLASSIFIED_WORK_TYPE_LABEL,
    failure_trends,
    failures_by_work_type,
    failures_by_work_type_trends,
)
from foundry.schemas.common import RunStatus

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
    work_type: str | None = None,
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
            created_at=created_at,
            updated_at=created_at,
        )
    )
    # FoundryRun has no work_type column; it is derived from the latest
    # TICKET_ANALYSIS artifact - the same field record_outcome reads. A run with no
    # analysis artifact (or an analysis with no work_type) is correctly counted as
    # unclassified.
    if work_type is not None:
        _counter += 1
        session.add(
            FoundryArtifact(
                id=f"a-{_counter}",
                run_id=rid,
                artifact_type=ArtifactType.TICKET_ANALYSIS,
                version=1,
                content_json=json.dumps({"work_type": work_type}),
                content_hash=f"h-{_counter}",
                created_at=created_at,
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


def _blocked(session, *, ago: timedelta, work_type: str | None) -> str:
    rid = _add_run(
        session, status=RunStatus.BLOCKED, created_at=NOW - ago, work_type=work_type
    )
    _add_event(
        session,
        rid,
        AuditEventType.RUN_BLOCKED,
        NOW - ago,
        metadata_json='{"category": "policy_denied"}',
    )
    return rid


def _failed(session, *, ago: timedelta, work_type: str | None) -> str:
    rid = _add_run(
        session,
        status=RunStatus.EXECUTION_FAILED,
        created_at=NOW - ago,
        work_type=work_type,
    )
    _add_event(
        session,
        rid,
        AuditEventType.AGENT_FAILED,
        NOW - ago,
        metadata_json='{"reason": "agent error"}',
    )
    return rid


def _wt(report: dict, name: str) -> dict:
    return next(w for w in report["work_types"] if w["work_type"] == name)


def test_empty(session_factory) -> None:
    with session_factory() as session:
        report = failures_by_work_type_trends(session, since=SINCE, now=NOW)
    assert report["count"] == 0
    assert report["blocked"] == 0
    assert report["failed"] == 0
    assert report["distinct_work_types"] == 0
    assert report["bucket"] == "day"
    assert report["periods"] == []
    assert report["work_types"] == []


def test_groups_by_work_type_with_aligned_zero_filled_series(session_factory) -> None:
    with session_factory() as session:
        # bug: one on the NOW day, one three days earlier.
        _blocked(session, ago=timedelta(hours=1), work_type="bug")
        _blocked(session, ago=timedelta(days=3, hours=1), work_type="bug")
        # feature: a single block on the NOW day.
        _blocked(session, ago=timedelta(hours=2), work_type="feature")
        session.commit()
        report = failures_by_work_type_trends(
            session, since=SINCE, now=NOW, bucket="day"
        )

    assert report["count"] == 3
    assert report["blocked"] == 3
    assert report["failed"] == 0
    assert report["distinct_work_types"] == 2

    # One shared axis spanning the first to the last populated day across *all*
    # work types, oldest first, so the per-work-type series line up
    # column-for-column.
    assert report["periods"] == [
        "2026-06-07T00:00:00+00:00",
        "2026-06-08T00:00:00+00:00",
        "2026-06-09T00:00:00+00:00",
        "2026-06-10T00:00:00+00:00",
    ]

    bug = _wt(report, "bug")
    assert bug["count"] == 2
    assert bug["blocked"] == 2
    assert bug["failed"] == 0
    # day 06-07 has one, the middle two are zero-filled, day 06-10 has one.
    assert [cell["count"] for cell in bug["series"]] == [1, 0, 0, 1]

    feature = _wt(report, "feature")
    assert feature["count"] == 1
    # feature only appears on the last day, but its series is aligned to the same
    # 4-period axis (zero-filled on the days it had no failures).
    assert [cell["count"] for cell in feature["series"]] == [0, 0, 0, 1]


def test_blocked_failed_split_per_work_type(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), work_type="bug")
        _failed(session, ago=timedelta(hours=2), work_type="bug")
        session.commit()
        report = failures_by_work_type_trends(session, since=SINCE, now=NOW)

    bug = _wt(report, "bug")
    assert bug["count"] == 2
    assert bug["blocked"] == 1
    assert bug["failed"] == 1
    # The single (NOW-day) period carries the same split.
    assert [c["blocked"] for c in bug["series"]] == [1]
    assert [c["failed"] for c in bug["series"]] == [1]


def test_unclassified_run_bucketed_under_sentinel(session_factory) -> None:
    with session_factory() as session:
        # A run with no analysis artifact - never classified, no work type.
        _blocked(session, ago=timedelta(hours=1), work_type=None)
        session.commit()
        report = failures_by_work_type_trends(session, since=SINCE, now=NOW)

    assert report["distinct_work_types"] == 1
    wt = report["work_types"][0]
    assert wt["work_type"] == UNCLASSIFIED_WORK_TYPE_LABEL
    assert wt["count"] == 1


def test_work_types_ordered_most_frequent_then_recent_then_name(
    session_factory,
) -> None:
    with session_factory() as session:
        # bug: 2 (the most frequent).
        _blocked(session, ago=timedelta(days=1), work_type="bug")
        _blocked(session, ago=timedelta(days=2), work_type="bug")
        # Two singletons tied on count - the more recent one sorts first.
        _blocked(session, ago=timedelta(hours=1), work_type="zeta")  # newest
        _blocked(session, ago=timedelta(days=4), work_type="alpha")  # older
        session.commit()
        report = failures_by_work_type_trends(session, since=SINCE, now=NOW)

    names = [w["work_type"] for w in report["work_types"]]
    # Most-frequent first; then the more-recent singleton (zeta) before the older
    # (alpha) despite alpha sorting first by name - recency wins the tiebreak.
    assert names == ["bug", "zeta", "alpha"]


def test_week_bucket_collapses_same_week(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), work_type="bug")
        _blocked(session, ago=timedelta(days=2), work_type="bug")
        session.commit()
        report = failures_by_work_type_trends(
            session, since=SINCE, now=NOW, bucket="week"
        )

    assert report["bucket"] == "week"
    assert report["periods"] == ["2026-06-08T00:00:00+00:00"]  # Monday of NOW's week
    bug = _wt(report, "bug")
    assert [c["count"] for c in bug["series"]] == [2]


def test_window_excludes_old_failures(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), work_type="bug")
        _blocked(session, ago=timedelta(days=40), work_type="bug")  # too old
        session.commit()
        report = failures_by_work_type_trends(
            session, since=NOW - timedelta(days=7), now=NOW
        )

    assert report["count"] == 1
    bug = _wt(report, "bug")
    assert bug["count"] == 1
    assert sum(c["count"] for c in bug["series"]) == 1


def test_only_failure_states_counted(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), work_type="bug")
        # An active run carrying a stale failure-marker event must not count.
        live = _add_run(
            session,
            status=RunStatus.AGENT_RUNNING,
            created_at=NOW - timedelta(hours=1),
            work_type="bug",
        )
        _add_event(
            session,
            live,
            AuditEventType.AGENT_FAILED,
            NOW - timedelta(hours=1),
            metadata_json='{"reason": "transient"}',
        )
        session.commit()
        report = failures_by_work_type_trends(session, since=SINCE, now=NOW)

    assert report["count"] == 1
    assert report["distinct_work_types"] == 1


def test_totals_match_the_org_wide_trend_and_rollup(session_factory) -> None:
    # This cut must agree with the org-wide trend and the by-work-type roll-up it
    # refines: same runs, same window, same derivation - the totals can't drift.
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), work_type="bug")
        _blocked(session, ago=timedelta(days=2), work_type="feature")
        _failed(session, ago=timedelta(days=5), work_type=None)
        session.commit()
        org = failure_trends(session, since=SINCE, now=NOW)
        rollup = failures_by_work_type(session, since=SINCE, now=NOW)
        cut = failures_by_work_type_trends(session, since=SINCE, now=NOW)

    assert cut["count"] == org["count"] == rollup["count"] == 3
    assert cut["blocked"] == org["blocked"] == rollup["blocked"]
    assert cut["failed"] == org["failed"] == rollup["failed"]
    assert cut["distinct_work_types"] == rollup["distinct_work_types"]
    # Per-work-type window totals match the point-in-time roll-up's counts.
    rollup_counts = {w["work_type"]: w["count"] for w in rollup["work_types"]}
    assert {w["work_type"]: w["count"] for w in cut["work_types"]} == rollup_counts
    # Every work type's series sums to its window count.
    for wt in cut["work_types"]:
        assert sum(cell["count"] for cell in wt["series"]) == wt["count"]


def test_bad_bucket_rejected(session_factory) -> None:
    with session_factory() as session:
        with pytest.raises(ValueError):
            failures_by_work_type_trends(
                session, since=SINCE, now=NOW, bucket="month"
            )
