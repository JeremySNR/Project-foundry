"""End-to-end orchestrator tests using deterministic engines + a fake provider."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from foundry.agents.manual import InMemoryFakeProvider
from foundry.connectors import (
    GitHubConnector,
    InMemoryIssueTracker,
    InMemoryNotifier,
)
from foundry.db import (
    FoundryAgentJob,
    FoundryArtifact,
    FoundryPolicyDecision,
    FoundryRun,
    create_all,
    make_engine,
    make_session_factory,
)
from foundry.db.models import AgentJobStatus, ArtifactType, FoundryRunOutcome
from foundry.engines.risk import CustomRiskCategory
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


def test_intake_records_achievable_agent_mode_and_pre_approval_denial(
    session_factory,
) -> None:
    """The gate denies dispatch with no approval recorded yet (issue #18), but a
    ready low-risk run must still (a) route to WAITING_APPROVAL and (b) advertise
    the agent mode *achievable* once approved (draft_pr) rather than the transient
    human-only of its unapproved state. The recorded intake decision is the
    honest pre-approval result: denied for the missing approval."""
    from foundry.schemas.common import AgentMode

    orch = _orch(session_factory)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")

    with session_factory() as s:
        run = s.get(FoundryRun, run_id)
        assert run.status is RunStatus.WAITING_APPROVAL
        # Achievable-once-approved mode, not the unapproved human-only.
        assert run.agent_mode is AgentMode.DRAFT_PR
        decision = (
            s.query(FoundryPolicyDecision)
            .filter_by(run_id=run_id)
            .one()
        )
        # The recorded gate decision is honest about the pre-approval state.
        assert decision.allowed is False
        assert "requires at least one recorded human approval" in decision.reason


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


def test_nested_forbidden_migrations_path_blocks_run(session_factory) -> None:
    """A migrations dir nested under a service path still hard-blocks.

    Regression for root-anchored forbidden globs: ``migrations/**`` only matched
    a top-level dir, so a nested ``services/api/migrations/...`` got the softer
    sensitive-area escalation (REVIEW_REQUIRED) instead of the sticky BLOCK the
    forbidden list promises.
    """
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
        files_changed=["services/api/migrations/0001_init.py"],
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


def test_oversized_pr_records_risk_escalated_event(session_factory) -> None:
    """The too-many-files escalation is no longer a silent status flip: it
    records a RISK_ESCALATED event so the trail says why the run went to review
    and the approval-queue clock can date the wait from this transition."""
    from foundry.db.models import AuditEventType

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

    metas = _audit_meta(session_factory, run_id, AuditEventType.RISK_ESCALATED)
    assert any(m.get("category") == "diff_too_large" for m in metas)
    meta = next(m for m in metas if m.get("category") == "diff_too_large")
    assert meta["files_changed"] == 3
    assert meta["max_files_changed"] == 2


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


def test_permanently_blocked_ticket_parks_blocked_not_waiting_approval(
    session_factory,
) -> None:
    """A DB-migration ticket is denied by policy no matter who approves, so it
    parks at BLOCKED at intake instead of inviting a futile approval that
    dispatch would only convert to BLOCKED.
    """
    import json

    from foundry.db.models import AuditEventType, FoundryAuditEvent

    orch = _orch(session_factory)
    ticket = _ready_ticket(
        title="Migrate the users table",
        description=(
            "Acceptance Criteria:\n"
            "- alter table users add column nickname\n"
            "- backfill existing rows\n"
        ),
    )
    run_id = orch.intake_and_plan(ticket, trigger_type="label")
    assert _status(session_factory, run_id) is RunStatus.BLOCKED
    # A permanently-blocked run cannot be approved.
    with pytest.raises(OrchestratorError):
        orch.approve(run_id, user="lead@example.com")

    # The block is recorded as a policy denial (not "unroutable"), with the
    # decision attached so the trail shows why approval was never offered.
    with session_factory() as s:
        events = (
            s.query(FoundryAuditEvent)
            .filter_by(run_id=run_id, event_type=AuditEventType.RUN_BLOCKED)
            .all()
        )
    assert len(events) == 1
    meta = json.loads(events[0].metadata_json or "{}")
    assert meta["category"] == "policy_denied"
    assert events[0].output_hash is not None


def test_dispatch_requires_approval_first(session_factory) -> None:
    orch = _orch(session_factory, provider=InMemoryFakeProvider())
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    with pytest.raises(OrchestratorError):
        orch.dispatch_agent(run_id)


def _infra_ticket(**overrides) -> RawTicket:
    """Infrastructure work: requires ENGINEERING approval but is medium-risk, so
    (unlike auth) it is dispatchable as a draft PR once approved with the role."""
    base = dict(
        issue_id="i-infra",
        issue_key="LIN-INF",
        title="Update the terraform deployment config",
        description=(
            "Acceptance Criteria:\n"
            "- terraform plan runs clean\n"
            "- the deployment config applies\n"
        ),
        known_repositories=["customer-web"],
    )
    base.update(overrides)
    return RawTicket(**base)


def _has_artifact(session_factory, run_id, artifact_type) -> bool:
    with session_factory() as s:
        return (
            s.query(FoundryArtifact)
            .filter_by(run_id=run_id, artifact_type=artifact_type)
            .count()
            > 0
        )


def test_approve_without_required_role_is_refused_and_records_nothing(
    session_factory,
) -> None:
    """Issue #18: an approver lacking a role this run requires is refused *before*
    anything is written - no void APPROVAL_GRANTED, no flip to APPROVED. Previously
    the approval was recorded and only blocked at dispatch (approve->blocked
    whiplash with a misleading audit trail)."""
    from foundry.db.models import AuditEventType

    orch = _orch(session_factory, provider=InMemoryFakeProvider())
    run_id = orch.intake_and_plan(_infra_ticket(), trigger_type="label")
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL

    # Infra work requires ENGINEERING; approving with no roles must be refused.
    with pytest.raises(OrchestratorError, match="approval refused"):
        orch.approve(run_id, user="lead@example.com")

    # The run is untouched: still awaiting approval, no approver recorded.
    with session_factory() as s:
        run = s.get(FoundryRun, run_id)
        assert run.status is RunStatus.WAITING_APPROVAL
        assert run.approved_by is None
    # No phantom approval artifact or audit event on the trail.
    assert not _has_artifact(session_factory, run_id, ArtifactType.APPROVAL_RECORD)
    assert (
        _events_of_type(session_factory, run_id, AuditEventType.APPROVAL_GRANTED) == []
    )


def test_approve_with_partial_roles_is_refused(session_factory) -> None:
    """A run needing SECURITY (customer PII) is not unlocked by an ENGINEERING-only
    sign-off; the refusal names the missing role."""
    orch = _orch(session_factory, provider=InMemoryFakeProvider())
    ticket = _ready_ticket(
        title="Export customer data with GDPR fields",
        description=(
            "Acceptance Criteria:\n"
            "- personal data is exportable\n"
            "- gdpr fields included\n"
        ),
    )
    run_id = orch.intake_and_plan(ticket, trigger_type="label")
    with pytest.raises(OrchestratorError, match="security"):
        orch.approve(
            run_id, user="lead@example.com", granted_roles={ApprovalRole.ENGINEERING}
        )
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL


def test_approve_with_required_role_records_and_dispatches(session_factory) -> None:
    """The same infra run, approved *with* the required ENGINEERING role, records
    the approval and dispatches (medium risk -> draft PR, not human-only)."""
    orch = _orch(session_factory, provider=InMemoryFakeProvider())
    run_id = orch.intake_and_plan(_infra_ticket(), trigger_type="label")
    orch.approve(
        run_id, user="lead@example.com", granted_roles={ApprovalRole.ENGINEERING}
    )
    assert _status(session_factory, run_id) is RunStatus.APPROVED
    assert _has_artifact(session_factory, run_id, ArtifactType.APPROVAL_RECORD)
    orch.dispatch_agent(run_id)
    assert _status(session_factory, run_id) is RunStatus.AGENT_RUNNING


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


# -- diff-aware per-path approval roles (issue #31/#35) --------------------------


def test_diff_touching_path_required_role_escalates(session_factory) -> None:
    """A PR touching a path the operator scoped to a role no approver holds
    escalates to human review - per-path policy for monorepos.

    ``**/legal/**`` is deliberately *not* one of the built-in sensitive-area
    globs, so the escalation here can only come from the per-path rule, not the
    existing unflagged-sensitive-area check.
    """
    orch, run_id = _dispatched_run(
        session_factory,
        orch_kwargs={"path_required_roles": {"**/legal/**": ("security",)}},
    )
    # The favourites ticket derived no security role, so the single approver
    # signed with none; a diff into the legal subtree now needs a security
    # sign-off the run never obtained.
    pr = _pr(files_changed=["src/legal/terms.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.REVIEW_REQUIRED


def test_diff_outside_path_rule_does_not_escalate(session_factory) -> None:
    """A path rule only escalates the paths it actually matches."""
    orch, run_id = _dispatched_run(
        session_factory,
        orch_kwargs={"path_required_roles": {"**/legal/**": ("security",)}},
    )
    pr = _pr(files_changed=["src/features/favourites/index.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_path_required_role_already_granted_does_not_escalate(session_factory) -> None:
    """If the run's approver already signed with the path's required role (e.g.
    risk forced it upfront), touching the path does not re-escalate - mirroring
    how an *anticipated* sensitive area is not flagged again."""
    provider = InMemoryFakeProvider()
    orch = _orch(
        session_factory,
        provider=provider,
        path_required_roles={"**/helm/**": ("engineering",)},
    )
    # An infrastructure ticket derives the engineering role, so the approver
    # signs with it; the helm path is then already covered.
    ticket = _ready_ticket(
        title="Tune the helm chart resource limits",
        description=READY_DESC + "\nAdjust the helm chart for the favourites service.",
    )
    run_id = orch.intake_and_plan(ticket, trigger_type="label")
    orch.approve(
        run_id, user="lead@example.com", granted_roles={ApprovalRole.ENGINEERING}
    )
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)
    pr = _pr(files_changed=["deploy/helm/values.yaml"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_no_path_rules_is_unchanged(session_factory) -> None:
    """With no path rules configured (the default) a legal-subtree diff is just
    an ordinary open PR - the feature is byte-for-byte off by default."""
    orch, run_id = _dispatched_run(session_factory)
    pr = _pr(files_changed=["src/legal/terms.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_path_required_role_escalation_is_audited(session_factory) -> None:
    """The escalation writes a risk.escalated event citing the path and role."""
    import json

    orch, run_id = _dispatched_run(
        session_factory,
        orch_kwargs={"path_required_roles": {"**/legal/**": ("security",)}},
    )
    orch.record_pr(run_id, _pr(files_changed=["src/legal/terms.ts"]))
    with session_factory() as s:
        from foundry.db import FoundryAuditEvent

        events = (
            s.query(FoundryAuditEvent)
            .filter(FoundryAuditEvent.run_id == run_id)
            .all()
        )
    escalations = [
        json.loads(e.metadata_json or "{}")
        for e in events
        if e.event_type.value == "risk.escalated"
    ]
    path_escalations = [
        m for m in escalations if m.get("category") == "path_required_roles"
    ]
    assert len(path_escalations) == 1
    meta = path_escalations[0]
    assert meta["required_roles"] == ["security"]
    assert meta["paths"]["security"] == ["src/legal/terms.ts"]


def test_forbidden_path_beats_path_required_role(session_factory) -> None:
    """A forbidden path is a sticky BLOCK and wins over the softer per-path
    role escalation when a diff trips both."""
    orch, run_id = _dispatched_run(
        session_factory,
        orch_kwargs={"path_required_roles": {"migrations/**": ("security",)}},
    )
    pr = _pr(files_changed=["migrations/0003_add_index.sql"])
    assert orch.record_pr(run_id, pr) is RunStatus.BLOCKED


# -- operator-defined custom risk categories (issue #155) ------------------------


def test_custom_category_ticket_keyword_demands_role(session_factory) -> None:
    """A custom category whose ticket-text keyword fires demands its approval
    role at the gate: an approver lacking it is refused, and granting it lets the
    run advance and dispatch."""
    provider = InMemoryFakeProvider()
    orch = _orch(
        session_factory,
        provider=provider,
        custom_risk_categories=[
            CustomRiskCategory(
                name="gdpr_subject_data",
                keywords=("favourite",),
                required_roles=("security",),
            )
        ],
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    # The custom category fired (the ticket is about favourites) and demands a
    # security sign-off the favourites ticket would not otherwise require.
    with pytest.raises(OrchestratorError, match="security"):
        orch.approve(run_id, user="lead@example.com")
    orch.approve(
        run_id, user="lead@example.com", granted_roles={ApprovalRole.SECURITY}
    )
    orch.dispatch_agent(run_id)
    assert _status(session_factory, run_id) is RunStatus.AGENT_RUNNING


def test_custom_category_ticket_hit_recorded_in_risk_assessment(
    session_factory,
) -> None:
    """A fired ticket-text category lands cited evidence + the demanded roles on
    the RiskAssessment artifact (auditable), without lowering risk."""
    orch = _orch(
        session_factory,
        custom_risk_categories=[
            CustomRiskCategory(
                name="gdpr_subject_data",
                keywords=("favourite",),
                required_roles=("security",),
            )
        ],
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    with session_factory() as s:
        from foundry.db.models import ArtifactType as _AT

        orch_risk = orch._load(s, run_id, _AT.RISK_ASSESSMENT)
    assert ApprovalRole.SECURITY in orch_risk.custom_required_approvals
    assert any(e.area == "gdpr_subject_data" for e in orch_risk.evidence)
    assert any("gdpr_subject_data" in r for r in orch_risk.risk_reasons)


def test_custom_category_diff_glob_escalates(session_factory) -> None:
    """A diff touching a custom category's path glob whose role no approver
    granted escalates the run to human review (the diff-path twin of the
    per-path roles)."""
    orch, run_id = _dispatched_run(
        session_factory,
        orch_kwargs={
            "custom_risk_categories": [
                CustomRiskCategory(
                    name="crypto_keys",
                    path_globs=("**/crypto/**",),
                    required_roles=("security",),
                )
            ]
        },
    )
    pr = _pr(files_changed=["src/crypto/signing.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.REVIEW_REQUIRED


def test_custom_category_diff_escalation_is_audited(session_factory) -> None:
    """The diff escalation writes a risk.escalated event naming the category."""
    import json

    orch, run_id = _dispatched_run(
        session_factory,
        orch_kwargs={
            "custom_risk_categories": [
                CustomRiskCategory(
                    name="crypto_keys",
                    path_globs=("**/crypto/**",),
                    required_roles=("security",),
                )
            ]
        },
    )
    orch.record_pr(run_id, _pr(files_changed=["src/crypto/signing.ts"]))
    with session_factory() as s:
        from foundry.db import FoundryAuditEvent

        events = (
            s.query(FoundryAuditEvent)
            .filter(FoundryAuditEvent.run_id == run_id)
            .all()
        )
    escalations = [
        json.loads(e.metadata_json or "{}")
        for e in events
        if e.event_type.value == "risk.escalated"
    ]
    custom = [m for m in escalations if m.get("category") == "custom_risk_category"]
    assert len(custom) == 1
    assert custom[0]["categories"] == ["crypto_keys"]
    assert custom[0]["required_roles"] == ["security"]
    assert custom[0]["paths"]["security"] == ["src/crypto/signing.ts"]


def test_custom_category_diff_role_already_granted_does_not_escalate(
    session_factory,
) -> None:
    """If the ticket-text trigger already forced the human to sign with the
    category's role, touching its diff path does not re-escalate - mirroring the
    anticipated-sensitive-area and per-path-role behaviour."""
    provider = InMemoryFakeProvider()
    orch = _orch(
        session_factory,
        provider=provider,
        custom_risk_categories=[
            CustomRiskCategory(
                name="gdpr_subject_data",
                keywords=("favourite",),
                path_globs=("**/gdpr/**",),
                required_roles=("security",),
            )
        ],
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(
        run_id, user="lead@example.com", granted_roles={ApprovalRole.SECURITY}
    )
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)
    pr = _pr(files_changed=["src/gdpr/export.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_custom_category_is_escalate_only(session_factory) -> None:
    """A custom category only ever *adds* a required role on top of the built-in
    risk-derived ones; it never drops a built-in area's role or lowers risk."""
    orch = _orch(
        session_factory,
        custom_risk_categories=[
            CustomRiskCategory(
                name="gdpr_subject_data",
                keywords=("favourite",),
                required_roles=("product",),
            )
        ],
    )
    # An infrastructure ticket derives the built-in engineering role; the custom
    # category adds product on top.
    ticket = _ready_ticket(
        title="Tune the helm chart resource limits",
        description=READY_DESC + "\nAdjust the helm chart for the favourites service.",
    )
    run_id = orch.intake_and_plan(ticket, trigger_type="label")
    with session_factory() as s:
        from foundry.db.models import ArtifactType as _AT

        risk = orch._load(s, run_id, _AT.RISK_ASSESSMENT)
    # The built-in area is still flagged and the risk level is not lowered.
    assert risk.sensitive_areas.infrastructure is True
    assert risk.overall_risk.value == "medium"
    # The built-in engineering role is *not* dropped: an approver with only the
    # custom product role is refused for the still-required engineering role.
    with pytest.raises(OrchestratorError, match="engineering"):
        orch.approve(
            run_id, user="a@example.com", granted_roles={ApprovalRole.PRODUCT}
        )
    # Both the built-in engineering role and the custom product role together
    # release the run.
    orch.approve(
        run_id,
        user="a@example.com",
        granted_roles={ApprovalRole.ENGINEERING, ApprovalRole.PRODUCT},
    )
    assert _status(session_factory, run_id) is RunStatus.APPROVED


def test_no_custom_categories_is_unchanged(session_factory) -> None:
    """With no categories configured (the default) a diff into what *would* be a
    category path is just an ordinary open PR - the feature is off by default."""
    orch, run_id = _dispatched_run(session_factory)
    pr = _pr(files_changed=["src/crypto/signing.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


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


def test_find_run_id_for_branch_ignores_terminal_runs(session_factory) -> None:
    """A branch whose run has gone terminal is not matched, so a late PR webhook
    cannot revive it (correlate_pr's documented PR-observable-only contract).
    """
    branch = "foundry/lin-123-add-customer-favourites"
    orch, run_id = _dispatched_run(session_factory)
    # While the run is observable (AGENT_RUNNING), the branch correlates.
    assert orch.find_run_id_for_branch(branch) == run_id
    # Drive it terminal; the same branch must no longer match.
    orch.stop(run_id, user="lead@example.com")
    assert orch.find_run_id_for_branch(branch) is None


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


# -- change-freeze windows (issue #31, "time windows") --------------------------
def _weekend_freeze():
    from foundry.policy.freeze import ChangeFreezeWindow

    return [
        ChangeFreezeWindow(
            reason="Weekend release blackout",
            weekdays=("sat", "sun"),
            start="00:00",
            end="23:59",
            tz="UTC",
        )
    ]


def test_change_freeze_holds_autonomous_redispatch(session_factory) -> None:
    """During an active freeze, a failing PR is parked for a human instead of
    being auto-retried - no new agent job, and the escalation is audited."""
    import json
    from datetime import datetime, timezone

    from foundry.db.models import AuditEventType, FoundryAuditEvent

    tracker = InMemoryIssueTracker()
    # 2026-06-20 is a Saturday -> inside the weekend freeze.
    saturday = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
    orch, run_id, _provider = _dispatched_with_provider(
        session_factory,
        issue_tracker=tracker,
        change_freeze_windows=_weekend_freeze(),
        clock=lambda: saturday,
    )
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN

    failing = _pr(files_changed=[], ci_status=CIStatus.FAILING, summary="2 failed")
    assert orch.record_pr(run_id, failing) is RunStatus.REVIEW_REQUIRED
    # No remediation job was dispatched (only the original dispatch exists).
    assert _job_count(session_factory, run_id) == 1
    # The hold is on the audit trail as a change_freeze escalation.
    with session_factory() as s:
        events = (
            s.query(FoundryAuditEvent)
            .filter_by(run_id=run_id, event_type=AuditEventType.RISK_ESCALATED)
            .all()
        )
        metas = [json.loads(e.metadata_json or "{}") for e in events]
        assert any(m.get("category") == "change_freeze" for m in metas)
    # And the human is told why on the issue.
    assert any("change-freeze" in c for c in tracker.comments["i-1"])


def test_change_freeze_inactive_allows_redispatch(session_factory) -> None:
    """The same window outside its hours leaves the auto-retry byte-for-byte
    unchanged - the freeze is a one-way ratchet, not a global off-switch."""
    from datetime import datetime, timezone

    # 2026-06-17 is a Wednesday -> outside the weekend freeze.
    wednesday = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
    orch, run_id, _provider = _dispatched_with_provider(
        session_factory,
        change_freeze_windows=_weekend_freeze(),
        clock=lambda: wednesday,
    )
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN
    failing = _pr(files_changed=[], ci_status=CIStatus.FAILING, summary="2 failed")
    assert orch.record_pr(run_id, failing) is RunStatus.AGENT_RUNNING
    assert _job_count(session_factory, run_id) == 2


def test_pr_opened_emitted_once_across_remediation(session_factory) -> None:
    """PR_OPENED fires only on the first observation; later events (including
    pushes during remediation, when the status is AGENT_RUNNING again) emit
    PR_UPDATED. Regression for keying ``first_observation`` off the run status.
    """
    from foundry.db.models import AuditEventType, FoundryAuditEvent

    orch, run_id, _provider = _dispatched_with_provider(session_factory)
    orch.record_pr(run_id, _pr())  # first observation -> PR_OPENED
    # CI fails -> re-dispatch; the run is AGENT_RUNNING again.
    assert (
        orch.record_pr(run_id, _pr(ci_status=CIStatus.FAILING))
        is RunStatus.AGENT_RUNNING
    )
    # The agent pushes again mid-remediation: must be PR_UPDATED, not PR_OPENED.
    orch.record_pr(run_id, _pr())

    with session_factory() as s:
        opened = (
            s.query(FoundryAuditEvent)
            .filter_by(run_id=run_id, event_type=AuditEventType.PR_OPENED)
            .count()
        )
        updated = (
            s.query(FoundryAuditEvent)
            .filter_by(run_id=run_id, event_type=AuditEventType.PR_UPDATED)
            .count()
        )
    assert opened == 1
    assert updated >= 2


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


def test_budget_cap_blocks_first_dispatch_when_estimate_exceeds_cap(
    session_factory,
) -> None:
    """A single dispatch whose estimated cost already exceeds the cap is
    refused at ``start_agent`` (issue #29): no provider call, run BLOCKED."""
    provider = InMemoryFakeProvider()
    orch = _orch(
        session_factory,
        provider=provider,
        max_cost_per_run=5.0,
        estimated_cost_per_dispatch=6.0,
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    with pytest.raises(OrchestratorError):
        orch.dispatch_agent(run_id)
    assert _status(session_factory, run_id) is RunStatus.BLOCKED
    # Phase 3 only records a job for an allowed dispatch, so nothing ran.
    assert _job_count(session_factory, run_id) == 0


def test_estimate_counts_unreported_cost_against_budget(session_factory) -> None:
    """The fake provider reports no ``cost_usd``. With an estimate configured,
    each dispatched attempt counts the estimate as a proxy so the cap still
    binds on retry (issue #29) - the case claude_code / webhook / manual hit
    in production where the provider never reports spend."""
    orch, run_id, _provider = _dispatched_with_provider(
        session_factory, max_cost_per_run=5.0, estimated_cost_per_dispatch=3.0
    )
    orch.record_pr(run_id, _pr())
    # One unreported job has run (estimate 3.0); a retry would project
    # 3.0 + 3.0 = 6.0, past the 5.0 cap, so remediation is denied.
    failing = _pr(ci_status=CIStatus.FAILING)
    assert orch.record_pr(run_id, failing) is RunStatus.REVIEW_REQUIRED
    assert _job_count(session_factory, run_id) == 1  # no re-dispatch


def test_budget_snapshot_reports_consumed_and_cap(session_factory) -> None:
    """``budget_snapshot`` surfaces spend vs cap for the timeline/dashboard,
    using the estimate as a proxy for a provider that reports no cost."""
    orch, run_id, _provider = _dispatched_with_provider(
        session_factory, max_cost_per_run=10.0, estimated_cost_per_dispatch=2.5
    )
    snap = orch.budget_snapshot(run_id)
    assert snap["cap_usd"] == 10.0
    assert snap["estimated_cost_per_dispatch"] == 2.5
    # One unreported job -> the estimate stands in as consumed spend.
    assert snap["consumed_usd"] == 2.5


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


# -- durable-wait expiry (Temporal driver): clean terminal, never a strand ------


def _audit_meta(session_factory, run_id, event_type):
    import json

    from foundry.db.models import FoundryAuditEvent

    with session_factory() as s:
        events = [
            e
            for e in s.query(FoundryAuditEvent).filter_by(run_id=run_id)
            if e.event_type is event_type
        ]
        return [json.loads(e.metadata_json) if e.metadata_json else {} for e in events]


def test_expire_pending_approval_blocks_with_audited_reason(session_factory) -> None:
    from foundry.db.models import AuditEventType

    orch = _orch(session_factory)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL

    status = orch.expire_pending_approval(run_id)
    assert status is RunStatus.BLOCKED
    assert _status(session_factory, run_id) is RunStatus.BLOCKED
    assert _outcome_value(session_factory, run_id) == "blocked"
    metas = _audit_meta(session_factory, run_id, AuditEventType.RUN_BLOCKED)
    assert any(m.get("category") == "approval_window_expired" for m in metas)


def test_expire_pending_pr_fails_dispatched_run(session_factory) -> None:
    from foundry.db.models import AuditEventType

    orch, run_id = _dispatched_run(session_factory)
    assert _status(session_factory, run_id) is RunStatus.AGENT_RUNNING

    status = orch.expire_pending_pr(run_id)
    assert status is RunStatus.EXECUTION_FAILED
    assert _status(session_factory, run_id) is RunStatus.EXECUTION_FAILED
    assert _outcome_value(session_factory, run_id) == "failed"
    metas = _audit_meta(session_factory, run_id, AuditEventType.AGENT_FAILED)
    assert any(m.get("category") == "pr_window_expired" for m in metas)


def test_expire_is_idempotent_when_run_already_moved_on(session_factory) -> None:
    # The awaited signal won the race (the run was approved), or the activity is
    # being retried: expiry must be a no-op, never overwriting the live run.
    orch = _orch(session_factory, provider=InMemoryFakeProvider())
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    assert _status(session_factory, run_id) is RunStatus.APPROVED

    status = orch.expire_pending_approval(run_id)
    assert status is RunStatus.APPROVED
    assert _status(session_factory, run_id) is RunStatus.APPROVED


def test_expire_pending_pr_on_open_pr_is_noop(session_factory) -> None:
    # A run that has already opened a PR is not AGENT_RUNNING; a late PR-window
    # expiry must leave the delivered PR untouched.
    orch, run_id = _dispatched_run(session_factory)
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN

    status = orch.expire_pending_pr(run_id)
    assert status is RunStatus.PR_OPEN
    assert _status(session_factory, run_id) is RunStatus.PR_OPEN


def test_fail_run_marks_dispatched_run_execution_failed(session_factory) -> None:
    # The durable-workflow compensation (issue #37): an activity exhausted its
    # retries mid-flight, so the run is auto-failed rather than left active
    # forever at agent_running.
    from foundry.db.models import AuditEventType

    orch, run_id = _dispatched_run(session_factory)
    assert _status(session_factory, run_id) is RunStatus.AGENT_RUNNING

    status = orch.fail_run(run_id, reason="record_pr exhausted retries")
    assert status is RunStatus.EXECUTION_FAILED
    assert _status(session_factory, run_id) is RunStatus.EXECUTION_FAILED
    assert _outcome_value(session_factory, run_id) == "failed"
    metas = _audit_meta(session_factory, run_id, AuditEventType.AGENT_FAILED)
    failed = [m for m in metas if m.get("category") == "workflow_irrecoverable_error"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "record_pr exhausted retries"


def test_fail_run_works_from_any_active_state(session_factory) -> None:
    # The crash can happen before dispatch too (e.g. the approve activity), so
    # fail_run must terminate any still-active run, not only agent_running.
    orch = _orch(session_factory)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL

    assert orch.fail_run(run_id) is RunStatus.EXECUTION_FAILED
    assert _status(session_factory, run_id) is RunStatus.EXECUTION_FAILED
    assert _outcome_value(session_factory, run_id) == "failed"


def test_fail_run_is_idempotent_on_terminal_run(session_factory) -> None:
    # Temporal delivers activities at-least-once: a retried compensation, or one
    # that races a human stop / a delivered PR, must never overwrite a real
    # terminal state (AGENTS.md invariant #7: sticky BLOCKED stays blocked).
    orch = _orch(session_factory, provider=InMemoryFakeProvider())
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.reject(run_id, user="lead@example.com")
    assert _status(session_factory, run_id) is RunStatus.REJECTED

    assert orch.fail_run(run_id) is RunStatus.REJECTED
    assert _status(session_factory, run_id) is RunStatus.REJECTED
    # The original outcome stands; no failure outcome overwrote it.
    assert _outcome_value(session_factory, run_id) == "rejected"


def test_fail_run_cancels_in_flight_job(session_factory) -> None:
    # A run that crashed mid-flight may still have a job spending; compensation
    # cancels it like a human stop / PR-window expiry would.
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    orch.dispatch_agent(run_id)  # job left in flight

    orch.fail_run(run_id)
    assert _status(session_factory, run_id) is RunStatus.EXECUTION_FAILED
    with session_factory() as s:
        job = s.query(FoundryAgentJob).filter_by(run_id=run_id).one()
        assert job.status is AgentJobStatus.CANCELLED


def test_expire_pending_pr_cancels_in_flight_job(session_factory) -> None:
    # A dispatched-but-undelivered run may still be spending; PR-window expiry
    # cancels the job like a human stop would.
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    orch.dispatch_agent(run_id)  # job left in flight (not run to completion)

    orch.expire_pending_pr(run_id)
    assert _status(session_factory, run_id) is RunStatus.EXECUTION_FAILED
    with session_factory() as s:
        job = s.query(FoundryAgentJob).filter_by(run_id=run_id).one()
        assert job.status is AgentJobStatus.CANCELLED


# -- stop cancels the agent: "stop" means stop spending, not just stop listening -


def _running_run_with_provider(session_factory, provider):
    """A run dispatched (but not yet finished) so its provider job is in flight."""
    orch = _orch(session_factory, provider=provider)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    return orch, run_id, job


def _cancellation_events(session_factory, run_id):
    import json

    from foundry.db.models import AuditEventType, FoundryAuditEvent

    with session_factory() as s:
        return [
            json.loads(e.metadata_json or "{}")
            for e in s.query(FoundryAuditEvent).filter_by(run_id=run_id)
            if e.event_type is AuditEventType.AGENT_CANCELLED
        ]


def test_stop_cancels_the_provider_job(session_factory) -> None:
    from foundry.db.models import AgentJobStatus

    provider = InMemoryFakeProvider()
    orch, run_id, job = _running_run_with_provider(session_factory, provider)
    # Provider status uses schemas.common.AgentJobStatus; the DB column uses the
    # db.models enum. They are distinct classes, so compare provider status by
    # value (==) and the persisted job row by identity (is).
    assert provider.get_job_status(job.job_id).status == AgentJobStatus.RUNNING

    orch.stop(run_id, user="lead@example.com")

    # The agent was told to stop; the run is blocked; the job row is terminal.
    assert provider.get_job_status(job.job_id).status == AgentJobStatus.CANCELLED
    assert _status(session_factory, run_id) is RunStatus.BLOCKED
    assert _outcome_value(session_factory, run_id) == "blocked"
    with session_factory() as s:
        job_row = s.query(FoundryAgentJob).filter_by(run_id=run_id).one()
        assert job_row.status is AgentJobStatus.CANCELLED
        assert job_row.completed_at is not None

    events = _cancellation_events(session_factory, run_id)
    assert len(events) == 1
    assert events[0]["cancelled"] is True
    assert events[0]["job_id"] == job.job_id
    assert events[0]["requested_by"] == "lead@example.com"


def test_stop_still_blocks_when_provider_cancel_fails(session_factory) -> None:
    from foundry.db.models import AgentJobStatus

    class _UncancellableProvider(InMemoryFakeProvider):
        def cancel_job(self, job_id: str) -> None:
            raise RuntimeError("cursor cancel API unavailable")

    provider = _UncancellableProvider()
    orch, run_id, _job = _running_run_with_provider(session_factory, provider)

    # A provider that cannot cancel must never block the human's stop.
    orch.stop(run_id, user="lead@example.com")
    assert _status(session_factory, run_id) is RunStatus.BLOCKED
    assert _outcome_value(session_factory, run_id) == "blocked"
    with session_factory() as s:
        job_row = s.query(FoundryAgentJob).filter_by(run_id=run_id).one()
        # The job is not marked cancelled (the provider refused); the failure is
        # recorded rather than swallowed.
        assert job_row.status is AgentJobStatus.RUNNING
        assert "unavailable" in (job_row.error or "")

    events = _cancellation_events(session_factory, run_id)
    assert len(events) == 1
    assert events[0]["cancelled"] is False
    assert "unavailable" in events[0]["error"]


def test_reject_before_dispatch_records_no_cancellation(session_factory) -> None:
    # No agent has been dispatched yet, so there is nothing to cancel and no
    # spurious AGENT_CANCELLED event should be written.
    orch = _orch(session_factory)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.reject(run_id, user="lead@example.com")
    assert _status(session_factory, run_id) is RunStatus.REJECTED
    assert _cancellation_events(session_factory, run_id) == []


# -- audit integrity on provider dispatch failure (issue #13) -------------------


class _RaisingProvider(InMemoryFakeProvider):
    """Fake provider whose dispatch raises, like a real provider's HTTP call
    timing out or the secret-leak guard tripping."""

    name = "raising"

    def _dispatch(self, job_input):  # type: ignore[override]
        raise RuntimeError("provider API unreachable")


def _events_of_type(session_factory, run_id, event_type):
    from foundry.db.models import FoundryAuditEvent

    with session_factory() as s:
        return [
            e
            for e in s.query(FoundryAuditEvent).filter_by(run_id=run_id)
            if e.event_type is event_type
        ]


def test_provider_failure_at_dispatch_is_audited_not_swallowed(session_factory) -> None:
    """A provider exception during the first dispatch must leave a trail: the
    recorded policy ALLOW decision survives, an AGENT_FAILED event is written,
    and the run lands in a definite (failed) status - never stranded as APPROVED
    with a live authorisation but no agent (issue #13)."""
    from foundry.db.models import AuditEventType

    orch = _orch(session_factory, provider=_RaisingProvider())
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")

    with pytest.raises(RuntimeError):
        orch.dispatch_agent(run_id)

    # The run did not silently stay APPROVED.
    assert _status(session_factory, run_id) is RunStatus.EXECUTION_FAILED
    # The policy decision that authorised the (failed) dispatch is on the trail.
    with session_factory() as s:
        decisions = s.query(FoundryPolicyDecision).filter_by(run_id=run_id).all()
    assert any(d.allowed for d in decisions), "the allow decision was lost"
    # The failure itself is recorded.
    failed = _events_of_type(session_factory, run_id, AuditEventType.AGENT_FAILED)
    assert len(failed) == 1
    # No phantom AGENT_STARTED event for an agent that never started, and no job row.
    assert _events_of_type(session_factory, run_id, AuditEventType.AGENT_STARTED) == []
    assert _job_count(session_factory, run_id) == 0


def test_failed_dispatch_run_is_re_triggerable(session_factory) -> None:
    """A dispatch that failed at the provider is terminal, so a fresh intake for
    the same issue is allowed to start a new run (not refused as still-active)."""
    orch = _orch(session_factory, provider=_RaisingProvider())
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    with pytest.raises(RuntimeError):
        orch.dispatch_agent(run_id)

    # The same issue can be picked up again.
    assert orch.find_active_run_id_for_issue("i-1") is None
    new_run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    assert new_run_id != run_id


def test_provider_failure_during_remediation_hands_pr_to_human(session_factory) -> None:
    """If re-dispatch to fix a failing PR raises at the provider, the RETRY policy
    decision is still recorded, the failure is audited, and the PR is parked for
    a human (REVIEW_REQUIRED) rather than left at PR_OPEN with the gate row lost."""
    from foundry.db.models import AuditEventType

    # Dispatch the first job successfully, then swap in a provider that fails so
    # the *remediation* dispatch is the one that raises. Remediation reuses the
    # original job's provider (issue #33), so the swap is on the registry entry
    # the original job resolves through, not the unrelated default singleton.
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN

    orch._providers[provider.name] = _RaisingProvider()
    failing = _pr(files_changed=[], ci_status=CIStatus.FAILING, summary="2 failed")
    with pytest.raises(RuntimeError):
        orch.record_pr(run_id, failing)

    assert _status(session_factory, run_id) is RunStatus.REVIEW_REQUIRED
    # The RETRY_AGENT allow decision was committed before the provider call.
    with session_factory() as s:
        decisions = s.query(FoundryPolicyDecision).filter_by(run_id=run_id).all()
    assert any(d.allowed for d in decisions)
    # The failed remediation is audited and no second job row was created.
    assert _events_of_type(session_factory, run_id, AuditEventType.AGENT_FAILED)
    assert _job_count(session_factory, run_id) == 1


# -- learned dispatch (agent.provider: auto, issue #33) -------------------------


class _FakeAgentA(InMemoryFakeProvider):
    name = "agent_a"


class _FakeAgentB(InMemoryFakeProvider):
    name = "agent_b"


def _seed_outcome(
    session_factory,
    *,
    provider: str,
    outcome: str,
    work_type: str = "feature",
    repo: str = "customer-web",
    count: int = 1,
) -> None:
    """Insert dispatched outcome rows so recommend_provider has history to read."""
    import uuid
    from datetime import datetime, timezone

    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    with session_factory() as s:
        for _ in range(count):
            rid = f"seed-{uuid.uuid4().hex[:8]}"
            s.add(
                FoundryRun(
                    id=rid,
                    linear_issue_id=f"i-{rid}",
                    linear_issue_key=f"ENG-{rid}",
                    status=RunStatus.COMPLETE,
                    trigger_type="label",
                )
            )
            s.add(
                FoundryRunOutcome(
                    run_id=rid,
                    linear_issue_id=f"i-{rid}",
                    issue_key_prefix="ENG",
                    outcome=outcome,
                    repo=repo,
                    provider=provider,
                    work_type=work_type,
                    trigger_type="label",
                    jobs_count=1,
                    created_at_run=now,
                    completed_at=now,
                    recorded_at=now,
                )
            )
        s.commit()


def _auto_orch(session_factory, **overrides) -> tuple:
    """An orchestrator wired for agent.provider: auto over two fake agents.

    Default fallback is agent_b; agent_a is the other candidate. The same
    instances back both the registry and (for agent_b) the default provider, so
    a test can drive each agent's fake job lifecycle.
    """
    a, b = _FakeAgentA(), _FakeAgentB()
    kwargs = dict(
        provider=b,
        providers={a.name: a, b.name: b},
        auto_dispatch=True,
        auto_candidates=[a.name, b.name],
        auto_min_samples=3,
    )
    kwargs.update(overrides)
    orch = _orch(session_factory, **kwargs)
    return orch, a, b


def _selection_metadata(session_factory, run_id):
    import json

    from foundry.db.models import AuditEventType

    events = _events_of_type(session_factory, run_id, AuditEventType.AGENT_STARTED)
    assert len(events) == 1
    return json.loads(events[0].metadata_json)


def test_auto_dispatch_routes_to_recommended_provider(session_factory) -> None:
    """With agent.provider: auto, a first dispatch routes to the scorecard's
    pick - the agent with the majority-merged history for this work - and records
    an explainable selection reason on the AGENT_STARTED event."""
    # agent_a: 3/3 merged on features in customer-web; agent_b: 0/3.
    _seed_outcome(session_factory, provider="agent_a", outcome="merged", count=3)
    _seed_outcome(session_factory, provider="agent_b", outcome="failed", count=3)

    orch, a, _b = _auto_orch(session_factory)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)

    assert job.provider == "agent_a"
    assert _status(session_factory, run_id) is RunStatus.AGENT_RUNNING
    with session_factory() as s:
        row = s.query(FoundryAgentJob).filter_by(run_id=run_id).one()
    assert row.provider == "agent_a"

    selection = _selection_metadata(session_factory, run_id)["selection"]
    assert selection["mode"] == "auto"
    assert selection["selected_by"] == "scorecard"
    assert selection["provider"] == "agent_a"
    assert "agent_a" in selection["reason"]


def test_auto_dispatch_falls_back_when_no_recommendation(session_factory) -> None:
    """With no qualifying history (the min-sample floor / majority-merged gate not
    met), auto dispatch falls back to the configured default provider rather than
    routing to an unproven agent - and says so on the trail."""
    orch, _a, _b = _auto_orch(session_factory)  # empty outcome history
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)

    assert job.provider == "agent_b"  # the configured fallback/default
    selection = _selection_metadata(session_factory, run_id)["selection"]
    assert selection["selected_by"] == "fallback"
    assert selection["provider"] == "agent_b"
    assert "not enough evidence" in selection["reason"]


def test_single_provider_dispatch_records_no_selection(session_factory) -> None:
    """The default single-provider path is unchanged: no scorecard query, and no
    selection metadata on AGENT_STARTED (so existing trails are byte-for-byte
    identical)."""
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    orch.dispatch_agent(run_id)

    assert "selection" not in _selection_metadata(session_factory, run_id)


def test_remediation_reuses_original_provider_under_auto(session_factory) -> None:
    """A retry never re-routes mid-run: even if the scorecard would now prefer a
    different agent, remediation reuses the agent that opened the PR (issue #33)."""
    _seed_outcome(session_factory, provider="agent_a", outcome="merged", count=3)
    _seed_outcome(session_factory, provider="agent_b", outcome="failed", count=3)

    orch, a, _b = _auto_orch(session_factory, max_agent_retries=2)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    assert job.provider == "agent_a"
    a.run(job.job_id)
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN

    # Flip the recommendation hard towards agent_b before the retry. Remediation
    # must still reuse agent_a, never re-route to the now-"better" agent.
    _seed_outcome(session_factory, provider="agent_b", outcome="merged", count=20)

    failing = _pr(files_changed=[], ci_status=CIStatus.FAILING, summary="2 failed")
    assert orch.record_pr(run_id, failing) is RunStatus.AGENT_RUNNING
    with session_factory() as s:
        providers = [
            j.provider
            for j in s.query(FoundryAgentJob)
            .filter_by(run_id=run_id)
            .order_by(FoundryAgentJob.started_at, FoundryAgentJob.id)
            .all()
        ]
    assert providers == ["agent_a", "agent_a"]


# -- custom forbidden_globs config is honoured end-to-end -----------------------


def test_custom_forbidden_globs_block_a_matching_diff(session_factory) -> None:
    """A configured (non-default) forbidden glob hard-blocks a matching PR."""
    orch, run_id = _dispatched_run(
        session_factory, orch_kwargs={"forbidden_globs": ("config/**",)}
    )
    pr = _pr(files_changed=["config/feature_flags.yaml"])
    assert orch.record_pr(run_id, pr) is RunStatus.BLOCKED
    # Sticky: a clean follow-up push cannot revive the forbidden-path block.
    with pytest.raises(OrchestratorError):
        orch.record_pr(run_id, _pr())


def test_custom_forbidden_globs_replace_the_defaults(session_factory) -> None:
    """Passing forbidden_globs overrides the defaults: a path that the default
    list would hard-block (migrations/**) is no longer forbidden once a custom
    list is supplied. (It may still escalate via the sensitive-area gate, but it
    is not the sticky BLOCK the forbidden list promises.)"""
    orch, run_id = _dispatched_run(
        session_factory, orch_kwargs={"forbidden_globs": ("config/**",)}
    )
    pr = _pr(files_changed=["migrations/0001_add_table.sql"])
    assert orch.record_pr(run_id, pr) is not RunStatus.BLOCKED


# -- retry cap boundary: max_agent_retries == 0 ---------------------------------


def test_zero_retry_cap_denies_first_remediation(session_factory) -> None:
    """With the cap at 0, the very first CI-failure remediation is denied by the
    gate (attempt 1 > cap 0): the run parks for a human and no agent re-dispatches."""
    tracker = InMemoryIssueTracker()
    orch, run_id, _provider = _dispatched_with_provider(
        session_factory, issue_tracker=tracker, max_agent_retries=0
    )
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN

    failing = _pr(ci_status=CIStatus.FAILING, summary="pytest: 1 failed")
    assert orch.record_pr(run_id, failing) is RunStatus.REVIEW_REQUIRED
    assert _job_count(session_factory, run_id) == 1  # no re-dispatch
    assert any("could not remediate" in c for c in tracker.comments["i-1"])


# -- intent: a merged PR completes without a separate mark_complete gate --------


def test_merged_pr_completes_with_no_mark_complete_policy_decision(session_factory) -> None:
    """Completion is an orchestrator state transition governed by the upfront
    START_AGENT gate, not a distinct MARK_COMPLETE/OPEN_PR policy evaluation.
    This pins current intent: only START_AGENT (and RETRY_AGENT) decisions are
    ever recorded, so a future change that adds a completion gate is deliberate.
    """
    import json

    orch, run_id = _dispatched_run(session_factory)
    orch.record_pr(run_id, _pr())
    assert orch.record_pr(run_id, _pr(status=PRStatus.MERGED)) is RunStatus.COMPLETE

    with session_factory() as s:
        actions = {
            json.loads(d.input_json)["action"]
            for d in s.query(FoundryPolicyDecision).filter_by(run_id=run_id)
        }
    # Only the autonomous-work gate ran; no branch/PR/complete sub-gates.
    assert actions == {"start_agent"}
    assert "mark_complete" not in actions
    assert "open_pr" not in actions
    assert "create_branch" not in actions


# -- locked state transitions: "blocked stays blocked" under races (issue #10) ----


class _StopDuringDispatchProvider(InMemoryFakeProvider):
    """Simulates a human ``stop()`` landing between a dispatch's phase-1 commit
    and its phase-3 job record by stopping the run from *inside* ``create_job``.

    The provider call is the one moment a (re)dispatch holds no row lock, so it
    is exactly where a concurrent terminal transition can slip in (issue #10).
    Arm it (``armed = True``) immediately before the dispatch under test.
    """

    def __init__(self, orch_box, run_box, *, user="lead@example.com") -> None:
        super().__init__()
        self._orch_box = orch_box
        self._run_box = run_box
        self._user = user
        self.armed = False

    def create_job(self, job_input):
        if self.armed:
            self.armed = False
            self._orch_box[0].stop(self._run_box[0], user=self._user)
        return super().create_job(job_input)


def test_remediation_bails_when_run_no_longer_pr_open(session_factory) -> None:
    """A run stopped after record_pr committed PR_OPEN must not be revived: the
    remediation re-reads status under the row lock and bails (issue #10)."""
    orch, run_id = _dispatched_run(session_factory)
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN
    orch.stop(run_id, user="lead@example.com")  # human ends the run out of band
    before = _job_count(session_factory, run_id)

    result = orch._attempt_remediation(
        run_id, reason="ci_failed", pr_state=_pr(ci_status=CIStatus.FAILING)
    )

    assert result is RunStatus.BLOCKED
    assert _status(session_factory, run_id) is RunStatus.BLOCKED
    assert _job_count(session_factory, run_id) == before  # no re-dispatch


def test_duplicate_remediation_delivery_does_not_double_dispatch(session_factory) -> None:
    """Once a remediation claims the run (phase 1 flips it to AGENT_RUNNING under
    the lock), a duplicate CI-failure delivery re-reads a non-PR_OPEN status and
    bails - no second job, no retry-cap undercount (issue #10)."""
    orch, run_id = _dispatched_run(session_factory)
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN
    failing = _pr(ci_status=CIStatus.FAILING)
    assert orch.record_pr(run_id, failing) is RunStatus.AGENT_RUNNING
    after_first = _job_count(session_factory, run_id)  # original + 1 remediation

    # A duplicate delivery of the same failure event arrives while the run is
    # already AGENT_RUNNING from the first remediation.
    result = orch._attempt_remediation(run_id, reason="ci_failed", pr_state=failing)

    assert result is RunStatus.AGENT_RUNNING
    assert _job_count(session_factory, run_id) == after_first  # no extra dispatch


def test_stop_during_remediation_dispatch_is_not_reverted(session_factory) -> None:
    """A human stop that wins the row lock while the remediation provider call is
    in flight must stick: the run stays BLOCKED and the just-launched job is
    cancelled so it stops spending (issue #10)."""
    orch_box, run_box = [], []
    provider = _StopDuringDispatchProvider(orch_box, run_box)
    orch = _orch(session_factory, provider=provider, issue_tracker=InMemoryIssueTracker())
    orch_box.append(orch)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    run_box.append(run_id)
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)
    orch.record_pr(run_id, _pr())

    provider.armed = True  # the remediation's create_job will stop the run
    result = orch.record_pr(run_id, _pr(ci_status=CIStatus.FAILING))

    assert result is RunStatus.BLOCKED
    assert _status(session_factory, run_id) is RunStatus.BLOCKED
    with session_factory() as s:
        latest = (
            s.query(FoundryAgentJob)
            .filter_by(run_id=run_id)
            .order_by(FoundryAgentJob.started_at)
            .all()[-1]
        )
        assert latest.status is AgentJobStatus.CANCELLED


def test_stop_during_initial_dispatch_is_not_reverted(session_factory) -> None:
    """Same race on the first dispatch: a stop between phase 1 and phase 3 keeps
    the run BLOCKED rather than flipping it to AGENT_RUNNING (issue #10)."""
    orch_box, run_box = [], []
    provider = _StopDuringDispatchProvider(orch_box, run_box)
    orch = _orch(session_factory, provider=provider)
    orch_box.append(orch)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    run_box.append(run_id)
    orch.approve(run_id, user="lead@example.com")

    provider.armed = True  # the dispatch's create_job will stop the run
    orch.dispatch_agent(run_id)

    assert _status(session_factory, run_id) is RunStatus.BLOCKED
    with session_factory() as s:
        row = s.query(FoundryAgentJob).filter_by(run_id=run_id).one()
        assert row.status is AgentJobStatus.CANCELLED


def test_audit_events_unique_run_sequence_index_present(session_factory) -> None:
    """The per-run sequence is the audit trail's guaranteed order; a unique index
    makes a duplicate fail loudly instead of silently corrupting it (issue #10)."""
    from sqlalchemy import inspect

    with session_factory() as s:
        indexes = inspect(s.get_bind()).get_indexes("foundry_audit_events")
    unique_seq = [
        i for i in indexes
        if i["unique"] and set(i["column_names"]) == {"run_id", "sequence"}
    ]
    assert unique_seq, f"missing unique (run_id, sequence) index; have {indexes}"


# -- per-repo forbidden globs (issue #35: path-scoped policy for monorepos) ------


def test_per_repo_forbidden_glob_blocks_run(session_factory) -> None:
    """A repo-scoped forbidden glob hard-blocks a PR in that repo, even when the
    path is in neither the global forbidden list nor any sensitive area."""
    orch, run_id = _dispatched_run(
        session_factory,
        orch_kwargs={"repo_forbidden_globs": {"customer-web": ("**/ledger/**",)}},
    )
    pr = _pr(files_changed=["src/ledger/posting.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.BLOCKED
    # Sticky: the block stands and further events are refused (invariant #7).
    with pytest.raises(OrchestratorError):
        orch.record_pr(run_id, _pr())


def test_per_repo_forbidden_glob_is_scoped_to_its_repo(session_factory) -> None:
    """The same path is fine for a run routed to a *different* repo - per-repo
    globs are scoped to the repo they name, not applied globally."""
    provider = InMemoryFakeProvider()
    orch = _orch(
        session_factory,
        provider=provider,
        repo_forbidden_globs={"customer-web": ("**/ledger/**",)},
    )
    # Route this run to a different repo than the one with the extra rule.
    ticket = _ready_ticket(
        issue_id="i-billing",
        issue_key="LIN-200",
        known_repositories=["billing-svc"],
    )
    run_id = orch.intake_and_plan(ticket, trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)

    pr = PullRequestState(
        repo="billing-svc",
        pr_number=9,
        url="https://github.com/example/billing-svc/pull/9",
        branch="foundry/lin-200",
        status=PRStatus.OPEN,
        files_changed=["src/ledger/posting.ts"],
    )
    # No customer-web rule applies here, and the path is otherwise benign.
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_global_forbidden_globs_still_apply_with_per_repo_config(session_factory) -> None:
    """Per-repo globs are additive: the global forbidden list still blocks every
    repo, even one that has its own extra rules configured."""
    orch, run_id = _dispatched_run(
        session_factory,
        orch_kwargs={"repo_forbidden_globs": {"customer-web": ("**/ledger/**",)}},
    )
    # A global forbidden path (migrations/**) the per-repo config never mentions.
    pr = _pr(files_changed=["migrations/0003_add_index.sql"])
    assert orch.record_pr(run_id, pr) is RunStatus.BLOCKED


def test_per_repo_forbidden_globs_reach_agent_do_not_modify(session_factory) -> None:
    """The dispatched agent is told about repo-specific protected paths too, so a
    well-behaved agent avoids them rather than only being blocked after the fact."""
    provider = InMemoryFakeProvider()
    orch = _orch(
        session_factory,
        provider=provider,
        repo_forbidden_globs={"customer-web": ("**/ledger/**",)},
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)

    do_not_modify = provider._inputs[job.job_id].constraints.do_not_modify
    assert "**/ledger/**" in do_not_modify  # per-repo rule present
    assert "migrations/**" in do_not_modify  # global floor still present


# --- per-repo required approval roles (issue #31) ------------------------------


def test_per_repo_required_role_refuses_unqualified_approval(session_factory) -> None:
    """A repo-scoped required role (issue #31) is enforced at approve() the same
    way risk-derived roles are: an approver lacking it is refused before any
    void approval is recorded, even though the risk classifier flagged nothing
    sensitive for this low-risk feature."""
    provider = InMemoryFakeProvider()
    orch = _orch(
        session_factory,
        provider=provider,
        repo_required_roles={"customer-web": ("security",)},
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL

    # Approving without the repo-required security role is refused up front.
    with pytest.raises(OrchestratorError, match="security"):
        orch.approve(run_id, user="dev@example.com")
    # The run is untouched - still awaiting a qualified approval.
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL

    # With the security role granted, approval lands and dispatch proceeds.
    orch.approve(
        run_id, user="sec@example.com", granted_roles={ApprovalRole.SECURITY}
    )
    assert _status(session_factory, run_id) is RunStatus.APPROVED
    orch.dispatch_agent(run_id)
    assert _status(session_factory, run_id) is RunStatus.AGENT_RUNNING


def test_per_repo_required_role_scoped_to_its_repo(session_factory) -> None:
    """The same rule does not apply to a run routed to a different repo: an
    ordinary approval still works there (per-repo scoping, not global)."""
    orch = _orch(
        session_factory,
        provider=InMemoryFakeProvider(),
        repo_required_roles={"payments-service": ("security",)},
    )
    # This run routes to customer-web, which has no per-repo rule.
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")  # no roles needed
    assert _status(session_factory, run_id) is RunStatus.APPROVED


def test_per_repo_required_role_unions_with_risk_roles(session_factory) -> None:
    """Per-repo roles are additive on top of risk-derived ones: an auth ticket
    already needs engineering; the repo additionally needs security, so both
    must be granted to approve."""
    orch = _orch(
        session_factory,
        provider=InMemoryFakeProvider(),
        repo_required_roles={"customer-web": ("security",)},
    )
    ticket = _ready_ticket(
        title="Rotate auth login session tokens",
        description="Acceptance Criteria:\n- auth tokens rotate\n- login still works",
    )
    run_id = orch.intake_and_plan(ticket, trigger_type="label")
    # Engineering alone (the risk-derived role) is not enough now.
    with pytest.raises(OrchestratorError, match="security"):
        orch.approve(
            run_id, user="eng@example.com", granted_roles={ApprovalRole.ENGINEERING}
        )
    # Both roles together satisfy the union.
    orch.approve(
        run_id,
        user="lead@example.com",
        granted_roles={ApprovalRole.ENGINEERING, ApprovalRole.SECURITY},
    )
    assert _status(session_factory, run_id) is RunStatus.APPROVED


def test_per_repo_required_role_surfaces_in_tracker_comment(session_factory) -> None:
    """The approval-required comment lists the effective roles (risk-derived plus
    per-repo), so an approver is told the security sign-off this repo needs even
    though the risk classifier inferred no sensitive area."""
    tracker = InMemoryIssueTracker()
    orch = _orch(
        session_factory,
        issue_tracker=tracker,
        repo_required_roles={"customer-web": ("security",)},
    )
    orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    comment = tracker.comments["i-1"][0]
    assert "Required approval:" in comment
    assert "security" in comment


# --- N-of-M approval matrix (the "two-person rule", issue #31) ---------------


def _run(session_factory, run_id: str) -> FoundryRun:
    with session_factory() as s:
        return s.get(FoundryRun, run_id)


def test_min_approvals_needs_two_distinct_approvers(session_factory) -> None:
    """With ``min_approvals=2`` a single sign-off is not enough: the run stays
    at WAITING_APPROVAL (recording progress) until a *second* distinct human
    approves, only then advancing to APPROVED and becoming dispatchable."""
    provider = InMemoryFakeProvider()
    orch = _orch(session_factory, provider=provider, min_approvals=2)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")

    # First approval is recorded but does not advance the run.
    orch.approve(run_id, user="alice@example.com")
    run = _run(session_factory, run_id)
    assert run.status is RunStatus.WAITING_APPROVAL
    assert run.approved_by is None
    assert run.current_step == "awaiting_approval (1/2)"

    # Second, distinct approval meets the threshold.
    orch.approve(run_id, user="bob@example.com")
    run = _run(session_factory, run_id)
    assert run.status is RunStatus.APPROVED
    assert run.approved_by == "bob@example.com"

    orch.dispatch_agent(run_id)
    assert _status(session_factory, run_id) is RunStatus.AGENT_RUNNING


def test_min_approvals_rejects_duplicate_approver(session_factory) -> None:
    """The same person approving twice does not satisfy a two-person rule: the
    second attempt is refused so the distinct-approver count stays honest."""
    orch = _orch(session_factory, provider=InMemoryFakeProvider(), min_approvals=2)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")

    orch.approve(run_id, user="alice@example.com")
    with pytest.raises(OrchestratorError, match="already approved"):
        orch.approve(run_id, user="alice@example.com")
    # Still one approver short; the run has not advanced.
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL
    assert _run(session_factory, run_id).current_step == "awaiting_approval (1/2)"


def test_min_approvals_default_one_is_unchanged(session_factory) -> None:
    """Default ``min_approvals=1`` is the historical single-approval lifecycle:
    one sign-off advances the run straight to APPROVED."""
    orch = _orch(session_factory, provider=InMemoryFakeProvider())
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    assert _status(session_factory, run_id) is RunStatus.APPROVED


def test_per_repo_min_approvals_raises_the_floor(session_factory) -> None:
    """A per-repo override demands more sign-offs than the global floor for runs
    routed to that repo (max of the two)."""
    orch = _orch(
        session_factory,
        provider=InMemoryFakeProvider(),
        min_approvals=1,
        repo_min_approvals={"customer-web": 2},
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="alice@example.com")
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL
    orch.approve(run_id, user="bob@example.com")
    assert _status(session_factory, run_id) is RunStatus.APPROVED


def test_per_repo_min_approvals_scoped_to_its_repo(session_factory) -> None:
    """A per-repo override on a *different* repo does not raise the bar here: a
    run routed to customer-web still advances on a single approval."""
    orch = _orch(
        session_factory,
        provider=InMemoryFakeProvider(),
        repo_min_approvals={"payments-service": 3},
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    assert _status(session_factory, run_id) is RunStatus.APPROVED


def test_per_repo_min_approvals_never_lowers_global_floor(session_factory) -> None:
    """A per-repo value below the global floor cannot weaken it: the effective
    minimum is the max, so the global floor still binds (invariant #1)."""
    orch = _orch(
        session_factory,
        provider=InMemoryFakeProvider(),
        min_approvals=2,
        repo_min_approvals={"customer-web": 1},
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="alice@example.com")
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL
    orch.approve(run_id, user="bob@example.com")
    assert _status(session_factory, run_id) is RunStatus.APPROVED


def test_min_approvals_surfaced_in_tracker_comment(session_factory) -> None:
    """The intake comment tells the approver up front that the run needs >1
    distinct sign-off, so the two-person rule isn't a parked-run surprise (#31)."""
    tracker = InMemoryIssueTracker()
    orch = _orch(session_factory, issue_tracker=tracker, min_approvals=2)
    orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    comment = tracker.comments["i-1"][0]
    assert "Approvers required:" in comment
    assert "2 distinct sign-offs" in comment


def test_single_approval_tracker_comment_has_no_count(session_factory) -> None:
    """Default single-approval intake comment is unchanged - no N-of-M line."""
    tracker = InMemoryIssueTracker()
    orch = _orch(session_factory, issue_tracker=tracker)
    orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    assert "Approvers required:" not in tracker.comments["i-1"][0]


def test_min_approvals_unions_roles_across_approvers(session_factory) -> None:
    """N-of-M composes with required roles: each distinct approver must hold the
    required role, and the run dispatches once enough have signed (the granted
    roles reaching the gate are the union across approvers)."""
    provider = InMemoryFakeProvider()
    orch = _orch(
        session_factory,
        provider=provider,
        min_approvals=2,
        repo_required_roles={"customer-web": ("security",)},
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")

    orch.approve(
        run_id, user="sec1@example.com", granted_roles={ApprovalRole.SECURITY}
    )
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL
    orch.approve(
        run_id, user="sec2@example.com", granted_roles={ApprovalRole.SECURITY}
    )
    assert _status(session_factory, run_id) is RunStatus.APPROVED

    orch.dispatch_agent(run_id)
    assert _status(session_factory, run_id) is RunStatus.AGENT_RUNNING


def test_min_approvals_records_each_signoff_in_audit(session_factory) -> None:
    """Every sign-off in an N-of-M run is its own recorded approval artifact, so
    the audit trail shows who approved, not just that the threshold was met."""
    orch = _orch(session_factory, provider=InMemoryFakeProvider(), min_approvals=2)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="alice@example.com")
    orch.approve(run_id, user="bob@example.com")
    with session_factory() as s:
        records = (
            s.query(FoundryArtifact)
            .filter(
                FoundryArtifact.run_id == run_id,
                FoundryArtifact.artifact_type == ArtifactType.APPROVAL_RECORD,
            )
            .all()
        )
    assert len(records) == 2


# --- N-of-M mid-flow re-ping (issue #31) -------------------------------------


def test_partial_signoff_nudges_next_approver(session_factory) -> None:
    """A partial N-of-M sign-off re-pings the next approver on both surfaces: a
    tracker comment and a chat progress nudge carrying the (collected/required)
    counts and who just signed (issue #31)."""
    tracker = InMemoryIssueTracker()
    notifier = InMemoryNotifier()
    orch = _orch(
        session_factory,
        provider=InMemoryFakeProvider(),
        issue_tracker=tracker,
        notifier=notifier,
        min_approvals=2,
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")

    # comments[0] is the intake analysis comment; the nudge is the next one.
    assert len(tracker.comments["i-1"]) == 1
    assert not notifier.progress  # nothing nudged before the first sign-off

    orch.approve(run_id, user="alice@example.com")
    assert _status(session_factory, run_id) is RunStatus.WAITING_APPROVAL

    # Chat progress nudge.
    assert len(notifier.progress) == 1
    nudge = notifier.progress[0]
    assert nudge.issue_id == "i-1"
    assert (nudge.collected, nudge.required) == (1, 2)
    assert nudge.last_approver == "alice@example.com"
    assert nudge.remaining == 1

    # Tracker progress comment.
    assert len(tracker.comments["i-1"]) == 2
    progress_comment = tracker.comments["i-1"][1]
    assert "approval progress" in progress_comment.lower()
    assert "1 of 2 distinct sign-offs" in progress_comment
    assert "alice@example.com" in progress_comment


def test_final_signoff_does_not_nudge(session_factory) -> None:
    """The sign-off that *meets* the threshold advances the run to APPROVED and
    must not also fire a 'more needed' progress nudge."""
    notifier = InMemoryNotifier()
    orch = _orch(
        session_factory,
        provider=InMemoryFakeProvider(),
        notifier=notifier,
        min_approvals=2,
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="alice@example.com")
    orch.approve(run_id, user="bob@example.com")
    assert _status(session_factory, run_id) is RunStatus.APPROVED
    # Exactly one nudge (after the first sign-off), none after the final one.
    assert len(notifier.progress) == 1


def test_single_approval_never_nudges(session_factory) -> None:
    """Default ``min_approvals=1`` never reaches the partial branch, so no
    progress nudge is sent and the tracker keeps only its intake comment - the
    single-approval path is byte-for-byte unchanged (invariant #1)."""
    tracker = InMemoryIssueTracker()
    notifier = InMemoryNotifier()
    orch = _orch(
        session_factory,
        provider=InMemoryFakeProvider(),
        issue_tracker=tracker,
        notifier=notifier,
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    assert _status(session_factory, run_id) is RunStatus.APPROVED
    assert notifier.progress == []
    assert len(tracker.comments["i-1"]) == 1  # intake comment only
