"""End-to-end orchestrator tests using deterministic engines + a fake provider."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from foundry.agents.manual import InMemoryFakeProvider
from foundry.connectors import GitHubConnector, InMemoryIssueTracker
from foundry.db import (
    FoundryAgentJob,
    FoundryArtifact,
    FoundryPolicyDecision,
    FoundryRun,
    create_all,
    make_engine,
    make_session_factory,
)
from foundry.db.models import ArtifactType, FoundryRunOutcome
from foundry.orchestrator import FoundryOrchestrator, OrchestratorError
from foundry.schemas.common import (
    ApprovalRole,
    CIStatus,
    PRStatus,
    ReviewStatus,
    RunStatus,
)
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


def _orch(session_factory, **kwargs) -> FoundryOrchestrator:
    return FoundryOrchestrator(session_factory, **kwargs)


def _status(session_factory, run_id: str) -> RunStatus:
    with session_factory() as s:
        return s.get(FoundryRun, run_id).status


def test_ready_ticket_reaches_waiting_approval(session_factory) -> None:
    orch = _orch(session_factory)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL


def test_intake_persists_all_artifacts(session_factory) -> None:
    orch = _orch(session_factory)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    with session_factory() as s:
        types = {
            a.artifact_type
            for a in s.query(FoundryArtifact).filter(FoundryArtifact.run_id == run_id)
        }
        assert ArtifactType.TICKET_SNAPSHOT in types
        assert ArtifactType.TICKET_ANALYSIS in types
        assert ArtifactType.CONTEXT_BUNDLE in types
        assert ArtifactType.RISK_ASSESSMENT in types
        assert ArtifactType.DELIVERY_PLAN in types
        # A policy decision was recorded during intake.
        assert s.query(FoundryPolicyDecision).filter_by(run_id=run_id).count() >= 1


def test_full_happy_path_to_pr_open(session_factory) -> None:
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider)

    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    assert _status(session_factory, run_id) is RunStatus.AGENT_RUNNING

    # Simulate the agent finishing and opening a PR.
    final = provider.run(job.job_id)
    pr = PullRequestState(
        repo="customer-web",
        pr_number=1,
        url=final.pr_url,
        branch=final.branch,
        status=PRStatus.OPEN,
        files_changed=["src/features/favourites/index.ts"],
    )
    result = orch.record_pr(run_id, pr)
    assert result is RunStatus.PR_OPEN

    with session_factory() as s:
        job_row = s.query(FoundryAgentJob).filter_by(run_id=run_id).one()
        assert job_row.pr_url == final.pr_url



def test_forbidden_file_blocks_run(session_factory) -> None:
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)

    pr = PullRequestState(
        repo="customer-web",
        pr_number=2,
        url="https://github.com/example/customer-web/pull/2",
        branch="foundry/lin-123",
        status=PRStatus.OPEN,
        files_changed=["migrations/0002_add_table.sql"],
    )
    assert orch.record_pr(run_id, pr) is RunStatus.BLOCKED


def test_forbidden_file_on_second_page_blocks_run(session_factory) -> None:
    """A forbidden file at position 101+ in a large PR still hard-blocks.

    Regression for the unpaginated file listing: the connector must fetch every
    page of the PR's files so the forbidden-path gate sees the full diff.
    """
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)

    pages = {
        1: [{"filename": f"src/file_{i}.ts"} for i in range(100)],
        2: [{"filename": "migrations/0042_drop_users.sql"}],
    }

    def transport(method: str, path: str):
        return pages[int(path.rsplit("page=", 1)[1])]

    payload = {
        "pull_request": {
            "number": 2,
            "html_url": "https://github.com/example/customer-web/pull/2",
            "head": {"ref": "foundry/lin-123"},
            "state": "open",
            "draft": False,
            "merged": False,
        },
        "repository": {"full_name": "customer-web"},
    }
    state = GitHubConnector(transport=transport).pr_state_from_event(
        "pull_request", payload
    )
    assert state is not None
    assert "migrations/0042_drop_users.sql" in state.files_changed
    assert orch.record_pr(run_id, state) is RunStatus.BLOCKED


def test_oversized_pr_requires_review(session_factory) -> None:
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider, max_files_changed=2)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)

    pr = PullRequestState(
        repo="customer-web",
        pr_number=3,
        url="https://github.com/example/customer-web/pull/3",
        branch="foundry/lin-123",
        status=PRStatus.OPEN,
        files_changed=["a.ts", "b.ts", "c.ts"],
    )
    assert orch.record_pr(run_id, pr) is RunStatus.REVIEW_REQUIRED


def test_vague_ticket_needs_clarification_and_cannot_dispatch(session_factory) -> None:
    orch = _orch(session_factory)
    run_id = orch.intake_and_plan(
        RawTicket(issue_id="i", issue_key="LIN-9", title="Make it nicer"),
        trigger_type="comment_command",
    )
    assert _status(session_factory, run_id) is RunStatus.NEEDS_CLARIFICATION
    # Cannot approve a run that is not awaiting approval.
    with pytest.raises(OrchestratorError):
        orch.approve(run_id, user="lead@example.com")


def test_auth_change_is_human_only_and_blocks_dispatch(session_factory) -> None:
    orch = _orch(session_factory, provider=InMemoryFakeProvider())
    ticket = _ready_ticket(
        title="Rotate auth login session tokens",
        description="Acceptance Criteria:\n- auth tokens rotate\n- login still works",
    )
    run_id = orch.intake_and_plan(ticket, trigger_type="label")
    # High-risk auth work is still planned and awaits approval...
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL
    orch.approve(run_id, user="lead@example.com", granted_roles={ApprovalRole.ENGINEERING})
    # ...but the policy gate keeps auth changes human-only, so dispatch is blocked.
    with pytest.raises(OrchestratorError):
        orch.dispatch_agent(run_id)
    assert _status(session_factory, run_id) is RunStatus.BLOCKED


def test_dispatch_requires_approval_first(session_factory) -> None:
    orch = _orch(session_factory, provider=InMemoryFakeProvider())
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    with pytest.raises(OrchestratorError):
        orch.dispatch_agent(run_id)


def test_tracker_receives_comment_and_state_on_intake(session_factory) -> None:
    tracker = InMemoryIssueTracker()
    orch = _orch(session_factory, issue_tracker=tracker)
    orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    assert len(tracker.comments["i-1"]) == 1
    assert "Foundry analysis complete" in tracker.comments["i-1"][0]
    assert tracker.states["i-1"] == "Foundry: Waiting Approval"


def test_tracker_state_follows_run_through_to_pr(session_factory) -> None:
    tracker = InMemoryIssueTracker()
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider, issue_tracker=tracker)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    assert tracker.states["i-1"] == "Foundry: Approved"
    job = orch.dispatch_agent(run_id)
    assert tracker.states["i-1"] == "Foundry: Agent Running"
    provider.run(job.job_id)
    orch.record_pr(
        run_id,
        PullRequestState(
            repo="customer-web",
            pr_number=1,
            url="https://github.com/example/customer-web/pull/1",
            branch="foundry/lin-123",
            status=PRStatus.OPEN,
            files_changed=["src/x.ts"],
        ),
    )
    assert tracker.states["i-1"] == "Foundry: PR Open"


def _dispatched_run(session_factory, orch_kwargs=None) -> tuple:
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider, **(orch_kwargs or {}))
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


# -- PR lifecycle: guardrails re-run on every push ------------------------------


def test_pr_update_after_open_is_recorded_not_rejected(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN
    # A second (synchronize) event must not raise.
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN


def test_forbidden_file_pushed_after_open_blocks_run(session_factory) -> None:
    """An agent cannot open a clean PR and sneak forbidden files in later."""
    orch, run_id = _dispatched_run(session_factory)
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN
    late_push = _pr(files_changed=["src/ok.ts", "migrations/0042_drop_users.sql"])
    assert orch.record_pr(run_id, late_push) is RunStatus.BLOCKED
    # Blocked is sticky: further events are refused, a human must intervene.
    with pytest.raises(OrchestratorError):
        orch.record_pr(run_id, _pr())


def test_eventless_update_does_not_weaken_review_required(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    big = _pr(files_changed=[f"src/f{i}.ts" for i in range(20)])
    assert orch.record_pr(run_id, big) is RunStatus.REVIEW_REQUIRED
    # A review/CI event carries no file list; the file-based decision stands.
    no_files = _pr(files_changed=[])
    assert orch.record_pr(run_id, no_files) is RunStatus.REVIEW_REQUIRED


def test_pr_shrinking_back_under_limit_recovers(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    big = _pr(files_changed=[f"src/f{i}.ts" for i in range(20)])
    assert orch.record_pr(run_id, big) is RunStatus.REVIEW_REQUIRED
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN


def test_merged_pr_completes_run_and_job(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    orch.record_pr(run_id, _pr())
    assert orch.record_pr(run_id, _pr(status=PRStatus.MERGED)) is RunStatus.COMPLETE
    with session_factory() as s:
        job_row = s.query(FoundryAgentJob).filter_by(run_id=run_id).one()
        assert job_row.completed_at is not None


def test_closed_unmerged_pr_blocks_run(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    orch.record_pr(run_id, _pr())
    assert orch.record_pr(run_id, _pr(status=PRStatus.CLOSED)) is RunStatus.BLOCKED


# -- diff-aware risk -------------------------------------------------------------


def test_diff_touching_unflagged_sensitive_area_escalates(session_factory) -> None:
    """The ticket said 'favourites'; the diff touched auth. Escalate."""
    orch, run_id = _dispatched_run(session_factory)
    sneaky = _pr(files_changed=["src/auth/session_handler.ts"])
    assert orch.record_pr(run_id, sneaky) is RunStatus.REVIEW_REQUIRED


def test_diff_in_anticipated_sensitive_area_does_not_escalate(session_factory) -> None:
    """An area the upfront risk pass flagged was already approved by a human."""
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider)
    ticket = _ready_ticket(
        title="Tune the helm chart resource limits",
        description=READY_DESC + "\nAdjust the helm chart for the favourites service.",
    )
    run_id = orch.intake_and_plan(ticket, trigger_type="label")
    # Infrastructure risk (medium) requires an engineering-role approval.
    orch.approve(
        run_id, user="lead@example.com", granted_roles={ApprovalRole.ENGINEERING}
    )
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)
    # The diff touches infrastructure paths - anticipated, approved, no escalation.
    pr = _pr(files_changed=["deploy/helm/values.yaml"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_custom_sensitive_globs_are_honoured(session_factory) -> None:
    orch, run_id = _dispatched_run(
        session_factory,
        orch_kwargs={"sensitive_path_globs": {"payments": ("**/money/**",)}},
    )
    pr = _pr(files_changed=["src/money/charge.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.REVIEW_REQUIRED


# -- PR correlation ---------------------------------------------------------------


def test_correlate_pr_by_exact_branch(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    assert orch.correlate_pr(_pr()) == run_id


def test_correlate_pr_by_issue_key_in_branch(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    pr = _pr(branch="cursor/lin-123-something-cursor-chose")
    assert orch.correlate_pr(pr) == run_id


def test_correlate_pr_by_issue_key_in_title(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    pr = _pr(branch="opaque-name", title="LIN-123: add favourites")
    assert orch.correlate_pr(pr) == run_id


def test_correlate_pr_no_match_returns_none(session_factory) -> None:
    orch, _run_id = _dispatched_run(session_factory)
    pr = _pr(branch="other-branch", title="OTHER-9 unrelated")
    assert orch.correlate_pr(pr) is None


# -- governed remediation loop ----------------------------------------------------


def _dispatched_with_provider(session_factory, **orch_kwargs):
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider, **orch_kwargs)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)
    return orch, run_id, provider


def _job_count(session_factory, run_id: str) -> int:
    with session_factory() as s:
        return s.query(FoundryAgentJob).filter_by(run_id=run_id).count()


def test_ci_failure_redispatches_agent(session_factory) -> None:
    orch, run_id, _provider = _dispatched_with_provider(session_factory)
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN

    failing = _pr(files_changed=[], ci_status=CIStatus.FAILING, summary="pytest: 2 failed")
    assert orch.record_pr(run_id, failing) is RunStatus.AGENT_RUNNING
    # A second agent job was dispatched for the remediation.
    assert _job_count(session_factory, run_id) == 2


def test_remediation_job_targets_same_branch_with_context(session_factory) -> None:
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)
    orch.record_pr(run_id, _pr(branch="cursor/lin-123-own-branch"))

    failing = _pr(
        branch="cursor/lin-123-own-branch",
        ci_status=CIStatus.FAILING,
        summary="- unit tests: 2 failed",
    )
    orch.record_pr(run_id, failing)
    remediation_input = list(provider._inputs.values())[-1]
    # Same branch the agent actually used, not a fresh Foundry-named one.
    assert remediation_input.branch_name == "cursor/lin-123-own-branch"
    assert "REMEDIATION REQUEST" in remediation_input.agent_instructions
    assert "- unit tests: 2 failed" in remediation_input.agent_instructions


def test_remediation_cap_parks_run_for_humans(session_factory) -> None:
    tracker = InMemoryIssueTracker()
    provider = InMemoryFakeProvider()
    orch = _orch(
        session_factory,
        provider=provider,
        issue_tracker=tracker,
        max_agent_retries=1,
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)
    orch.record_pr(run_id, _pr())

    failing = _pr(ci_status=CIStatus.FAILING)
    # Attempt 1: within the cap, re-dispatches.
    assert orch.record_pr(run_id, failing) is RunStatus.AGENT_RUNNING
    # The agent pushes again, PR re-opens, CI fails again.
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN
    # Attempt 2: over the cap -> denied by policy, parked for review.
    assert orch.record_pr(run_id, failing) is RunStatus.REVIEW_REQUIRED
    assert _job_count(session_factory, run_id) == 2  # no third dispatch
    # And a human-readable comment landed on the issue.
    assert any("could not remediate" in c for c in tracker.comments["i-1"])


def test_changes_requested_review_triggers_remediation(session_factory) -> None:
    orch, run_id, _provider = _dispatched_with_provider(session_factory)
    orch.record_pr(run_id, _pr())
    review = _pr(files_changed=[], review_status=ReviewStatus.CHANGES_REQUESTED)
    assert orch.record_pr(run_id, review) is RunStatus.AGENT_RUNNING


def test_retry_on_config_disables_remediation(session_factory) -> None:
    orch, run_id, _provider = _dispatched_with_provider(
        session_factory, retry_on=()
    )
    orch.record_pr(run_id, _pr())
    failing = _pr(ci_status=CIStatus.FAILING)
    # Remediation disabled: CI failure is recorded but the run stays PR_OPEN.
    assert orch.record_pr(run_id, failing) is RunStatus.PR_OPEN
    assert _job_count(session_factory, run_id) == 1


def test_budget_cap_denies_remediation(session_factory) -> None:
    tracker = InMemoryIssueTracker()
    orch, run_id, _provider = _dispatched_with_provider(
        session_factory, issue_tracker=tracker, max_cost_per_run=5.0
    )
    orch.record_pr(run_id, _pr())
    # The first job already burned through the budget.
    with session_factory() as s:
        job = s.query(FoundryAgentJob).filter_by(run_id=run_id).one()
        job.cost_usd = 6.0
        s.commit()

    failing = _pr(ci_status=CIStatus.FAILING)
    assert orch.record_pr(run_id, failing) is RunStatus.REVIEW_REQUIRED
    assert _job_count(session_factory, run_id) == 1  # no re-dispatch
    assert any("budget cap" in c for c in tracker.comments["i-1"])


def test_remediation_allowed_when_under_budget(session_factory) -> None:
    orch, run_id, _provider = _dispatched_with_provider(
        session_factory, max_cost_per_run=50.0
    )
    orch.record_pr(run_id, _pr())
    with session_factory() as s:
        job = s.query(FoundryAgentJob).filter_by(run_id=run_id).one()
        job.cost_usd = 6.0
        s.commit()
    failing = _pr(ci_status=CIStatus.FAILING)
    assert orch.record_pr(run_id, failing) is RunStatus.AGENT_RUNNING


def test_no_remediation_for_forbidden_path_block(session_factory) -> None:
    """BLOCKED is sticky; remediation never resurrects a forbidden-path block."""
    orch, run_id, _provider = _dispatched_with_provider(session_factory)
    bad = _pr(
        files_changed=["migrations/0001_drop.sql"], ci_status=CIStatus.FAILING
    )
    assert orch.record_pr(run_id, bad) is RunStatus.BLOCKED
    assert _job_count(session_factory, run_id) == 1


# -- one active run per issue ------------------------------------------------------


def test_second_intake_for_active_issue_is_refused(session_factory) -> None:
    orch = _orch(session_factory)
    orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    with pytest.raises(OrchestratorError):
        orch.intake_and_plan(_ready_ticket(), trigger_type="label")


def test_rejected_issue_can_be_reanalysed(session_factory) -> None:
    orch = _orch(session_factory)
    first = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.reject(first, user="lead@example.com")
    second = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    assert second != first


def test_db_refuses_second_active_run_for_issue(session_factory) -> None:
    """The schema itself is the arbiter, not just the intake pre-check."""
    with session_factory() as s:
        s.add(
            FoundryRun(
                id="run-a",
                linear_issue_id="i-1",
                linear_issue_key="LIN-123",
                status=RunStatus.WAITING_APPROVAL,
                trigger_type="label",
            )
        )
        s.commit()
    with session_factory() as s:
        s.add(
            FoundryRun(
                id="run-b",
                linear_issue_id="i-1",
                linear_issue_key="LIN-123",
                status=RunStatus.ANALYSING,
                trigger_type="label",
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_db_allows_new_run_alongside_finished_ones(session_factory) -> None:
    """The unique index is partial: terminal runs never pin their issue."""
    with session_factory() as s:
        for run_id, status in [
            ("run-a", RunStatus.REJECTED),
            ("run-b", RunStatus.NEEDS_CLARIFICATION),
            ("run-c", RunStatus.NEEDS_CLARIFICATION),
            ("run-d", RunStatus.WAITING_APPROVAL),
        ]:
            s.add(
                FoundryRun(
                    id=run_id,
                    linear_issue_id="i-1",
                    linear_issue_key="LIN-123",
                    status=status,
                    trigger_type="label",
                )
            )
            s.commit()


def test_intake_race_attaches_to_surviving_run(session_factory, monkeypatch) -> None:
    """Two webhook deliveries race past the pre-check; exactly one run survives.

    The pre-check is simulated as stale for both deliveries (each read before
    the other committed, as in a real multi-worker race); the loser must fall
    back to the surviving run instead of creating a duplicate or erroring.
    """
    orch = _orch(session_factory)
    real_lookup = FoundryOrchestrator.find_active_run_id_for_issue
    lookups = {"count": 0}

    def stale_then_real(self, issue_id):
        lookups["count"] += 1
        if lookups["count"] <= 2:  # the two deliveries' pre-checks
            return None
        return real_lookup(self, issue_id)  # the loser's recovery lookup

    monkeypatch.setattr(
        FoundryOrchestrator, "find_active_run_id_for_issue", stale_then_real
    )
    first = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    second = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    assert second == first
    with session_factory() as s:
        assert s.query(FoundryRun).count() == 1
    assert _status(session_factory, second) is RunStatus.WAITING_APPROVAL


def test_cursor_via_linear_delegation_end_to_end(session_factory) -> None:
    # Foundry governs, then hands the approved work to Cursor via a Linear comment.
    from foundry.agents import CursorViaLinearProvider

    tracker = InMemoryIssueTracker()
    orch = _orch(
        session_factory,
        provider=CursorViaLinearProvider(tracker),
        issue_tracker=tracker,
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    orch.dispatch_agent(run_id)
    # An @Cursor delegation comment was posted in addition to the analysis comment.
    assert any(c.startswith("@Cursor") for c in tracker.comments["i-1"])
    assert _status(session_factory, run_id) is RunStatus.AGENT_RUNNING


def test_injected_diff_risk_classifier_escalates_with_evidence(session_factory) -> None:
    """The diff-stage seam: a classifier that flags beyond the globs escalates
    the run and its cited evidence lands in the RISK_ESCALATED audit event."""
    import json

    from foundry.db.models import AuditEventType, FoundryAuditEvent
    from foundry.schemas.risk import DiffRiskFindings, RiskEvidence

    class FlaggingDiffClassifier:
        def classify_diff(self, files, ticket=None):
            return DiffRiskFindings(
                areas={"auth": sorted(files)},
                evidence=[
                    RiskEvidence(
                        area="auth",
                        detail="touches session issuance in src/tokens/issue.ts",
                        source="llm",
                    )
                ],
            )

    orch, run_id = _dispatched_run(
        session_factory,
        orch_kwargs={"diff_risk_classifier": FlaggingDiffClassifier()},
    )
    pr = _pr(files_changed=["src/tokens/issue.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.REVIEW_REQUIRED
    with session_factory() as s:
        events = [
            e
            for e in s.query(FoundryAuditEvent).filter_by(run_id=run_id)
            if e.event_type is AuditEventType.RISK_ESCALATED
        ]
        assert len(events) == 1
        meta = json.loads(events[0].metadata_json)
        assert meta["areas"] == {"auth": ["src/tokens/issue.ts"]}
        assert meta["evidence"] == [
            {
                "area": "auth",
                "detail": "touches session issuance in src/tokens/issue.ts",
                "source": "llm",
            }
        ]


# -- terminal-state guards: finished history is immutable -----------------------


def _merged_run(session_factory) -> tuple:
    orch, run_id = _dispatched_run(session_factory)
    orch.record_pr(run_id, _pr(status=PRStatus.MERGED))
    assert _status(session_factory, run_id) is RunStatus.COMPLETE
    return orch, run_id


def _outcome_value(session_factory, run_id: str) -> str:
    with session_factory() as s:
        return s.get(FoundryRunOutcome, run_id).outcome


def test_stop_on_complete_run_is_refused(session_factory) -> None:
    orch, run_id = _merged_run(session_factory)
    with pytest.raises(OrchestratorError, match="already terminal"):
        orch.stop(run_id, user="lead@example.com")
    assert _status(session_factory, run_id) is RunStatus.COMPLETE
    assert _outcome_value(session_factory, run_id) == "merged"


def test_reject_on_complete_run_is_refused(session_factory) -> None:
    orch, run_id = _merged_run(session_factory)
    with pytest.raises(OrchestratorError, match="already terminal"):
        orch.reject(run_id, user="lead@example.com")
    assert _status(session_factory, run_id) is RunStatus.COMPLETE
    assert _outcome_value(session_factory, run_id) == "merged"


def test_mark_agent_failed_on_complete_run_is_refused(session_factory) -> None:
    orch, run_id = _merged_run(session_factory)
    with pytest.raises(OrchestratorError, match="already terminal"):
        orch.mark_agent_failed(run_id, reason="late crash report")
    assert _status(session_factory, run_id) is RunStatus.COMPLETE
    assert _outcome_value(session_factory, run_id) == "merged"


def test_stop_on_already_stopped_run_is_refused(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    orch.stop(run_id, user="lead@example.com")
    with pytest.raises(OrchestratorError, match="already terminal"):
        orch.stop(run_id, user="lead@example.com")
    assert _status(session_factory, run_id) is RunStatus.BLOCKED
    assert _outcome_value(session_factory, run_id) == "blocked"


def test_mark_agent_failed_on_rejected_run_is_refused(session_factory) -> None:
    orch = _orch(session_factory)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.reject(run_id, user="lead@example.com")
    with pytest.raises(OrchestratorError, match="already terminal"):
        orch.mark_agent_failed(run_id)
    assert _status(session_factory, run_id) is RunStatus.REJECTED
    assert _outcome_value(session_factory, run_id) == "rejected"


def test_stop_on_active_run_still_blocks_it(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    orch.stop(run_id, user="lead@example.com")
    assert _status(session_factory, run_id) is RunStatus.BLOCKED
    assert _outcome_value(session_factory, run_id) == "blocked"


def test_mark_agent_failed_on_running_run_still_fails_it(session_factory) -> None:
    orch, run_id = _dispatched_run(session_factory)
    orch.mark_agent_failed(run_id, reason="agent crashed")
    assert _status(session_factory, run_id) is RunStatus.EXECUTION_FAILED
    assert _outcome_value(session_factory, run_id) == "failed"
