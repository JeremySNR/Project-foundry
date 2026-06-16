"""foundry-memory approvals / executions / reviews: the offline twins of the
three *in-flight* queue drill-downs (GET /metrics/approvals, /metrics/executions,
/metrics/reviews, issue #37).

These read the DB directly and call the same ``memory/metrics.py`` derivations the
API serves (``approval_queue`` / ``execution_queue`` / ``review_queue``), so an
on-call engineer with DB access but no running API / bearer token can still answer
"what is the oldest thing waiting, and is it overdue?" from the command line.
Mirrors how ``foundry-memory fleet``/``failures`` are the offline twins of the
operational fleet metrics.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import AuditEventType, FoundryAuditEvent
from foundry.memory.cli import main
from foundry.schemas.common import OverallRisk, RunStatus


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{tmp_path}/foundry.db"


_counter = 0


def _add_run(
    session,
    *,
    status: RunStatus,
    created_at: datetime,
    risk: OverallRisk | None = None,
    current_step: str | None = None,
) -> str:
    global _counter
    _counter += 1
    rid = f"q-{_counter}"
    session.add(
        FoundryRun(
            id=rid,
            linear_issue_id=f"i-{_counter}",
            linear_issue_key=f"ENG-{_counter}",
            status=status,
            trigger_type="label",
            risk_level=risk,
            current_step=current_step,
            created_at=created_at,
            updated_at=created_at,
        )
    )
    return rid


def _add_event(
    session, run_id: str, event_type: AuditEventType, created_at: datetime
) -> None:
    global _counter
    _counter += 1
    session.add(
        FoundryAuditEvent(
            id=f"qe-{_counter}",
            run_id=run_id,
            sequence=_counter,
            event_type=event_type,
            actor_type="foundry",
            created_at=created_at,
        )
    )


def _seed(db_url: str):
    engine = make_engine(db_url)
    create_all(engine)
    return make_session_factory(engine)


def _run_cli(monkeypatch, db_url: str, *argv: str, config: str | None = None) -> None:
    if config is None:
        monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    else:
        monkeypatch.setenv("FOUNDRY_CONFIG", config)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr("sys.argv", ["foundry-memory", *argv])
    try:
        main()
    except SystemExit as exc:
        assert exc.code in (0, None)


# --- approvals ------------------------------------------------------------


def test_approvals_empty_database(monkeypatch, capsys, db_url) -> None:
    _seed(db_url)
    _run_cli(monkeypatch, db_url, "approvals")
    assert "No runs are awaiting human approval." in capsys.readouterr().out


def test_approvals_lists_parked_run(monkeypatch, capsys, db_url) -> None:
    now = datetime.now(timezone.utc)
    sf = _seed(db_url)
    with sf() as session:
        rid = _add_run(
            session,
            status=RunStatus.WAITING_APPROVAL,
            created_at=now - timedelta(hours=3),
            risk=OverallRisk.HIGH,
            current_step="awaiting_approval (1/2)",
        )
        _add_event(
            session, rid, AuditEventType.APPROVAL_REQUESTED, now - timedelta(hours=3)
        )
        session.commit()

    _run_cli(monkeypatch, db_url, "approvals")
    out = capsys.readouterr().out
    assert "Approval queue (runs parked on a human): 1 total" in out
    assert "ENG-" in out  # the issue key
    assert "waiting_approval" in out
    assert "awaiting_approval (1/2)" in out  # the current step
    assert "3h" in out  # the wait age, dated from APPROVAL_REQUESTED


def test_approvals_flags_sla_breach(monkeypatch, capsys, db_url, tmp_path) -> None:
    """The CLI reads the same dashboard.approval_sla_seconds knob as the dashboard,
    so a long-waiting run is flagged exactly as GET /metrics/approvals flags it."""
    now = datetime.now(timezone.utc)
    sf = _seed(db_url)
    with sf() as session:
        rid = _add_run(
            session,
            status=RunStatus.WAITING_APPROVAL,
            created_at=now - timedelta(hours=2),
        )
        _add_event(
            session, rid, AuditEventType.APPROVAL_REQUESTED, now - timedelta(hours=2)
        )
        session.commit()

    config = tmp_path / "foundry.yaml"
    config.write_text("dashboard:\n  approval_sla_seconds: 60\n")

    _run_cli(monkeypatch, db_url, "approvals", config=str(config))
    out = capsys.readouterr().out
    assert "1 breaching SLA 60s" in out  # the header summary
    assert "! breaching SLA" in out  # the per-row marker


# --- executions -----------------------------------------------------------


def test_executions_empty_database(monkeypatch, capsys, db_url) -> None:
    _seed(db_url)
    _run_cli(monkeypatch, db_url, "executions")
    assert "No agents are currently running." in capsys.readouterr().out


def test_executions_lists_in_flight_run(monkeypatch, capsys, db_url) -> None:
    now = datetime.now(timezone.utc)
    sf = _seed(db_url)
    with sf() as session:
        rid = _add_run(
            session,
            status=RunStatus.AGENT_RUNNING,
            created_at=now - timedelta(minutes=90),
        )
        # The run-time is dated from the latest AGENT_STARTED, not created_at.
        _add_event(
            session, rid, AuditEventType.AGENT_STARTED, now - timedelta(minutes=30)
        )
        session.commit()

    _run_cli(monkeypatch, db_url, "executions")
    out = capsys.readouterr().out
    assert "Execution queue (agents in flight): 1 total" in out
    assert "agent_running" in out
    assert "30m" in out  # dated from AGENT_STARTED, not the 90m-old created_at


# --- reviews --------------------------------------------------------------


def test_reviews_empty_database(monkeypatch, capsys, db_url) -> None:
    _seed(db_url)
    _run_cli(monkeypatch, db_url, "reviews")
    assert "No open PRs are awaiting review." in capsys.readouterr().out


def test_reviews_lists_open_pr_with_two_ages(monkeypatch, capsys, db_url) -> None:
    now = datetime.now(timezone.utc)
    sf = _seed(db_url)
    with sf() as session:
        rid = _add_run(
            session, status=RunStatus.PR_OPEN, created_at=now - timedelta(hours=5)
        )
        # Opened 4h ago, last pushed 1h ago: unreviewed=4h, inactive=1h.
        _add_event(session, rid, AuditEventType.PR_OPENED, now - timedelta(hours=4))
        _add_event(session, rid, AuditEventType.PR_UPDATED, now - timedelta(hours=1))
        session.commit()

    _run_cli(monkeypatch, db_url, "reviews")
    out = capsys.readouterr().out
    assert "Review queue (open PRs awaiting review): 1 total" in out
    assert "pr_open" in out
    assert "4h" in out  # unreviewed age (from PR_OPENED)
    assert "1h" in out  # inactive age (from the latest PR_UPDATED)


def test_reviews_flags_review_and_stale_slas(monkeypatch, capsys, db_url, tmp_path) -> None:
    now = datetime.now(timezone.utc)
    sf = _seed(db_url)
    with sf() as session:
        rid = _add_run(
            session, status=RunStatus.PR_OPEN, created_at=now - timedelta(hours=6)
        )
        # Opened 5h ago and untouched since => both review-age and staleness breach.
        _add_event(session, rid, AuditEventType.PR_OPENED, now - timedelta(hours=5))
        session.commit()

    config = tmp_path / "foundry.yaml"
    config.write_text(
        "dashboard:\n  review_sla_seconds: 60\n  review_stale_sla_seconds: 60\n"
    )

    _run_cli(monkeypatch, db_url, "reviews", config=str(config))
    out = capsys.readouterr().out
    assert "1 breaching SLA 60s" in out  # the review-age header summary
    assert "staleness" in out  # the separate staleness summary line
    assert "review+stale SLA" in out  # both per-row flags set
