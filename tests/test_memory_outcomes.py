"""Delivery-memory outcome recording: every terminal path leaves one row."""

from __future__ import annotations

import json

import pytest

from foundry.agents.manual import InMemoryFakeProvider
from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import (
    ArtifactType,
    FoundryAgentJob,
    FoundryArtifact,
    FoundryRunOutcome,
)
from foundry.memory import outcomes as outcomes_module
from foundry.orchestrator import FoundryOrchestrator, OrchestratorError
from foundry.schemas.common import CIStatus, PRStatus, RunStatus
from foundry.schemas.pr import PullRequestState
from foundry.schemas.ticket import RawTicket

READY_DESC = """\
Customers want to favourite items.

Acceptance Criteria:
- A favourites button exists
- Favourites persist across sessions
"""


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


def _ready_ticket(**overrides) -> RawTicket:
    base = dict(
        issue_id="i-1",
        issue_key="LIN-123",
        title="Add customer favourites",
        description=READY_DESC,
        known_repositories=["customer-web"],
    )
    base.update(overrides)
    return RawTicket(**base)


def _dispatched_run(session_factory, orch_kwargs=None) -> tuple:
    provider = InMemoryFakeProvider()
    orch = FoundryOrchestrator(session_factory, provider=provider, **(orch_kwargs or {}))
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)
    return orch, run_id


def _pr(branch="foundry/lin-123-add-customer-favourites", **overrides) -> PullRequestState:
    base = dict(
        repo="customer-web",
        pr_number=7,
        url="https://github.com/example/customer-web/pull/7",
        branch=branch,
        status=PRStatus.OPEN,
        files_changed=["src/features/favourites/index.ts"],
    )
    base.update(overrides)
    return PullRequestState(**base)


def _outcome(session_factory, run_id: str) -> FoundryRunOutcome | None:
    with session_factory() as s:
        return s.get(FoundryRunOutcome, run_id)


def _summaries(session_factory, run_id: str) -> list[FoundryArtifact]:
    with session_factory() as s:
        return (
            s.query(FoundryArtifact)
            .filter_by(run_id=run_id, artifact_type=ArtifactType.FINAL_SUMMARY)
            .all()
        )


# -- the happy path -------------------------------------------------------------


