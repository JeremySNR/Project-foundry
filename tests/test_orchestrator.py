"""End-to-end orchestrator tests using deterministic engines + a fake provider."""

from __future__ import annotations

import pytest

from foundry.agents.manual import InMemoryFakeProvider
from foundry.connectors import InMemoryIssueTracker
from foundry.db import (
    FoundryAgentJob,
    FoundryArtifact,
    FoundryPolicyDecision,
    FoundryRun,
    create_all,
    make_engine,
    make_session_factory,
)
from foundry.db.models import ArtifactType
from foundry.orchestrator import FoundryOrchestrator, OrchestratorError
from foundry.schemas.common import ApprovalRole, PRStatus, RunStatus
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
