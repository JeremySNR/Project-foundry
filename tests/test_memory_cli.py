"""foundry-memory CLI: backfill rebuilds outcomes from the audit trail alone."""

from __future__ import annotations

import pytest

from foundry.agents.manual import InMemoryFakeProvider
from foundry.db import create_all, make_engine, make_session_factory
from foundry.db.models import ArtifactType, FoundryArtifact, FoundryRunOutcome
from foundry.memory.cli import main
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.common import PRStatus
from foundry.schemas.pr import PullRequestState
from foundry.schemas.ticket import RawTicket

READY_DESC = (
    "Customers want to favourite items.\n\n"
    "Acceptance Criteria:\n"
    "- A favourites button exists\n"
    "- Favourites persist across sessions\n"
)


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{tmp_path}/foundry.db"


def _seed_merged_run(db_url: str, *, wipe_memory: bool) -> str:
    """Drive a run to merged, optionally erasing what the hook recorded."""
    engine = make_engine(db_url)
    create_all(engine)
    sf = make_session_factory(engine)
    provider = InMemoryFakeProvider()
    orch = FoundryOrchestrator(sf, provider=provider)
    run_id = orch.intake_and_plan(
        RawTicket(
            issue_id="i-1",
            issue_key="LIN-123",
            title="Add customer favourites",
            description=READY_DESC,
            known_repositories=["customer-web"],
        ),
        trigger_type="label",
    )
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    final = provider.run(job.job_id)
    orch.record_pr(
        run_id,
        PullRequestState(
            repo="customer-web",
            pr_number=1,
            url=final.pr_url,
            branch=final.branch,
            status=PRStatus.MERGED,
            files_changed=["src/features/favourites/index.ts"],
        ),
    )
    if wipe_memory:
        # Simulate a run that finished before delivery memory existed.
        with sf() as session:
            session.query(FoundryRunOutcome).delete()
            session.query(FoundryArtifact).filter_by(
                artifact_type=ArtifactType.FINAL_SUMMARY
            ).delete()
            session.commit()
    return run_id


def _outcomes(db_url: str) -> list[FoundryRunOutcome]:
    sf = make_session_factory(make_engine(db_url))
    with sf() as session:
        return session.query(FoundryRunOutcome).all()


def _run_cli(monkeypatch, db_url: str, *argv: str) -> None:
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr("sys.argv", ["foundry-memory", *argv])
    try:
        main()
    except SystemExit as exc:
        assert exc.code in (0, None)


def test_backfill_rebuilds_outcomes_from_audit_trail(
    monkeypatch, capsys, db_url
) -> None:
    run_id = _seed_merged_run(db_url, wipe_memory=True)
    assert _outcomes(db_url) == []

    _run_cli(monkeypatch, db_url, "backfill")
    assert "1 outcomes backfilled" in capsys.readouterr().out

    rows = _outcomes(db_url)
    assert len(rows) == 1
    assert rows[0].run_id == run_id
    assert rows[0].outcome == "merged"
    assert rows[0].repo == "customer-web"
    assert rows[0].time_to_merge_seconds is not None


def test_backfill_is_idempotent(monkeypatch, capsys, db_url) -> None:
    _seed_merged_run(db_url, wipe_memory=True)
    _run_cli(monkeypatch, db_url, "backfill")
    capsys.readouterr()
    _run_cli(monkeypatch, db_url, "backfill")
    assert "0 outcomes backfilled" in capsys.readouterr().out
    assert len(_outcomes(db_url)) == 1


def test_backfill_skips_rows_written_by_the_hook(monkeypatch, capsys, db_url) -> None:
    _seed_merged_run(db_url, wipe_memory=False)  # the orchestrator hook recorded it
    _run_cli(monkeypatch, db_url, "backfill")
    assert "0 outcomes backfilled" in capsys.readouterr().out


