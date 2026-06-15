"""foundry-evidence CLI: offline evidence-pack export (issue #36).

The CLI is the offline twin of the evidence endpoints - it reads the same
content-hashed trail straight from the DB and produces the same packs from the
same builders/renderers. These tests drive real runs (and a real epic) to a
terminal/parked state, then export them through the console entry point.
"""

from __future__ import annotations

import json

import pytest

from foundry.agents.manual import InMemoryFakeProvider
from foundry.compliance.cli import main
from foundry.db import create_all, make_engine, make_session_factory
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

EPIC_DESC = (
    "Add favourites across our surfaces.\n\n"
    "Repositories:\n"
    "- customer-web: add the favourites button\n"
    "- mobile-app: add the favourites button\n\n"
    "Acceptance Criteria:\n"
    "- A favourites button exists\n"
    "- Favourites persist across sessions\n"
)


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{tmp_path}/foundry.db"


def _orch(db_url: str) -> FoundryOrchestrator:
    engine = make_engine(db_url)
    create_all(engine)
    sf = make_session_factory(engine)
    return FoundryOrchestrator(sf, provider=InMemoryFakeProvider())


def _seed_merged_run(db_url: str) -> str:
    """Drive a run all the way to a merged PR so its pack is fully populated."""
    orch = _orch(db_url)
    provider: InMemoryFakeProvider = orch._provider  # type: ignore[attr-defined]
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
    return run_id


def _seed_epic(db_url: str):
    """Open an epic (parent + one child per repo); children are parked."""
    orch = _orch(db_url)
    return orch.intake_epic(
        RawTicket(
            issue_id="epic-99",
            issue_key="LIN-900",
            title="Add favourites everywhere",
            description=EPIC_DESC,
        ),
        trigger_type="label",
    )


def _run_cli(monkeypatch, db_url: str, *argv: str) -> None:
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr("sys.argv", ["foundry-evidence", *argv])
    try:
        main()
    except SystemExit as exc:
        assert exc.code in (0, None)


# -- run -----------------------------------------------------------------------


def test_run_json_to_stdout(monkeypatch, capsys, db_url) -> None:
    run_id = _seed_merged_run(db_url)
    _run_cli(monkeypatch, db_url, "run", run_id)
    pack = json.loads(capsys.readouterr().out)
    assert pack["run"]["id"] == run_id
    assert pack["integrity"]["verified"] is True
    # Control mappings come from committed config (the defaults here).
    assert any(c["framework"] == "SOC 2" for c in pack["control_mappings"])


def test_run_html_to_stdout(monkeypatch, capsys, db_url) -> None:
    run_id = _seed_merged_run(db_url)
    _run_cli(monkeypatch, db_url, "run", run_id, "--format", "html")
    out = capsys.readouterr().out
    assert "<!doctype html>" in out
    assert "Compliance evidence pack" in out
    assert "INTEGRITY VERIFIED" in out


def test_run_writes_to_output_file(monkeypatch, capsys, db_url, tmp_path) -> None:
    run_id = _seed_merged_run(db_url)
    out_path = tmp_path / "pack.json"
    _run_cli(monkeypatch, db_url, "run", run_id, "--output", str(out_path))
    captured = capsys.readouterr()
    assert f"Wrote {out_path}" in captured.err
    assert captured.out.strip() == ""  # nothing on stdout when writing a file
    pack = json.loads(out_path.read_text())
    assert pack["run"]["id"] == run_id


def test_run_pdf_to_output_file(monkeypatch, capsys, db_url, tmp_path) -> None:
    pytest.importorskip("fpdf")
    run_id = _seed_merged_run(db_url)
    out_path = tmp_path / "pack.pdf"
    _run_cli(monkeypatch, db_url, "run", run_id, "--format", "pdf", "--output", str(out_path))
    assert f"Wrote {out_path}" in capsys.readouterr().err
    data = out_path.read_bytes()
    assert data.startswith(b"%PDF-")
    assert b"%%EOF" in data


def test_run_not_found_exits_1(monkeypatch, capsys, db_url) -> None:
    _orch(db_url)  # create the schema, but no run with this id
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr("sys.argv", ["foundry-evidence", "run", "missing"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert "run not found" in capsys.readouterr().err


# -- epic ----------------------------------------------------------------------


def test_epic_json_by_parent(monkeypatch, capsys, db_url) -> None:
    result = _seed_epic(db_url)
    _run_cli(monkeypatch, db_url, "epic", result.parent_run_id)
    pack = json.loads(capsys.readouterr().out)
    assert pack["epic"]["root_run_id"] == result.parent_run_id
    assert set(pack["epic"]["child_run_ids"]) == set(result.child_run_ids)
    assert pack["run_count"] == 1 + len(result.child_run_ids)


def test_epic_resolves_root_from_a_child(monkeypatch, capsys, db_url) -> None:
    result = _seed_epic(db_url)
    child_id = result.child_run_ids[0]
    # Pointing the CLI at a child still exports the whole epic.
    _run_cli(monkeypatch, db_url, "epic", child_id)
    pack = json.loads(capsys.readouterr().out)
    assert pack["epic"]["root_run_id"] == result.parent_run_id
    assert child_id in pack["epic"]["child_run_ids"]


def test_epic_html(monkeypatch, capsys, db_url) -> None:
    result = _seed_epic(db_url)
    _run_cli(monkeypatch, db_url, "epic", result.parent_run_id, "--format", "html")
    out = capsys.readouterr().out
    assert "Epic evidence pack" in out
    assert "LIN-900" in out


def test_epic_not_found_exits_1(monkeypatch, capsys, db_url) -> None:
    _orch(db_url)
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr("sys.argv", ["foundry-evidence", "epic", "missing"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


# -- archive -------------------------------------------------------------------


def test_archive_json_default_window(monkeypatch, capsys, db_url) -> None:
    run_id = _seed_merged_run(db_url)
    _run_cli(monkeypatch, db_url, "archive")
    archive = json.loads(capsys.readouterr().out)
    assert archive["run_count"] == 1
    assert archive["runs"][0]["run"]["id"] == run_id
    assert archive["summary"]["verified"] is True


def test_archive_html(monkeypatch, capsys, db_url) -> None:
    _seed_merged_run(db_url)
    _run_cli(monkeypatch, db_url, "archive", "--format", "html")
    out = capsys.readouterr().out
    assert "Compliance evidence archive" in out


def test_archive_explicit_window(monkeypatch, capsys, db_url) -> None:
    _seed_merged_run(db_url)
    _run_cli(
        monkeypatch, db_url, "archive", "--from", "2020-01-01", "--to", "2999-01-01"
    )
    archive = json.loads(capsys.readouterr().out)
    assert archive["run_count"] == 1
    assert archive["range"]["from"].startswith("2020-01-01")


def test_archive_window_excludes_out_of_range(monkeypatch, capsys, db_url) -> None:
    _seed_merged_run(db_url)
    # A window entirely in the past contains no runs.
    _run_cli(
        monkeypatch, db_url, "archive", "--from", "2000-01-01", "--to", "2000-12-31"
    )
    archive = json.loads(capsys.readouterr().out)
    assert archive["run_count"] == 0


def test_archive_rejects_bad_date(monkeypatch, db_url) -> None:
    _orch(db_url)
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr(
        "sys.argv", ["foundry-evidence", "archive", "--from", "not-a-date"]
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_archive_rejects_bad_days(monkeypatch, db_url) -> None:
    _orch(db_url)
    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", db_url)
    monkeypatch.setattr("sys.argv", ["foundry-evidence", "archive", "--days", "0"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
