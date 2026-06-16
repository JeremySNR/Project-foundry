"""failures_by_work_type: the work-type-axis triage cut for the fleet dashboard.

The work-type-grouped complement to ``failures_by_category`` and
``failures_by_repo`` (issue #37): where those group recently-failed runs by
*reason* and by *routed repo*, this groups the same runs by their *work type* -
the on-call's "do bugs fail while features ship?" question, the failure-side
mirror of ``delivery_by_work_type``. Counts per work type (with a blocked/failed
split and the newest/oldest age span), most-frequent first.

Reuses the same ``_failure_event_map`` / ``_FAILURE_EVENTS_BY_STATUS`` derivation
the feed and the other roll-ups use, so the totals here can never disagree with
theirs. Work type is derived from the run's latest ``TICKET_ANALYSIS`` artifact
(the same field ``record_outcome`` stamps onto ``FoundryRunOutcome.work_type``,
since ``FoundryRun`` carries no work-type column); runs that were never classified
bucket under the ``(unclassified)`` sentinel, exactly as in the delivery cut.
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
    failure_queue,
    failures_by_work_type,
)
from foundry.schemas.common import RunStatus

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


def _blocked(
    session, *, ago: timedelta, work_type: str | None, reason: str = "policy_denied"
) -> str:
    rid = _add_run(
        session, status=RunStatus.BLOCKED, created_at=NOW - ago, work_type=work_type
    )
    _add_event(
        session,
        rid,
        AuditEventType.RUN_BLOCKED,
        NOW - ago,
        metadata_json=f'{{"category": "{reason}"}}',
    )
    return rid


def _failed(
    session, *, ago: timedelta, work_type: str | None, reason: str = "agent error"
) -> str:
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
        metadata_json=f'{{"reason": "{reason}"}}',
    )
    return rid


def test_empty(session_factory) -> None:
    with session_factory() as session:
        report = failures_by_work_type(session, since=SINCE, now=NOW)
    assert report["count"] == 0
    assert report["blocked"] == 0
    assert report["failed"] == 0
    assert report["distinct_work_types"] == 0
    assert report["work_types"] == []


def test_groups_by_work_type_most_frequent_first(session_factory) -> None:
    with session_factory() as session:
        # 3 bug failures, 1 feature failure.
        _blocked(session, ago=timedelta(hours=1), work_type="bug")
        _blocked(session, ago=timedelta(hours=2), work_type="bug")
        _failed(session, ago=timedelta(hours=5), work_type="bug")
        _blocked(session, ago=timedelta(hours=3), work_type="feature")
        session.commit()
        report = failures_by_work_type(session, since=SINCE, now=NOW)

    assert report["count"] == 4
    assert report["blocked"] == 3
    assert report["failed"] == 1
    assert report["distinct_work_types"] == 2
    wts = report["work_types"]
    assert [w["work_type"] for w in wts] == ["bug", "feature"]

    top = wts[0]
    assert top["count"] == 3
    assert top["blocked"] == 2
    assert top["failed"] == 1
    # newest of the three is the 1h-ago one, oldest is the 5h-ago one.
    assert top["newest_failure_seconds"] == 1 * 3600
    assert top["oldest_failure_seconds"] == 5 * 3600
    assert top["last_failure"] == (NOW - timedelta(hours=1)).isoformat()


def test_unclassified_runs_bucketed_under_sentinel(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(minutes=5), work_type=None)
        _failed(session, ago=timedelta(minutes=6), work_type=None)
        session.commit()
        report = failures_by_work_type(session, since=SINCE, now=NOW)

    assert report["distinct_work_types"] == 1
    wt = report["work_types"][0]
    assert wt["work_type"] == UNCLASSIFIED_WORK_TYPE_LABEL
    assert wt["count"] == 2
    assert wt["blocked"] == 1
    assert wt["failed"] == 1


def test_analysis_without_work_type_is_unclassified(session_factory) -> None:
    # A TICKET_ANALYSIS artifact that simply never carried a work_type field is
    # treated as unclassified, not crashed on.
    with session_factory() as session:
        rid = _add_run(
            session, status=RunStatus.BLOCKED, created_at=NOW - timedelta(hours=1)
        )
        session.add(
            FoundryArtifact(
                id="a-noworktype",
                run_id=rid,
                artifact_type=ArtifactType.TICKET_ANALYSIS,
                version=1,
                content_json=json.dumps({"summary": "no work type here"}),
                content_hash="h-noworktype",
                created_at=NOW - timedelta(hours=1),
            )
        )
        _add_event(session, rid, AuditEventType.RUN_BLOCKED, NOW - timedelta(hours=1))
        session.commit()
        report = failures_by_work_type(session, since=SINCE, now=NOW)

    assert report["distinct_work_types"] == 1
    assert report["work_types"][0]["work_type"] == UNCLASSIFIED_WORK_TYPE_LABEL


def test_latest_analysis_version_wins(session_factory) -> None:
    # When a run was re-analysed, the latest TICKET_ANALYSIS artifact's work_type
    # is the one used - mirroring record_outcome's "latest wins" derivation.
    with session_factory() as session:
        rid = _add_run(
            session,
            status=RunStatus.BLOCKED,
            created_at=NOW - timedelta(hours=1),
            work_type="feature",  # version 1
        )
        session.add(
            FoundryArtifact(
                id="a-v2",
                run_id=rid,
                artifact_type=ArtifactType.TICKET_ANALYSIS,
                version=2,
                content_json=json.dumps({"work_type": "bug"}),  # reclassified
                content_hash="h-v2",
                created_at=NOW - timedelta(minutes=30),
            )
        )
        _add_event(session, rid, AuditEventType.RUN_BLOCKED, NOW - timedelta(hours=1))
        session.commit()
        report = failures_by_work_type(session, since=SINCE, now=NOW)

    assert [w["work_type"] for w in report["work_types"]] == ["bug"]


def test_blocked_and_failed_split_within_a_work_type(session_factory) -> None:
    # The work-type key comes from the analysis, not the run status, so a blocked
    # run and an execution-failed run can share a work type.
    with session_factory() as session:
        _blocked(session, ago=timedelta(minutes=10), work_type="tech_debt")
        _failed(session, ago=timedelta(minutes=20), work_type="tech_debt")
        session.commit()
        report = failures_by_work_type(session, since=SINCE, now=NOW)

    assert report["distinct_work_types"] == 1
    wt = report["work_types"][0]
    assert wt["work_type"] == "tech_debt"
    assert wt["count"] == 2
    assert wt["blocked"] == 1
    assert wt["failed"] == 1


def test_window_excludes_old_failures(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), work_type="bug")
        _blocked(session, ago=timedelta(days=40), work_type="bug")  # too old
        session.commit()
        report = failures_by_work_type(session, since=NOW - timedelta(days=7), now=NOW)

    assert report["count"] == 1
    assert report["work_types"][0]["count"] == 1


def test_only_failure_states_counted(session_factory) -> None:
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), work_type="bug")
        # An active run with a (stale) failure-marker event must not be counted.
        live = _add_run(
            session,
            status=RunStatus.AGENT_RUNNING,
            created_at=NOW - timedelta(hours=1),
            work_type="feature",
        )
        _add_event(
            session,
            live,
            AuditEventType.AGENT_FAILED,
            NOW - timedelta(hours=1),
            metadata_json='{"reason": "transient"}',
        )
        session.commit()
        report = failures_by_work_type(session, since=SINCE, now=NOW)

    assert report["count"] == 1
    assert [w["work_type"] for w in report["work_types"]] == ["bug"]


def test_totals_match_the_feed(session_factory) -> None:
    # The roll-up must agree with the per-run feed it complements: same runs,
    # same window, same derivation - so the counts can never drift.
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), work_type="bug")
        _blocked(session, ago=timedelta(hours=2), work_type="bug")
        _failed(session, ago=timedelta(hours=3), work_type="feature")
        session.commit()
        feed = failure_queue(session, since=SINCE, now=NOW)
        report = failures_by_work_type(session, since=SINCE, now=NOW)

    assert report["count"] == feed["count"]
    assert report["blocked"] == feed["blocked"]
    assert report["failed"] == feed["failed"]
    assert sum(w["count"] for w in report["work_types"]) == feed["count"]


def test_tie_break_by_most_recent_then_name(session_factory) -> None:
    # Two work types with equal counts: the one whose newest failure is more
    # recent sorts first; a further tie falls back to work-type name.
    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=5), work_type="alpha")
        _blocked(session, ago=timedelta(minutes=30), work_type="beta")  # more recent
        session.commit()
        report = failures_by_work_type(session, since=SINCE, now=NOW)

    assert [w["work_type"] for w in report["work_types"]] == ["beta", "alpha"]


def test_totals_match_other_cuts(session_factory) -> None:
    # by-work-type, by-repo and by-category are three cuts of the same
    # recently-failed set, so their window totals must always agree.
    from foundry.memory.metrics import failures_by_category, failures_by_repo

    with session_factory() as session:
        _blocked(session, ago=timedelta(hours=1), work_type="bug", reason="policy_denied")
        _failed(session, ago=timedelta(hours=2), work_type="feature", reason="agent error")
        _blocked(session, ago=timedelta(hours=3), work_type=None, reason="budget_exceeded")
        session.commit()
        by_wt = failures_by_work_type(session, since=SINCE, now=NOW)
        by_repo = failures_by_repo(session, since=SINCE, now=NOW)
        by_cat = failures_by_category(session, since=SINCE, now=NOW)

    for other in (by_repo, by_cat):
        assert by_wt["count"] == other["count"]
        assert by_wt["blocked"] == other["blocked"]
        assert by_wt["failed"] == other["failed"]
