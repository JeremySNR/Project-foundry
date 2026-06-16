"""foundry-memory fleet / failures: the offline twins of the operational fleet
metrics endpoints (GET /metrics/fleet, GET /metrics/failures, issue #37).

These read the DB directly and call the same ``memory/metrics.py`` derivations
the API serves, so an on-call engineer / auditor with DB access but no running
API or bearer token can still answer "is everything healthy?" and "what broke?".
Mirrors how ``foundry-evidence`` is the offline twin of the evidence endpoints.
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
    rid = f"r-{_counter}"
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


# --- fleet ----------------------------------------------------------------


def test_fleet_empty_database(monkeypatch, capsys, db_url) -> None:
    _seed(db_url)
    _run_cli(monkeypatch, db_url, "fleet")
    out = capsys.readouterr().out
    assert "Fleet snapshot (live):" in out
    assert "runs total      0" in out
    assert "spend committed -" in out  # no in-flight cost => no conjured $0


def test_fleet_counts_parked_and_terminal_runs(monkeypatch, capsys, db_url) -> None:
    now = datetime.now(timezone.utc)
    sf = _seed(db_url)
    with sf() as session:
        _add_run(session, status=RunStatus.WAITING_APPROVAL, created_at=now)
        _add_run(session, status=RunStatus.COMPLETE, created_at=now)
        session.commit()

    _run_cli(monkeypatch, db_url, "fleet")
    out = capsys.readouterr().out
    assert "runs total      2" in out
    assert "awaiting human  1" in out
    assert "runs terminal   1" in out
    # The by-status breakdown lists each live status with its count.
    assert "waiting_approval" in out
    assert "complete" in out


def test_fleet_honours_sla_config(monkeypatch, capsys, db_url, tmp_path) -> None:
    """The CLI reads the same dashboard.*_sla_seconds knobs as the dashboard, so
    the breach signal matches GET /metrics/fleet."""
    sf = _seed(db_url)
    with sf() as session:
        _add_run(
            session, status=RunStatus.WAITING_APPROVAL, created_at=datetime.now(timezone.utc)
        )
        session.commit()

    config = tmp_path / "foundry.yaml"
    config.write_text("dashboard:\n  approval_sla_seconds: 10\n")

    _run_cli(monkeypatch, db_url, "fleet", config=str(config))
    out = capsys.readouterr().out
    # SLA configured but the just-created run hasn't breached yet.
    assert "SLA 10s" in out


# --- failures -------------------------------------------------------------


def test_failures_empty_database(monkeypatch, capsys, db_url) -> None:
    _seed(db_url)
    _run_cli(monkeypatch, db_url, "failures")
    assert "No runs failed in the last 7d" in capsys.readouterr().out


def test_failures_lists_blocked_run_with_reason(monkeypatch, capsys, db_url) -> None:
    now = datetime.now(timezone.utc)
    sf = _seed(db_url)
    with sf() as session:
        rid = _add_run(
            session,
            status=RunStatus.BLOCKED,
            created_at=now - timedelta(hours=2),
            risk=OverallRisk.HIGH,
        )
        _add_event(
            session,
            rid,
            AuditEventType.RUN_BLOCKED,
            now - timedelta(hours=2),
            metadata_json='{"category": "forbidden_path"}',
        )
        session.commit()

    _run_cli(monkeypatch, db_url, "failures")
    out = capsys.readouterr().out
    assert "1 total, 1 blocked, 0 execution-failed" in out
    assert "forbidden_path" in out  # the reason from the audit metadata
    assert "blocked" in out
    assert "ENG-" in out  # the issue key


def test_failures_window_excludes_old_incidents(monkeypatch, capsys, db_url) -> None:
    now = datetime.now(timezone.utc)
    sf = _seed(db_url)
    with sf() as session:
        rid = _add_run(
            session, status=RunStatus.BLOCKED, created_at=now - timedelta(days=30)
        )
        _add_event(
            session,
            rid,
            AuditEventType.RUN_BLOCKED,
            now - timedelta(days=30),
            metadata_json='{"category": "policy_denied"}',
        )
        session.commit()

    # Default 7-day window excludes a 30-day-old block...
    _run_cli(monkeypatch, db_url, "failures")
    assert "No runs failed in the last 7d" in capsys.readouterr().out

    # ...but a wide enough window surfaces it.
    _run_cli(monkeypatch, db_url, "failures", "--days", "60")
    out = capsys.readouterr().out
    assert "1 total, 1 blocked" in out
    assert "policy_denied" in out


def test_failures_rejects_bad_window(monkeypatch, db_url) -> None:
    _seed(db_url)
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr("sys.argv", ["foundry-memory", "failures", "--days", "0"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
