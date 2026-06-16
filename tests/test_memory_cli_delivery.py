"""foundry-memory delivery / delivery-trends / delivery-by-repo /
delivery-by-repo-trends: the offline twins of the delivery metrics endpoints
(GET /metrics/delivery + /trends + /by-repo + /by-repo/trends, issue #37).

These read the DB directly and call the same ``memory/metrics.py`` derivations
the API serves, so an on-call engineer / auditor with DB access but no running
API or bearer token can still answer "what did we ship, where, and at what
cost?" and "is throughput trending up or down?". Mirrors how ``foundry-evidence``
and ``foundry-memory fleet`` are the offline twins of their endpoints.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRunOutcome
from foundry.memory.cli import main
from foundry.schemas.common import RunStatus

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{tmp_path}/foundry.db"


_counter = 0


def _seed(db_url: str):
    engine = make_engine(db_url)
    create_all(engine)
    return make_session_factory(engine)


def _add_outcome(
    session,
    *,
    outcome: str,
    repo: str | None = "payments-service",
    work_type: str | None = None,
    completed_at: datetime = NOW,
    jobs_count: int = 1,
    cost_usd: float | None = None,
    time_to_merge_seconds: int | None = None,
    routed_confidence: int | None = None,
    blocked_reason_category: str | None = None,
    escalations_count: int = 0,
    ci_failures_count: int = 0,
) -> str:
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
            work_type=work_type,
            routed_confidence=routed_confidence,
            trigger_type="label",
            created_at_run=completed_at - timedelta(hours=1),
            completed_at=completed_at,
            jobs_count=jobs_count,
            cost_usd=cost_usd,
            time_to_merge_seconds=time_to_merge_seconds,
            blocked_reason_category=blocked_reason_category,
            escalations_count=escalations_count,
            ci_failures_count=ci_failures_count,
            recorded_at=completed_at,
        )
    )
    return rid


def _run_cli(monkeypatch, db_url: str, *argv: str) -> None:
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr("sys.argv", ["foundry-memory", *argv])
    try:
        main()
    except SystemExit as exc:
        assert exc.code in (0, None)


# --- delivery -------------------------------------------------------------


def test_delivery_empty_database(monkeypatch, capsys, db_url) -> None:
    _seed(db_url)
    _run_cli(monkeypatch, db_url, "delivery")
    out = capsys.readouterr().out
    assert "0 runs finished" in out
    assert "PRs shipped       0" in out
    # No run reported cost => no conjured $0.
    assert "spend             -" in out
    assert "time-to-merge     - (no merges)" in out


def test_delivery_aggregates_outcomes_cost_and_precision(monkeypatch, capsys, db_url) -> None:
    sf = _seed(db_url)
    with sf() as session:
        _add_outcome(
            session,
            outcome="merged",
            cost_usd=1.50,
            jobs_count=2,  # one retry consumed
            time_to_merge_seconds=3600,
            routed_confidence=85,
        )
        _add_outcome(session, outcome="merged", routed_confidence=85)
        _add_outcome(
            session,
            outcome="blocked",
            blocked_reason_category="forbidden_path",
            routed_confidence=85,
        )
        session.commit()

    _run_cli(monkeypatch, db_url, "delivery")
    out = capsys.readouterr().out
    assert "3 runs finished" in out
    assert "PRs shipped       2" in out
    assert "blocked           1" in out
    assert "retries consumed  1" in out
    assert "spend             $1.5" in out
    # time-to-merge surfaces when there is at least one merge.
    assert "time-to-merge     median 1h00m" in out
    # blocks-by-reason breakdown + supersession line.
    assert "forbidden_path" in out
    assert "(superseded by later merge)" in out
    # routing precision by confidence band: 2/3 merged in the 80-89 band.
    assert "80-89" in out
    assert "2/3 merged" in out


def test_delivery_rejects_bad_window(monkeypatch, db_url) -> None:
    _seed(db_url)
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr("sys.argv", ["foundry-memory", "delivery", "--days", "0"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


# --- delivery-trends ------------------------------------------------------


def test_delivery_trends_empty_database(monkeypatch, capsys, db_url) -> None:
    _seed(db_url)
    _run_cli(monkeypatch, db_url, "delivery-trends")
    assert "No runs finished in the last 90d." in capsys.readouterr().out


def test_delivery_trends_buckets_over_time(monkeypatch, capsys, db_url) -> None:
    now = datetime.now(timezone.utc)
    sf = _seed(db_url)
    with sf() as session:
        _add_outcome(session, outcome="merged", completed_at=now, cost_usd=2.0)
        _add_outcome(session, outcome="blocked", completed_at=now)
        session.commit()

    _run_cli(monkeypatch, db_url, "delivery-trends", "--bucket", "day")
    out = capsys.readouterr().out
    assert "Delivery by day (last 90d):" in out
    assert "shipped   1" in out
    assert "blocked   1" in out
    assert "spend $2.0" in out


def test_delivery_trends_rejects_bad_bucket(monkeypatch, db_url) -> None:
    _seed(db_url)
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr(
        "sys.argv", ["foundry-memory", "delivery-trends", "--bucket", "month"]
    )
    # argparse rejects an out-of-choice value before the command runs.
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


# --- delivery-by-repo -----------------------------------------------------


def test_delivery_by_repo_empty_database(monkeypatch, capsys, db_url) -> None:
    _seed(db_url)
    _run_cli(monkeypatch, db_url, "delivery-by-repo")
    assert "No runs finished in the last 90d." in capsys.readouterr().out


def test_delivery_by_repo_groups_and_orders(monkeypatch, capsys, db_url) -> None:
    sf = _seed(db_url)
    with sf() as session:
        # payments-service ships 2, web ships 1 => payments first (most-shipping).
        _add_outcome(session, outcome="merged", repo="payments-service", cost_usd=1.0)
        _add_outcome(session, outcome="merged", repo="payments-service")
        _add_outcome(session, outcome="merged", repo="web-frontend")
        # An unrouted block buckets under the sentinel.
        _add_outcome(session, outcome="blocked", repo=None)
        session.commit()

    _run_cli(monkeypatch, db_url, "delivery-by-repo")
    out = capsys.readouterr().out
    assert "across 3 repo(s)" in out
    assert "payments-service" in out
    assert "web-frontend" in out
    assert "(unrouted)" in out
    # Most-shipping repo (payments-service, 2) is listed before web-frontend (1).
    assert out.index("payments-service") < out.index("web-frontend")


# --- delivery-by-work-type ------------------------------------------------


def test_delivery_by_work_type_empty_database(monkeypatch, capsys, db_url) -> None:
    _seed(db_url)
    _run_cli(monkeypatch, db_url, "delivery-by-work-type")
    assert "No runs finished in the last 90d." in capsys.readouterr().out


def test_delivery_by_work_type_groups_and_orders(monkeypatch, capsys, db_url) -> None:
    sf = _seed(db_url)
    with sf() as session:
        # bug ships 2, feature ships 1 => bug first (most-shipping).
        _add_outcome(session, outcome="merged", work_type="bug", cost_usd=1.0)
        _add_outcome(session, outcome="merged", work_type="bug")
        _add_outcome(session, outcome="merged", work_type="feature")
        # An unclassified block buckets under the sentinel.
        _add_outcome(session, outcome="blocked", work_type=None)
        session.commit()

    _run_cli(monkeypatch, db_url, "delivery-by-work-type")
    out = capsys.readouterr().out
    assert "across 3 type(s)" in out
    assert "bug" in out
    assert "feature" in out
    assert "(unclassified)" in out
    # Most-shipping type (bug, 2) is listed before feature (1).
    assert out.index("bug") < out.index("feature")


def test_delivery_by_work_type_rejects_bad_window(monkeypatch, db_url) -> None:
    _seed(db_url)
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr(
        "sys.argv", ["foundry-memory", "delivery-by-work-type", "--days", "0"]
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


# --- delivery-by-repo-trends ----------------------------------------------


def test_delivery_by_repo_trends_empty_database(monkeypatch, capsys, db_url) -> None:
    _seed(db_url)
    _run_cli(monkeypatch, db_url, "delivery-by-repo-trends")
    assert "No runs finished in the last 90d." in capsys.readouterr().out


def test_delivery_by_repo_trends_lists_per_repo_series(monkeypatch, capsys, db_url) -> None:
    now = datetime.now(timezone.utc)
    sf = _seed(db_url)
    with sf() as session:
        _add_outcome(
            session, outcome="merged", repo="payments-service", completed_at=now
        )
        _add_outcome(session, outcome="merged", repo="web-frontend", completed_at=now)
        session.commit()

    _run_cli(monkeypatch, db_url, "delivery-by-repo-trends", "--bucket", "week")
    out = capsys.readouterr().out
    assert "Per-repo delivery by week (last 90d" in out
    assert "payments-service: 1/1 merged" in out
    assert "web-frontend: 1/1 merged" in out
    assert "week of" in out


# --- delivery-by-work-type-trends -----------------------------------------


def test_delivery_by_work_type_trends_empty_database(
    monkeypatch, capsys, db_url
) -> None:
    _seed(db_url)
    _run_cli(monkeypatch, db_url, "delivery-by-work-type-trends")
    assert "No runs finished in the last 90d." in capsys.readouterr().out


def test_delivery_by_work_type_trends_lists_per_type_series(
    monkeypatch, capsys, db_url
) -> None:
    now = datetime.now(timezone.utc)
    sf = _seed(db_url)
    with sf() as session:
        _add_outcome(session, outcome="merged", work_type="bug", completed_at=now)
        _add_outcome(session, outcome="merged", work_type="feature", completed_at=now)
        session.commit()

    _run_cli(monkeypatch, db_url, "delivery-by-work-type-trends", "--bucket", "week")
    out = capsys.readouterr().out
    assert "Per-work-type delivery by week (last 90d" in out
    assert "bug: 1/1 merged" in out
    assert "feature: 1/1 merged" in out
    assert "week of" in out


def test_delivery_by_work_type_trends_rejects_bad_bucket(monkeypatch, db_url) -> None:
    _seed(db_url)
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr(
        "sys.argv",
        ["foundry-memory", "delivery-by-work-type-trends", "--bucket", "month"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