def test_recompute_rederives_existing_rows(monkeypatch, capsys, db_url) -> None:
    _seed_merged_run(db_url, wipe_memory=False)
    _run_cli(monkeypatch, db_url, "backfill", "--recompute")
    assert "1 outcomes recomputed" in capsys.readouterr().out
    assert len(_outcomes(db_url)) == 1


def test_show_priors_prints_history(monkeypatch, capsys, db_url) -> None:
    _seed_merged_run(db_url, wipe_memory=False)
    _run_cli(monkeypatch, db_url, "show-priors")
    out = capsys.readouterr().out
    assert "customer-web" in out
    assert "1/1" in out


def test_show_priors_empty_database(monkeypatch, capsys, db_url) -> None:
    engine = make_engine(db_url)
    create_all(engine)
    _run_cli(monkeypatch, db_url, "show-priors")
    assert "No routed outcomes" in capsys.readouterr().out


def test_show_scorecards_prints_provider(monkeypatch, capsys, db_url) -> None:
    _seed_merged_run(db_url, wipe_memory=False)
    _run_cli(monkeypatch, db_url, "show-scorecards")
    out = capsys.readouterr().out
    # InMemoryFakeProvider dispatches as the "fake" provider.
    assert "fake" in out
    assert "1/1 merged" in out
    assert "customer-web" in out


def test_recommend_agent_empty_database(monkeypatch, capsys, db_url) -> None:
    engine = make_engine(db_url)
    create_all(engine)
    _run_cli(monkeypatch, db_url, "recommend-agent", "--work-type", "feature")
    out = capsys.readouterr().out
    assert "Recommended: none" in out
    assert "not enough evidence" in out


def test_recommend_agent_with_lowered_floor(monkeypatch, capsys, db_url) -> None:
    _seed_merged_run(db_url, wipe_memory=False)
    # One merged run; lower the floor so it qualifies.
    _run_cli(monkeypatch, db_url, "recommend-agent", "--min-samples", "1")
    out = capsys.readouterr().out
    assert "Recommended:" in out
    assert "fake" in out
    assert "yes" in out  # the eligible column


def test_recommend_agent_rejects_bad_window(monkeypatch, db_url) -> None:
    engine = make_engine(db_url)
    create_all(engine)
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr("sys.argv", ["foundry-memory", "recommend-agent", "--days", "0"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_show_scorecards_empty_database(monkeypatch, capsys, db_url) -> None:
    engine = make_engine(db_url)
    create_all(engine)
    _run_cli(monkeypatch, db_url, "show-scorecards")
    assert "No dispatched outcomes" in capsys.readouterr().out


def test_show_scorecard_trends_prints_provider(monkeypatch, capsys, db_url) -> None:
    _seed_merged_run(db_url, wipe_memory=False)
    _run_cli(monkeypatch, db_url, "show-scorecard-trends")
    out = capsys.readouterr().out
    assert "fake" in out
    assert "1/1 merged overall" in out
    # The week-bucket label and a populated period are both present.
    assert "week of" in out


def test_show_scorecard_trends_day_bucket(monkeypatch, capsys, db_url) -> None:
    _seed_merged_run(db_url, wipe_memory=False)
    _run_cli(monkeypatch, db_url, "show-scorecard-trends", "--bucket", "day")
    out = capsys.readouterr().out
    assert "fake" in out
    assert "by day" in out


def test_show_scorecard_trends_empty_database(monkeypatch, capsys, db_url) -> None:
    engine = make_engine(db_url)
    create_all(engine)
    _run_cli(monkeypatch, db_url, "show-scorecard-trends")
    assert "No dispatched outcomes" in capsys.readouterr().out


def test_show_scorecard_trends_rejects_bad_window(monkeypatch, db_url) -> None:
    engine = make_engine(db_url)
    create_all(engine)
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr(
        "sys.argv", ["foundry-memory", "show-scorecard-trends", "--days", "0"]
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