def test_merged_pr_records_merged_outcome(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    orch.record_pr(run_id, _pr())
    orch.record_pr(run_id, _pr(status=PRStatus.MERGED))

    row = _outcome(session_factory, run_id)
    assert row is not None
    assert row.outcome == "merged"
    assert row.repo == "customer-web"
    assert row.issue_key_prefix == "LIN"
    assert row.work_type == "feature"
    assert row.jobs_count == 1
    assert row.time_to_merge_seconds is not None and row.time_to_merge_seconds >= 0
    assert row.completed_at is not None
    assert row.routed_confidence == 90  # explicit known_repositories signal
    assert row.files_changed_count == 1
    assert row.blocked_reason_category is None
    assert row.block_justified is None


def test_merged_outcome_writes_final_summary_artifact_once(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    orch.record_pr(run_id, _pr(status=PRStatus.MERGED))
    summaries = _summaries(session_factory, run_id)
    assert len(summaries) == 1
    content = json.loads(summaries[0].content_json)
    assert content["outcome"] == "merged"
    assert content["repo"] == "customer-web"


def test_no_outcome_recorded_while_run_is_active(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    orch.record_pr(run_id, _pr())  # PR_OPEN: not terminal
    assert _outcome(session_factory, run_id) is None
    assert _summaries(session_factory, run_id) == []


def test_retries_counted_from_remediation_jobs(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    failing = _pr(ci_status=CIStatus.FAILING, summary="pytest: 2 failed")
    assert orch.record_pr(run_id, failing) is RunStatus.AGENT_RUNNING
    orch.record_pr(run_id, _pr(status=PRStatus.MERGED))

    row = _outcome(session_factory, run_id)
    assert row.outcome == "merged"
    assert row.jobs_count == 2  # original dispatch + one remediation
    assert row.ci_failures_count == 1


def test_cost_summed_across_jobs(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    with session_factory() as s:
        job = s.query(FoundryAgentJob).filter_by(run_id=run_id).one()
        job.cost_usd = 3.5
        s.commit()
    orch.record_pr(run_id, _pr(status=PRStatus.MERGED))
    assert _outcome(session_factory, run_id).cost_usd == 3.5


# -- blocked taxonomy ------------------------------------------------------------


def test_closed_unmerged_pr_records_block_taxonomy(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    orch.record_pr(run_id, _pr(status=PRStatus.CLOSED))
    row = _outcome(session_factory, run_id)
    assert row.outcome == "blocked"
    assert row.blocked_reason_category == "pr_closed_unmerged"
    assert row.block_justified is None  # derived on read, never guessed at write


def test_forbidden_paths_block_records_taxonomy(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    orch.record_pr(run_id, _pr(files_changed=["migrations/0042_drop_users.sql"]))
    row = _outcome(session_factory, run_id)
    assert row.outcome == "blocked"
    assert row.blocked_reason_category == "forbidden_paths"


def test_human_stop_records_taxonomy(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    orch.stop(run_id, user="lead@example.com")
    row = _outcome(session_factory, run_id)
    assert row.outcome == "blocked"
    assert row.blocked_reason_category == "human_stopped"


def test_rejection_records_rejected_outcome(session_factory) -> None:
    orch = FoundryOrchestrator(session_factory)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.reject(run_id, user="lead@example.com")
    row = _outcome(session_factory, run_id)
    assert row.outcome == "rejected"
    assert row.repo is None  # never dispatched


def test_agent_failure_records_failed_outcome(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    orch.mark_agent_failed(run_id, reason="agent crashed")
    row = _outcome(session_factory, run_id)
    assert row.outcome == "failed"
    assert row.repo == "customer-web"
    # The agent that ran is captured for per-provider scorecards.
    assert row.provider == "fake"


def test_undispatched_run_records_null_provider(session_factory) -> None:
    orch = FoundryOrchestrator(session_factory)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.reject(run_id, user="lead@example.com")
    row = _outcome(session_factory, run_id)
    assert row.outcome == "rejected"
    assert row.provider is None  # no agent ever ran


def test_latest_job_repo_is_deterministic_with_null_started_at(session_factory) -> None:
    """A NULL started_at must sort last on every backend, not first on SQLite.

    Otherwise "the latest job's repo" picks the unstarted job on SQLite and a
    real job on Postgres - the derived outcome row would differ by backend.
    """
    from datetime import datetime, timezone

    base = datetime(2026, 6, 13, tzinfo=timezone.utc)
    with session_factory() as s:
        run = FoundryRun(
            id="r-null-order",
            linear_issue_id="i-null",
            linear_issue_key="LIN-9",
            status=RunStatus.COMPLETE,
            trigger_type="label",
        )
        s.add(run)
        s.add_all(
            [
                FoundryAgentJob(
                    id="j-early",
                    run_id="r-null-order",
                    provider="fake",
                    repo="early-repo",
                    started_at=base,
                ),
                FoundryAgentJob(
                    id="j-late",
                    run_id="r-null-order",
                    provider="fake",
                    repo="late-repo",
                    started_at=base.replace(hour=5),
                ),
                # Never started; must not be treated as the most recent job.
                FoundryAgentJob(
                    id="j-unstarted",
                    run_id="r-null-order",
                    provider="fake",
                    repo="unstarted-repo",
                    started_at=None,
                ),
            ]
        )
        s.commit()
        outcome = outcomes_module.derive_outcome(s, run)
        assert outcome.repo == "late-repo"
        assert outcome.provider == "fake"


def test_vague_ticket_records_needs_clarification_with_null_repo(session_factory) -> None:
    orch = FoundryOrchestrator(session_factory)
    run_id = orch.intake_and_plan(
        RawTicket(issue_id="i-9", issue_key="OPS-7", title="Fix it"),
        trigger_type="label",
    )
    row = _outcome(session_factory, run_id)
    assert row.outcome == "needs_clarification"
    assert row.repo is None
    assert row.issue_key_prefix == "OPS"
    assert row.time_to_merge_seconds is None


def test_dispatch_policy_block_records_policy_denied(session_factory) -> None:
    """An auth-area ticket is HUMAN_ONLY; dispatch is blocked by policy."""
    orch = FoundryOrchestrator(session_factory)
    run_id = orch.intake_and_plan(
        _ready_ticket(
            issue_id="i-2",
            issue_key="SEC-1",
            title="Change login flow",
            description="Update the auth login flow.\n\nAcceptance Criteria:\n- SSO works",
        ),
        trigger_type="label",
    )
    orch.approve(run_id, user="lead@example.com")
    with pytest.raises(OrchestratorError):
        orch.dispatch_agent(run_id)
    row = _outcome(session_factory, run_id)
    assert row.outcome == "blocked"
    assert row.blocked_reason_category == "policy_denied"


# -- fail-soft -------------------------------------------------------------------


def test_outcome_failure_never_breaks_the_governance_path(
    session_factory, monkeypatch
) -> None:
    orch, run_id = _dispatched_run(session_factory)

    def boom(session, run):
        raise RuntimeError("memory exploded")

    monkeypatch.setattr(outcomes_module, "derive_outcome", boom)
    # record_pr must still complete the run even though memory failed.
    assert orch.record_pr(run_id, _pr(status=PRStatus.MERGED)) is RunStatus.COMPLETE
    with session_factory() as s:
        assert s.get(FoundryRun, run_id).status is RunStatus.COMPLETE
    assert _outcome(session_factory, run_id) is None


def test_record_outcome_is_idempotent(session_factory) -> None:
    from foundry.memory.outcomes import record_outcome

    orch, run_id = _dispatched_run(session_factory)
    orch.record_pr(run_id, _pr(status=PRStatus.MERGED))
    with session_factory() as s:
        run = s.get(FoundryRun, run_id)
        record_outcome(s, run)  # backfill over an existing row
        s.commit()
    assert _outcome(session_factory, run_id).outcome == "merged"
    assert len(_summaries(session_factory, run_id)) == 1
