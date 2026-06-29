"""FoundryOrchestrator - drives a single Ticket-to-PR run.

This is the connective tissue between the intelligence engines, the policy gate,
the coding-agent providers and the data model. It is deliberately
infrastructure-light: it persists artifacts/audit/policy rows through a SQLAlchemy
session factory and pauses at human approval. A Temporal workflow will later wrap
these same steps as durable activities; LangGraph/LLM engines slot in behind the
engine protocols. None of this layer makes a network call.

Lifecycle:

    intake_and_plan(ticket)        # analyse -> enrich -> risk -> plan -> gate
        -> WAITING_APPROVAL | NEEDS_CLARIFICATION | BLOCKED
    approve(run_id, ...)           # records approval, -> APPROVED
    dispatch_agent(run_id)         # re-checks policy, launches provider -> AGENT_RUNNING
    record_pr(run_id, pr_state)    # PR_OPEN | REVIEW_REQUIRED | BLOCKED
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from sqlalchemy.exc import IntegrityError

from foundry.agents.manual import ManualProvider
from foundry.agents.provider import CodingAgentProvider
from foundry.connectors.base import IssueTracker
from foundry.connectors.comments import (
    format_analysis_comment,
    format_approval_progress_comment,
    state_for,
)
from foundry.connectors.notify import ApprovalProgress, ApprovalRequest, RunNotifier
from foundry.engines.decomposition import EpicDecomposer, HeuristicDecomposer
from foundry.epics import EpicIntakeResult, compute_epic_rollup
from foundry.observability import traced
from foundry.audit.events import (
    build_artifact,
    build_audit_event,
    build_policy_decision_row,
    new_id,
)
from foundry.db.models import (
    AgentJobStatus,
    ArtifactType,
    AuditEventType,
    FoundryAgentJob,
    FoundryArtifact,
    FoundryRun,
)
from foundry.engines.analyzer import HeuristicAnalyzer, TicketAnalyzer
from foundry.engines.enrichment import ContextEnricher, StaticContextEnricher
from foundry.engines.llm_plan_satisfaction import PlanSatisfactionJudge
from foundry.engines.planner import (
    DEFAULT_FORBIDDEN_GLOBS,
    DeliveryPlanner,
    TemplatePlanner,
    branch_name_for,
)
from foundry.engines.risk import (
    CustomRiskCategory,
    DiffRiskClassifier,
    GlobDiffRiskClassifier,
    HeuristicRiskClassifier,
    RiskClassifier,
    diff_touches_tests,
    files_matching_scope,
    files_outside_scope,
    glob_match,
    merge_sensitive_keywords,
)
from foundry.policy.engine import (
    LocalPolicyEngine,
    PolicyBudget,
    PolicyEngine,
    PolicyInput,
    PolicyRepo,
    PolicyRetry,
    PolicyRisk,
    PolicyTicket,
    required_approvals,
)
from foundry.policy.freeze import ChangeFreezeWindow, active_freeze, describe_window
from foundry.schemas.agent import CodingAgentJob, CodingAgentJobInput, JobConstraints
from foundry.schemas.analysis import TicketAnalysis
from foundry.memory.outcomes import record_outcome
from foundry.memory.scorecards import DEFAULT_MIN_SAMPLES, recommend_provider
from foundry.schemas.common import (
    ACTIVE_RUN_STATUSES,
    PR_OBSERVABLE_STATUSES,
    AgentMode,
    ApprovalRole,
    CIStatus,
    OverallRisk,
    PolicyAction,
    PRStatus,
    ReviewStatus,
    RunStatus,
    TERMINAL_RUN_STATUSES,
)
from foundry.schemas.context import ContextBundle
from foundry.schemas.plan import DeliveryPlan
from foundry.schemas.pr import PullRequestState
from foundry.schemas.risk import RiskAssessment, RiskEvidence
from foundry.schemas.ticket import RawTicket

# Which Pydantic model each loadable artifact type deserialises back into.
_ARTIFACT_MODELS: dict[ArtifactType, type] = {
    ArtifactType.TICKET_SNAPSHOT: RawTicket,
    ArtifactType.TICKET_ANALYSIS: TicketAnalysis,
    ArtifactType.CONTEXT_BUNDLE: ContextBundle,
    ArtifactType.RISK_ASSESSMENT: RiskAssessment,
    ArtifactType.DELIVERY_PLAN: DeliveryPlan,
}


_log = logging.getLogger(__name__)

# Run states in which PR webhook events are meaningful for the run. Defined once
# in schemas/common.py so the orchestrator and the durable workflow share it.
_PR_OBSERVABLE_STATUSES = PR_OBSERVABLE_STATUSES

# Status transitions worth pinging a chat surface about: parked, blocked, PR
# open, merged (and the failure terminals, which an approver wants to hear about
# as much as a clean block). Routine intermediate states (analysing, approved,
# agent_running, review_required) are not notified - they would be noise.
_NOTIFIABLE_STATUSES = frozenset(
    {
        RunStatus.NEEDS_CLARIFICATION,
        RunStatus.BLOCKED,
        RunStatus.REJECTED,
        RunStatus.EXECUTION_FAILED,
        RunStatus.PR_OPEN,
        RunStatus.COMPLETE,
    }
)

# Linear-style issue keys (ENG-123) appearing in branch names or PR titles.
_ISSUE_KEY_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{1,9}-\d+)\b")


class OrchestratorError(RuntimeError):
    """Raised when a run cannot proceed (e.g. policy blocked dispatch)."""


class FoundryOrchestrator:
    def __init__(
        self,
        session_factory,
        *,
        analyzer: TicketAnalyzer | None = None,
        enricher: ContextEnricher | None = None,
        risk_classifier: RiskClassifier | None = None,
        diff_risk_classifier: DiffRiskClassifier | None = None,
        planner: DeliveryPlanner | None = None,
        decomposer: EpicDecomposer | None = None,
        policy_engine: PolicyEngine | None = None,
        provider: CodingAgentProvider | None = None,
        providers: Mapping[str, CodingAgentProvider] | None = None,
        auto_dispatch: bool = False,
        auto_candidates: Sequence[str] | None = None,
        auto_min_samples: int = DEFAULT_MIN_SAMPLES,
        issue_tracker: IssueTracker | None = None,
        notifier: RunNotifier | None = None,
        max_files_changed: int = 12,
        forbidden_globs: tuple[str, ...] | list[str] | None = None,
        repo_forbidden_globs: Mapping[str, tuple[str, ...]] | None = None,
        repo_required_roles: Mapping[str, tuple[str, ...]] | None = None,
        min_approvals: int = 1,
        repo_min_approvals: Mapping[str, int] | None = None,
        path_required_roles: Mapping[str, tuple[str, ...]] | None = None,
        enforce_plan_scope: bool = True,
        enforce_plan_out_of_scope: bool = True,
        enforce_plan_tests: bool = False,
        test_path_globs: Sequence[str] | None = None,
        plan_satisfaction_judge: PlanSatisfactionJudge | None = None,
        sensitive_path_globs: Mapping[str, tuple[str, ...]] | None = None,
        extra_sensitive_keywords: Mapping[str, Sequence[str]] | None = None,
        custom_risk_categories: Sequence[CustomRiskCategory] | None = None,
        change_freeze_windows: Sequence[ChangeFreezeWindow] | None = None,
        clock: Callable[[], datetime] | None = None,
        max_agent_retries: int = 2,
        retry_on: tuple[str, ...] | list[str] = ("ci_failed", "changes_requested"),
        max_cost_per_run: float | None = None,
        estimated_cost_per_dispatch: float = 0.0,
    ) -> None:
        self._sf = session_factory
        self._analyzer = analyzer or HeuristicAnalyzer()
        self._enricher = enricher or StaticContextEnricher()
        # Ticket-text risk classifier. When none is injected, build the default
        # heuristic, layering any operator-supplied extra keywords on top of the
        # built-in floor (issue #31, the ticket-text twin of sensitive_path_globs).
        # Strictly additive - extras can only flag *more* areas, never fewer.
        if risk_classifier is not None:
            self._risk = risk_classifier
        elif extra_sensitive_keywords:
            self._risk = HeuristicRiskClassifier(
                keywords=merge_sensitive_keywords(extra_sensitive_keywords)
            )
        else:
            self._risk = HeuristicRiskClassifier()
        self._planner = planner or TemplatePlanner()
        # Epic-producer seam (issue #35): deterministic by default; the LLM
        # decomposer (decomposition.provider: llm) recovers prose-described
        # epics, keeping the deterministic decomposer as a non-overridable floor.
        self._decomposer = decomposer or HeuristicDecomposer()
        self._policy = policy_engine or LocalPolicyEngine()
        self._provider = provider or ManualProvider()
        # Learned dispatch (issue #33): the orchestrator is provider-*registry*
        # aware so cost-refresh, cancellation and remediation can reconcile each
        # run against the agent that *actually* ran it (its recorded
        # ``job.provider``), not a single configured singleton. The default
        # provider is always in the registry under its own name, so the
        # single-provider path is byte-for-byte unchanged: ``_provider_for`` then
        # resolves exactly the same provider the old ``job.provider == name``
        # guard did, and returns ``None`` ("not ours") for any other.
        self._providers: dict[str, CodingAgentProvider] = dict(providers or {})
        self._providers.setdefault(self._provider.name, self._provider)
        # When ``auto_dispatch`` is on (``agent.provider: auto``), a *first*
        # dispatch picks the provider by scorecard over ``auto_candidates``,
        # falling back to ``_provider`` when no agent has earned a recommendation
        # yet. Off by default, so nothing changes unless explicitly enabled.
        self._auto_dispatch = auto_dispatch
        self._auto_candidates = tuple(auto_candidates or self._providers.keys())
        self._auto_min_samples = auto_min_samples
        # Optional: when set, Foundry writes progress/state back to the tracker.
        self._tracker = issue_tracker
        # Optional: when set, Foundry posts approval messages + status updates to
        # a chat surface (Slack). Best-effort, like the tracker write-back.
        self._notifier = notifier
        self._max_files_changed = max_files_changed
        self._forbidden_globs = list(
            forbidden_globs if forbidden_globs is not None else DEFAULT_FORBIDDEN_GLOBS
        )
        # Per-repo forbidden globs (issue #35, path-scoped policy for monorepos):
        # additional protected subtrees that apply only to runs routed to a given
        # repo, layered *on top of* the global list above - never replacing it.
        self._repo_forbidden_globs = {
            repo: list(globs) for repo, globs in (repo_forbidden_globs or {}).items()
        }
        # Per-repo required approval roles (issue #31, per-repo policy scoping):
        # extra approval roles demanded of any run routed to a given repo, on top
        # of whatever the risk classifier derives. Strictly additive - they only
        # ever *add* a required approval (invariant #1). Role names are validated
        # against the ApprovalRole vocabulary at config load, so coercing here is
        # safe.
        self._repo_required_roles = {
            repo: [ApprovalRole(r) for r in roles]
            for repo, roles in (repo_required_roles or {}).items()
        }
        # Minimum distinct human approvers per run (issue #31, N-of-M approval
        # matrix / "two-person rule"). Default 1 = the historical single-approval
        # lifecycle, byte-for-byte. A run accumulates approvals and only advances
        # to APPROVED once the effective minimum is met; a per-repo override can
        # only raise the bar (``max`` of the two), never lower it (invariant #1).
        # Enforced here in the lifecycle - like the orchestrator-only
        # forbidden-path block - not in the policy gate, so there is no
        # Python/Rego lock-step concern (invariant #2 does not apply).
        self._min_approvals = max(1, min_approvals)
        self._repo_min_approvals = {
            repo: count for repo, count in (repo_min_approvals or {}).items()
        }
        # Per-*path* required approval roles (issue #31/#35, per-path policy
        # scoping for monorepos). Unlike the per-repo roles above (resolved at
        # intake from the routed repo, before a diff exists), these are evaluated
        # diff-aware in the PR re-check: a PR whose diff touches a configured path
        # whose role is not already covered by the run's approvers escalates the
        # run to REVIEW_REQUIRED for a human sign-off. Strictly additive - it can
        # only ever *escalate* to human review, never release a run (invariant #1)
        # - and enforced in the orchestrator lifecycle, like the forbidden-path
        # block, so there is no policy-engine/Rego lock-step concern (invariant #2
        # does not apply). Role names are validated at config load, so coercing
        # here is safe. Empty by default = the historical behaviour byte-for-byte.
        self._path_required_roles = {
            glob: [ApprovalRole(r) for r in roles]
            for glob, roles in (path_required_roles or {}).items()
        }
        # Plan-scope drift escalation (the long-promised plan-vs-diff check, the
        # consumer of the LLM planner's ``DeliveryPlan.expected_files_or_areas``).
        # When a PR's diff changes files that fall outside *every* file/area the
        # approved plan declared, the run is escalated to REVIEW_REQUIRED for a
        # human - the "agent strayed outside its approved scope" signal. Like the
        # per-path approval-role escalation it is strictly additive (it can only
        # ever *escalate* to human review, never release a run - invariant #1) and
        # enforced in the orchestrator lifecycle, not the policy gate, so there is
        # no Python/Rego lock-step concern (invariant #2 does not apply). It is
        # data-inert whenever the plan declares no expected files/areas (the
        # template planner's default), so the only runs it engages are ones a
        # code-aware planner scoped - the kill switch is this flag.
        self._enforce_plan_scope = enforce_plan_scope
        # The out-of-scope twin of the drift check: a PR touching a path/area the
        # approved plan explicitly listed in ``out_of_scope`` is a *stronger*
        # off-plan signal than merely straying outside ``expected_files_or_areas``
        # (the plan promised not to touch it), so it escalates the run to a human.
        # Same family as plan-scope drift - orchestrator-only, escalate-only
        # (invariant #1), no Rego mirror (invariant #2 does not apply), and
        # data-inert whenever the plan declares no out-of-scope entries (the
        # template planner's default) - and the kill switch is this flag.
        self._enforce_plan_out_of_scope = enforce_plan_out_of_scope
        # Plan-tests-satisfaction (issue #169, slice 2): a deterministic member of
        # the same orchestrator-only, escalate-only plan-aware family. When on, a
        # PR whose approved plan promised tests (any unit/integration/e2e entry in
        # ``test_plan``) but whose diff touches *no* test file (per
        # ``test_path_globs``) escalates the run to a human - the "the plan
        # promised tests, the diff shipped none" signal. Default off: it is a
        # heuristic whose ``test_path_globs`` convention may need per-repo tuning
        # before it is trustworthy enough to gate on - so the *default* behaviour
        # is byte-for-byte unchanged (unlike the scope checks, the template planner
        # *does* promise a unit test per AC, so inertness here rides on the switch,
        # not on an empty plan field). Escalate-only (invariant #1), no Rego mirror
        # (invariant #2 does not apply), and data-inert whenever the plan declares
        # no tests at all.
        self._enforce_plan_tests = enforce_plan_tests
        if test_path_globs is None:
            from foundry.config import DEFAULT_TEST_PATH_GLOBS

            test_path_globs = DEFAULT_TEST_PATH_GLOBS
        self._test_path_globs = tuple(test_path_globs)
        # Plan-satisfaction judge (issue #169, slice 3): the headline plan-aware
        # gate that reasons about whether the diff actually *does what the plan
        # said* (goal/scope/steps), beyond the deterministic file-containment
        # checks above. Injected only when ``plan_satisfaction.provider: llm`` is
        # configured; ``None`` (the default) means the check is a pure no-op, so
        # offline deployments are byte-for-byte unchanged. Escalate-only and
        # degrade-to-noop: it can only ever raise a run to REVIEW_REQUIRED, and an
        # LLM failure leaves the deterministic gates in charge (never blocks or
        # releases a run). Orchestrator-only, no Rego mirror (invariant #2 does
        # not apply).
        self._plan_satisfaction_judge = plan_satisfaction_judge
        if sensitive_path_globs is None:
            from foundry.config import DEFAULT_SENSITIVE_PATH_GLOBS

            sensitive_path_globs = dict(DEFAULT_SENSITIVE_PATH_GLOBS)
        self._sensitive_path_globs = dict(sensitive_path_globs)
        self._diff_risk = diff_risk_classifier or GlobDiffRiskClassifier(
            self._sensitive_path_globs
        )
        # The default glob classifier never reads the ticket; skip the artifact
        # load on every PR event in that (common) case.
        self._diff_risk_needs_ticket = not isinstance(
            self._diff_risk, GlobDiffRiskClassifier
        )
        # Operator-defined custom risk categories (issue #155): named categories
        # beyond the fixed seven built-in areas, each with ticket-text keyword
        # and/or diff-path-glob triggers that demand approval roles. Fired on the
        # ticket text at intake (the roles are unioned into the run's resolved
        # required roles, so the gate enforces them) and on the diff at every PR
        # push (an unsatisfied role escalates the run to REVIEW_REQUIRED, like
        # the per-path roles). Strictly additive / escalate-only - validated at
        # config load, so coercing roles here is safe. Empty by default =
        # byte-for-byte the historical behaviour.
        self._custom_risk_categories = list(custom_risk_categories or [])
        # Change-freeze / maintenance windows (issue #31, the "time windows"
        # policy dimension). During an active window an *autonomous* re-dispatch
        # is held and the run is escalated to REVIEW_REQUIRED for a human, like
        # the per-path approval-role escalation - strictly additive (a freeze can
        # only hold an action, never release one - invariant #1) and enforced in
        # the lifecycle, not the gate, so there is no Python/Rego lock-step
        # concern (invariant #2 does not apply). Empty = the historical behaviour
        # byte-for-byte. ``clock`` is injectable so the freeze check tests offline
        # (invariant #3); it defaults to wall-clock UTC.
        self._change_freeze_windows = tuple(change_freeze_windows or ())
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_agent_retries = max_agent_retries
        self._retry_on = frozenset(retry_on)
        self._max_cost_per_run = max_cost_per_run
        self._estimated_cost_per_dispatch = estimated_cost_per_dispatch

    # -- intake + planning ----------------------------------------------------

    @traced("foundry.intake_and_plan")
    def intake_and_plan(
        self,
        ticket: RawTicket,
        *,
        trigger_type: str,
        created_by: str | None = None,
        parent_run_id: str | None = None,
    ) -> str:
        """Run analysis -> context -> risk -> plan -> policy gate; persist all.

        Concurrent intake for the same issue is safe: the pre-check below is
        the fast path, and the ``uq_foundry_runs_one_active_per_issue`` partial
        unique index is the arbiter. An intake that loses the race returns the
        surviving run's id instead of creating a duplicate.

        ``parent_run_id`` links the new run as a child of an existing epic run
        (issue #35). The parent must exist and itself be a root - epics are a
        single level in v1, so a child cannot be an epic. Each child is an
        ordinary, independently-gated run; the parent only groups them.
        """
        # At most one *active* run per issue; finished/blocked runs may be
        # superseded by a fresh trigger (e.g. after the ticket is clarified).
        active = self.find_active_run_id_for_issue(ticket.issue_id)
        if active is not None:
            raise OrchestratorError(
                f"issue {ticket.issue_id} already has an active run ({active})"
            )
        if parent_run_id is not None:
            self._validate_epic_parent(parent_run_id)
        run_id = new_id("run")
        analysis = self._analyzer.analyse(ticket)
        context = self._enricher.enrich(ticket, analysis)
        risk = self._risk.classify(ticket, analysis, context)
        # Layer operator-defined custom risk categories on top of the classifier's
        # built-in-area assessment (issue #155): a category whose ticket-text
        # keywords fire demands its approval roles. Escalate-only - this only ever
        # *adds* required roles and cited evidence, never lowers risk or drops a
        # built-in's role.
        risk = self._apply_custom_risk_categories(ticket, risk)
        plan = self._planner.plan(ticket, analysis, context, risk)
        payload = self._policy_input(PolicyAction.START_AGENT, analysis, context, risk)
        decision = self._policy.evaluate(payload)

        # The recorded gate decision above is the honest *pre-approval* result:
        # with no approval yet, the gate denies dispatch (every autonomous action
        # now requires a recorded approval, issue #18). Probe the gate with a
        # recorded approval carrying every required role granted to learn two
        # things: (1) whether the denial is permanent - blocked no matter who
        # approves, e.g. DB migrations / prod deploys - which parks at BLOCKED
        # instead of inviting a futile approval; and (2) the agent mode actually
        # *achievable* once approved, so the run advertises that rather than the
        # transient human-only of its unapproved state.
        permanently_blocked = False
        effective_mode = decision.allowed_agent_mode
        if not decision.allowed:
            with_approvals = payload.model_copy(
                update={
                    "approval": {role.value: True for role in decision.required_approvals},
                    "approval_present": True,
                }
            )
            approved_decision = self._policy.evaluate(with_approvals)
            permanently_blocked = not approved_decision.allowed
            if not permanently_blocked:
                effective_mode = approved_decision.allowed_agent_mode

        status = self._post_plan_status(analysis, risk, permanently_blocked)

        try:
            with self._sf() as session:
                run = FoundryRun(
                    id=run_id,
                    linear_issue_id=ticket.issue_id,
                    linear_issue_key=ticket.issue_key,
                    status=RunStatus.ANALYSING,
                    trigger_type=trigger_type,
                    created_by=created_by,
                    current_step="intake",
                    risk_level=risk.overall_risk,
                    agent_mode=effective_mode,
                    parent_run_id=parent_run_id,
                )
                session.add(run)
                self._add(session, run_id, ArtifactType.TICKET_SNAPSHOT, ticket)
                self._add(session, run_id, ArtifactType.TICKET_ANALYSIS, analysis)
                self._add(session, run_id, ArtifactType.CONTEXT_BUNDLE, context)
                self._add(session, run_id, ArtifactType.RISK_ASSESSMENT, risk)
                self._add(session, run_id, ArtifactType.DELIVERY_PLAN, plan)
                session.add(
                    build_audit_event(
                        run_id=run_id,
                        event_type=AuditEventType.RUN_STARTED,
                        actor_type="foundry",
                        output_content=ticket,
                    )
                )
                session.add(
                    build_audit_event(
                        run_id=run_id,
                        event_type=AuditEventType.ANALYSIS_COMPLETED,
                        actor_type="foundry",
                        output_content=analysis,
                    )
                )
                session.add(
                    build_policy_decision_row(
                        run_id=run_id, payload=payload, decision=decision
                    )
                )
                session.add(
                    build_audit_event(
                        run_id=run_id,
                        event_type=AuditEventType.POLICY_EVALUATED,
                        actor_type="foundry",
                        output_content=decision,
                    )
                )
                run.status = status
                run.current_step = "planned"
                if status is RunStatus.WAITING_APPROVAL:
                    session.add(
                        build_audit_event(
                            run_id=run_id,
                            event_type=AuditEventType.APPROVAL_REQUESTED,
                            actor_type="foundry",
                        )
                    )
                elif status is RunStatus.BLOCKED:
                    # Two routes to BLOCKED at intake: the work couldn't be scoped
                    # to a repo (unroutable), or the policy gate denies it no matter
                    # who approves (policy_denied). Record the decision in the latter
                    # case so the trail shows *why* approval was never offered.
                    if risk.overall_risk is OverallRisk.BLOCKED:
                        session.add(
                            build_audit_event(
                                run_id=run_id,
                                event_type=AuditEventType.RUN_BLOCKED,
                                actor_type="foundry",
                                metadata={"category": "unroutable"},
                            )
                        )
                    else:
                        session.add(
                            build_audit_event(
                                run_id=run_id,
                                event_type=AuditEventType.RUN_BLOCKED,
                                actor_type="foundry",
                                output_content=decision,
                                metadata={"category": "policy_denied"},
                            )
                        )
                self._record_outcome_if_terminal(session, run)
                session.commit()
        except IntegrityError:
            # Lost an intake race: a concurrent delivery for this issue
            # committed its run first and the one-active-run-per-issue index
            # refused ours (everything above rolled back as one transaction).
            # Attach to the surviving run; the winner already owns the tracker
            # write-back.
            existing = self.find_active_run_id_for_issue(ticket.issue_id)
            if existing is None:
                raise
            _log.info(
                "duplicate intake for issue %s lost the race; attaching to "
                "active run %s",
                ticket.issue_id,
                existing,
            )
            return existing

        # The *effective* approval roles a human must hold = the risk-derived
        # roles unioned with any per-repo roles configured for the routed repo
        # (issue #31). Surfaced in the tracker/chat approval prompts so an
        # approver is told exactly which roles to sign with, matching what
        # approve() and the gate require - no approve->blocked whiplash.
        repo_name = context.best_repository.repo if context.best_repository else None
        effective_roles = [
            r.value
            for r in required_approvals(
                PolicyRisk(
                    overall_risk=risk.overall_risk,
                    **risk.sensitive_areas.model_dump(),
                ),
                self._repo_required_roles_for(repo_name),
            )
        ]
        # The effective N-of-M count for the routed repo (issue #31). Surfaced in
        # the approval prompts alongside the roles so an approver is told up front
        # that one sign-off may not be enough - matching what approve() enforces,
        # so the "two-person rule" never surprises the first approver as a run
        # that stays parked after they sign off.
        min_required = self._min_approvals_for(repo_name)
        # Mirror the outcome back to the tracker (Linear) if one is configured.
        if self._tracker is not None:
            try:
                self._tracker.post_comment(
                    ticket.issue_id,
                    format_analysis_comment(
                        analysis,
                        risk,
                        plan,
                        status,
                        required_roles=effective_roles,
                        min_approvals=min_required,
                    ),
                )
                self._tracker.set_state(ticket.issue_id, state_for(status))
            except Exception:
                _log.exception(
                    "tracker write-back failed for issue %s; Foundry state is "
                    "authoritative but Linear may be stale",
                    ticket.issue_id,
                )
        # Mirror the same outcome onto the chat surface: an actionable approval
        # message when the run parks for approval, else a status notification.
        if status is RunStatus.WAITING_APPROVAL:
            self._notify_approval(
                ticket,
                analysis,
                risk,
                plan,
                required_roles=effective_roles,
                min_approvals=min_required,
            )
        else:
            self._notify_run_status(ticket.issue_id, ticket.issue_key, status)
        return run_id

    @traced("foundry.intake_epic")
    def intake_epic(
        self,
        ticket: RawTicket,
        *,
        trigger_type: str,
        created_by: str | None = None,
    ) -> EpicIntakeResult:
        """Decompose an epic ticket into a parent run + per-repo child runs.

        The *producer* half of the parent/child run model (issue #35): a ticket
        that spans several repositories is split (by the configured
        :class:`~foundry.engines.decomposition.EpicDecomposer` - deterministic by
        default, or the LLM-assisted decomposer behind ``decomposition.provider``)
        into one independently gated child run per repo, grouped under a single
        parent run for the epic ticket itself.

        Each child is an ordinary :meth:`intake_and_plan` run linked via
        ``parent_run_id`` - it is analysed, risk-classified, planned and
        policy-gated on its own, parks for its own approval, and rolls up into
        the epic's status. **No gate is weakened**: this only opens more
        normal runs through the existing path.

        A ticket that does not decompose (fewer than two distinct repos) degrades
        to a single ordinary run with no children, so a caller can always route
        through ``intake_epic`` safely. The parent run is created first (and
        committed) so the children's ``parent_run_id`` validation finds a
        root; if the parent loses the one-active-run-per-issue race or is
        otherwise rejected, the error surfaces before any child is created.
        """
        decomposition = self._decomposer.decompose(ticket)
        parent_run_id = self.intake_and_plan(
            ticket, trigger_type=trigger_type, created_by=created_by
        )
        if not decomposition.is_epic:
            return EpicIntakeResult(
                parent_run_id=parent_run_id, decomposition=decomposition
            )
        child_run_ids: list[str] = []
        for child in decomposition.children:
            child_run_ids.append(
                self.intake_and_plan(
                    child,
                    trigger_type=trigger_type,
                    created_by=created_by,
                    parent_run_id=parent_run_id,
                )
            )
        return EpicIntakeResult(
            parent_run_id=parent_run_id,
            child_run_ids=child_run_ids,
            decomposition=decomposition,
        )

    def _notify_state(self, issue_id: str, status: RunStatus) -> None:
        if self._tracker is not None:
            try:
                self._tracker.set_state(issue_id, state_for(status))
            except Exception:
                _log.exception(
                    "tracker state update failed for issue %s (-> %s)",
                    issue_id,
                    status.value,
                )
        # The intake path notifies chat itself (it has the full plan context for
        # the approval message); every later transition flows through here.
        self._notify_run_status(issue_id, None, status)

    def _notify_run_status(
        self, issue_id: str, issue_key: str | None, status: RunStatus
    ) -> None:
        """Post a status update to the chat surface for notable transitions."""
        if self._notifier is None or status not in _NOTIFIABLE_STATUSES:
            return
        try:
            self._notifier.status_changed(issue_id, issue_key, status)
        except Exception:
            _log.exception(
                "notifier status update failed for issue %s (-> %s)",
                issue_id,
                status.value,
            )

    def _notify_approval(
        self,
        ticket: RawTicket,
        analysis: TicketAnalysis,
        risk: RiskAssessment,
        plan: DeliveryPlan,
        *,
        required_roles: list[str] | None = None,
        min_approvals: int = 1,
    ) -> None:
        """Post the interactive approval message to the chat surface.

        ``required_roles`` is the *effective* approval roles (risk-derived plus
        any per-repo roles, issue #31); it falls back to the risk-derived roles
        for callers that don't compute the union. ``min_approvals`` is the
        effective N-of-M count (issue #31), surfaced in the message only when >1.
        """
        if self._notifier is None:
            return
        repo = plan.affected_repositories[0] if plan.affected_repositories else "unknown"
        roles = (
            tuple(required_roles)
            if required_roles is not None
            else tuple(r.value for r in risk.required_approvals)
        )
        request = ApprovalRequest(
            issue_id=ticket.issue_id,
            issue_key=ticket.issue_key,
            title=analysis.title,
            work_type=analysis.work_type.value,
            risk=risk.overall_risk.value,
            agent_mode=risk.allowed_agent_mode.value,
            repo=repo,
            acceptance_criteria=tuple(analysis.acceptance_criteria),
            required_approvals=roles,
            min_approvals=min_approvals,
        )
        try:
            self._notifier.approval_requested(request)
        except Exception:
            _log.exception(
                "notifier approval message failed for issue %s", ticket.issue_id
            )

    def _notify_approval_progress(
        self,
        issue_id: str,
        issue_key: str | None,
        *,
        collected: int,
        required: int,
        last_approver: str,
    ) -> None:
        """Nudge the next approver after a partial N-of-M sign-off (issue #31).

        The first approval prompt tells approvers up front that the run needs
        several *distinct* sign-offs, but once one lands the run sits parked at
        ``awaiting_approval (1/2)`` with nothing telling the next approver to
        act. This posts a short progress nudge to the tracker and the chat
        surface so a two-person-rule run doesn't go silent between sign-offs.

        Presentation/notification-only and best-effort, exactly like every other
        notification: it never advances, blocks, or releases a run (the count
        check above is the gate), and a tracker/chat outage must never break the
        approval path.
        """
        self._notify_comment(
            issue_id,
            format_approval_progress_comment(
                collected=collected,
                required=required,
                last_approver=last_approver,
            ),
        )
        if self._notifier is None:
            return
        try:
            self._notifier.approval_progress(
                ApprovalProgress(
                    issue_id=issue_id,
                    issue_key=issue_key,
                    collected=collected,
                    required=required,
                    last_approver=last_approver,
                )
            )
        except Exception:
            _log.exception(
                "notifier approval-progress message failed for issue %s", issue_id
            )

    def _notify_comment(self, issue_id: str, body: str) -> None:
        if self._tracker is not None:
            try:
                self._tracker.post_comment(issue_id, body)
            except Exception:
                _log.exception("tracker comment failed for issue %s", issue_id)

    @staticmethod
    def _post_plan_status(
        analysis: TicketAnalysis,
        risk: RiskAssessment,
        policy_permanently_blocked: bool,
    ) -> RunStatus:
        # Readiness first: an unclear ticket should be clarified before we worry
        # about anything downstream (it usually also lacks a resolvable repo).
        if not analysis.is_ready_to_build:
            return RunStatus.NEEDS_CLARIFICATION
        # The ticket is clear, but the work still can't be scoped to a repo.
        if risk.overall_risk is OverallRisk.BLOCKED:
            return RunStatus.BLOCKED
        # The plan is ready and scoped, but the policy gate will refuse dispatch
        # no matter who approves it. Park at BLOCKED rather than inviting an
        # approval that dispatch would only convert to BLOCKED anyway.
        if policy_permanently_blocked:
            return RunStatus.BLOCKED
        # A ready, scoped plan awaits human approval before any agent runs.
        return RunStatus.WAITING_APPROVAL

    # -- approval -------------------------------------------------------------

    def approve(
        self, run_id: str, *, user: str, granted_roles: set[ApprovalRole] | None = None
    ) -> None:
        granted_roles = granted_roles or set()
        with self._sf() as session:
            run = self._require_run(session, run_id, lock=True)
            if run.status is not RunStatus.WAITING_APPROVAL:
                raise OrchestratorError(
                    f"run {run_id} is '{run.status.value}', not awaiting approval"
                )
            # Pre-validate the approver's roles against what this run's risk
            # actually requires. Recording an approval the policy gate will only
            # refuse at dispatch writes a *void* APPROVAL_GRANTED into the audit
            # trail and shows tracker users an approve->blocked whiplash (issue
            # #18). Refuse up front, before anything is written, so the timeline
            # never shows an approval for work that was actually denied.
            repo = self._run_repo(session, run_id)
            risk = self._load(session, run_id, ArtifactType.RISK_ASSESSMENT)
            required = required_approvals(
                PolicyRisk(
                    overall_risk=risk.overall_risk,
                    **risk.sensitive_areas.model_dump(),
                ),
                # The routed repo (issue #31) and any fired custom risk category
                # (issue #155) may demand extra roles. Refuse an approver lacking
                # them here, before recording, just as for the risk-derived roles
                # - so a per-repo or custom-category rule never surfaces as an
                # approve->blocked whiplash at dispatch.
                self._resolved_required_roles(repo, risk),
            )
            missing = [role for role in required if role not in granted_roles]
            if missing:
                raise OrchestratorError(
                    f"approval refused: '{user}' lacks the required role(s) "
                    f"({', '.join(role.value for role in missing)}) for this run; "
                    f"required: {', '.join(role.value for role in required)}"
                )
            # N-of-M approval matrix (issue #31): a run may need several *distinct*
            # human approvers before it advances. Refuse a duplicate sign-off from
            # someone who already approved, so the distinct-approver count - and
            # the audit trail - stay honest rather than being inflated by one
            # person clicking twice.
            prior_users, _ = self._approval_summary(session, run_id)
            if user in prior_users:
                raise OrchestratorError(
                    f"approval refused: '{user}' has already approved run {run_id}"
                )
            approval_record = {
                "user": user,
                "granted_roles": sorted(r.value for r in granted_roles),
            }
            self._add(
                session, run_id, ArtifactType.APPROVAL_RECORD, approval_record
            )
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.APPROVAL_GRANTED,
                    actor_type="human",
                    actor_id=user,
                    output_content=approval_record,
                )
            )
            # Count this approver among the distinct sign-offs and only advance
            # the run once the effective minimum is met. Until then the run stays
            # at WAITING_APPROVAL, accumulating approvals - the human gate is a
            # one-way ratchet towards stricter, never weakened (invariant #1).
            approver_count = len(prior_users) + 1
            min_required = self._min_approvals_for(repo)
            issue_id = run.linear_issue_id
            issue_key = run.linear_issue_key
            if approver_count >= min_required:
                run.status = RunStatus.APPROVED
                run.approved_by = user
                run.approved_at = datetime.now(timezone.utc)
                run.current_step = "approved"
                session.commit()
                self._notify_state(issue_id, RunStatus.APPROVED)
            else:
                # Still short of the required sign-offs: record progress on the
                # run so the timeline/dashboard shows "1 of 2", and leave the run
                # parked for the next approver. The status has not changed, so we
                # send no *state-change* notification (which would re-spam the
                # original prompt); instead we send a dedicated progress nudge so
                # the next approver is told one sign-off won't release the run,
                # rather than the run going silent at "(1/2)" (issue #31).
                run.current_step = (
                    f"awaiting_approval ({approver_count}/{min_required})"
                )
                session.commit()
                self._notify_approval_progress(
                    issue_id,
                    issue_key,
                    collected=approver_count,
                    required=min_required,
                    last_approver=user,
                )

    def reject(self, run_id: str, *, user: str) -> None:
        """Terminate a run a human declined (from ``/foundry reject``)."""
        self._terminate(
            run_id,
            user=user,
            status=RunStatus.REJECTED,
            event_type=AuditEventType.APPROVAL_REJECTED,
        )

    def stop(self, run_id: str, *, user: str) -> None:
        """Halt a run a human stopped (from ``/foundry stop``)."""
        self._terminate(
            run_id,
            user=user,
            status=RunStatus.BLOCKED,
            event_type=AuditEventType.RUN_BLOCKED,
        )

    def _terminate(
        self,
        run_id: str,
        *,
        user: str,
        status: RunStatus,
        event_type: AuditEventType,
    ) -> None:
        with self._sf() as session:
            run = self._require_run(session, run_id, lock=True)
            self._refuse_if_terminal(run)
            run.status = status
            run.current_step = status.value
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=event_type,
                    actor_type="human",
                    actor_id=user,
                    metadata=(
                        {"category": "human_stopped"}
                        if event_type is AuditEventType.RUN_BLOCKED
                        else None
                    ),
                )
            )
            # "Stop"/"reject" must also stop the agent working and spending, not
            # just stop Foundry listening. Best-effort: a provider that can't
            # cancel never blocks the run's terminal transition.
            self._cancel_active_job(session, run_id, user=user)
            issue_id = run.linear_issue_id
            self._record_outcome_if_terminal(session, run)
            session.commit()
        self._notify_state(issue_id, status)

    def expire_pending_approval(self, run_id: str) -> RunStatus:
        """Terminate a run whose approval window elapsed without a decision.

        Called by the durable Temporal driver when ``wait_condition`` for the
        approval signal times out: instead of letting the workflow fail and
        strand the run at ``WAITING_APPROVAL`` with no audit row, transition it
        cleanly to ``BLOCKED`` with a ``run.blocked`` event tagged
        ``approval_window_expired``. Idempotent — a run that has since been
        approved/rejected (the signal raced the timeout, or the activity is
        being retried) is left untouched and its current status returned.
        """
        return self._expire_wait(
            run_id,
            expected=RunStatus.WAITING_APPROVAL,
            status=RunStatus.BLOCKED,
            event_type=AuditEventType.RUN_BLOCKED,
            category="approval_window_expired",
        )

    def expire_pending_pr(self, run_id: str) -> RunStatus:
        """Terminate a dispatched run whose PR never arrived in the wait window.

        The durable counterpart to a silently-stranded ``AGENT_RUNNING`` run:
        the agent was dispatched but produced no PR within the workflow's PR
        window, so the run failed to deliver. Transition to ``EXECUTION_FAILED``
        with an ``agent.failed`` event (consistent with a provider that raises)
        and cancel any in-flight job so it stops spending. Idempotent — a run
        that has since opened a PR (or already terminated) is left untouched.
        """
        return self._expire_wait(
            run_id,
            expected=RunStatus.AGENT_RUNNING,
            status=RunStatus.EXECUTION_FAILED,
            event_type=AuditEventType.AGENT_FAILED,
            category="pr_window_expired",
        )

    def _expire_wait(
        self,
        run_id: str,
        *,
        expected: RunStatus,
        status: RunStatus,
        event_type: AuditEventType,
        category: str,
    ) -> RunStatus:
        with self._sf() as session:
            run = self._require_run(session, run_id, lock=True)
            if run.status is not expected:
                # The awaited signal won the race, or this expiry is a retry:
                # nothing to do, never overwrite a run that already moved on.
                return run.status
            run.status = status
            run.current_step = status.value
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=event_type,
                    actor_type="foundry",
                    metadata={"category": category},
                )
            )
            # A dispatched-but-undelivered run may still have a job spending;
            # cancel it best-effort exactly as a human stop would.
            self._cancel_active_job(session, run_id, user="foundry")
            issue_id = run.linear_issue_id
            self._record_outcome_if_terminal(session, run)
            session.commit()
        self._notify_state(issue_id, status)
        return status

    def fail_run(self, run_id: str, *, reason: str | None = None) -> RunStatus:
        """Compensation for an irrecoverable durable-workflow error (issue #37).

        When a Temporal activity exhausts its retry budget (a non-retryable
        deterministic error, or ``maximum_attempts`` reached on a transient
        one) the workflow fails. Without compensation the run row is left in
        whatever *active* state it last reached (e.g. ``AGENT_RUNNING`` after a
        crashing ``record_pr``) - active forever, never recorded as an outcome,
        and silently distorting the fleet snapshot and the routing priors. This
        is the durable workflow's last-resort compensation activity: it
        transitions any still-active run to ``EXECUTION_FAILED`` with an audited
        ``agent.failed`` event (consistent with ``expire_pending_pr`` and a
        provider that raises), cancels any in-flight job so a stranded run stops
        spending, and records the terminal outcome.

        Idempotent and terminal-safe: a run that already finished - including a
        sticky forbidden-path ``BLOCKED`` (AGENTS.md invariant #7), a human
        ``stop``/``reject``, or a delivered PR - is left exactly as it is and
        its current status returned, so re-running this compensation activity
        under Temporal's at-least-once delivery never overwrites a real terminal
        state.
        """
        with self._sf() as session:
            run = self._require_run(session, run_id, lock=True)
            if run.status in TERMINAL_RUN_STATUSES:
                # Already finished (including a sticky BLOCKED or a delivered
                # PR's COMPLETE): never overwrite the single recorded outcome.
                return run.status
            run.status = RunStatus.EXECUTION_FAILED
            run.current_step = RunStatus.EXECUTION_FAILED.value
            metadata: dict[str, str] = {"category": "workflow_irrecoverable_error"}
            if reason:
                # Bound the free-text reason so a verbose error never bloats the
                # audit row; the category is the stable, queryable field.
                metadata["reason"] = reason[:500]
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.AGENT_FAILED,
                    actor_type="foundry",
                    metadata=metadata,
                )
            )
            # A run that crashed mid-flight may still have a job spending;
            # cancel it best-effort exactly as a human stop / PR expiry would.
            self._cancel_active_job(session, run_id, user="foundry")
            issue_id = run.linear_issue_id
            self._record_outcome_if_terminal(session, run)
            session.commit()
        self._notify_state(issue_id, RunStatus.EXECUTION_FAILED)
        return RunStatus.EXECUTION_FAILED

    def _provider_for(self, name: str | None) -> CodingAgentProvider | None:
        """The configured provider that owns a recorded job, or ``None`` if foreign.

        Cost refresh, cancellation and remediation key off the *recorded*
        ``job.provider`` rather than a single configured provider, so a run is
        always reconciled against the agent that actually ran it - even under
        ``agent.provider: auto`` where different runs use different providers. A
        provider name we no longer have configured is "not ours" (returns
        ``None``), exactly as the old single-provider guard skipped a foreign
        ``provider_job_id``.
        """
        if name is None:
            return None
        return self._providers.get(name)

    def _select_dispatch_provider(
        self, session, *, analysis: TicketAnalysis, context: ContextBundle
    ) -> tuple[CodingAgentProvider, dict | None]:
        """Pick the provider for a *first* dispatch (issue #33).

        Default (``agent.provider`` is a single agent, ``auto_dispatch`` off):
        the configured provider, with no selection metadata - byte-for-byte the
        previous behaviour, and no scorecard query at all.

        With ``agent.provider: auto`` on: the scorecard-recommended provider over
        the configured ``auto_candidates`` for this run's work-type/repo, honouring
        the same min-sample floor and majority-merged gate ``recommend_provider``
        already enforces. When no agent has earned a recommendation yet, it falls
        back to the configured default provider - so a fresh deployment with no
        history behaves predictably, and the kill switch is simply *not* setting
        ``provider: auto``. The decision is returned as an explainable ``selection``
        dict recorded on the ``AGENT_STARTED`` event, mirroring how repo routing
        records its reason string - so a human can always see *why* this agent ran.
        """
        if not self._auto_dispatch:
            return self._provider, None
        work_type = analysis.work_type.value if analysis else None
        repo = (
            context.best_repository.repo
            if context and context.best_repository
            else None
        )
        rec = recommend_provider(
            session,
            work_type=work_type,
            repo=repo,
            candidates=list(self._auto_candidates),
            min_samples=self._auto_min_samples,
        )
        chosen = rec["recommended"]
        provider = self._providers.get(chosen) if chosen else None
        if provider is not None:
            selection = {
                "mode": "auto",
                "selected_by": "scorecard",
                "provider": provider.name,
                "scope": rec["scope"],
                "reason": rec["reason"],
            }
            return provider, selection
        # No eligible agent yet (thin/losing history): fall back to the default.
        selection = {
            "mode": "auto",
            "selected_by": "fallback",
            "provider": self._provider.name,
            "scope": rec["scope"],
            "reason": rec["reason"],
        }
        return self._provider, selection

    def _cancel_active_job(self, session, run_id: str, *, user: str) -> None:
        """Cancel the run's in-flight provider job when a human ends the run.

        Only the latest job is cancellable, and only while it is still
        ``CREATED``/``RUNNING`` and was launched by a *configured* provider
        (resolved by its recorded ``job.provider``; mirrors
        :meth:`_refresh_job_costs`: a foreign ``provider_job_id`` is not ours to
        cancel). Failure-isolated exactly like tracker write-back - a provider
        whose cancel call raises still leaves the run blocked, with the failure
        recorded on the ``AGENT_CANCELLED`` audit event for the trail.
        """
        job = (
            session.query(FoundryAgentJob)
            .filter(FoundryAgentJob.run_id == run_id)
            .order_by(FoundryAgentJob.started_at.desc())
            .first()
        )
        if job is None or job.status not in (
            AgentJobStatus.CREATED,
            AgentJobStatus.RUNNING,
        ):
            return
        provider = self._provider_for(job.provider)
        if not job.provider_job_id or provider is None:
            return

        error: str | None = None
        try:
            provider.cancel_job(job.provider_job_id)
        except Exception as exc:  # never let cancellation break the termination
            error = str(exc) or exc.__class__.__name__
            _log.exception(
                "provider cancel failed for job %s (run %s); run still blocked",
                job.id,
                run_id,
            )

        if error is None:
            job.status = AgentJobStatus.CANCELLED
            job.completed_at = datetime.now(timezone.utc)
        else:
            job.error = error

        metadata = {
            "provider": job.provider,
            "job_id": job.provider_job_id,
            "cancelled": error is None,
            "requested_by": user,
        }
        if error is not None:
            metadata["error"] = error
        session.add(
            build_audit_event(
                run_id=run_id,
                event_type=AuditEventType.AGENT_CANCELLED,
                actor_type="foundry",
                metadata=metadata,
            )
        )

    def _cancel_superseded_job(self, run_id: str, job: CodingAgentJob, job_row) -> None:
        """Cancel a job we launched for a run that has since left our control.

        A (re)dispatch commits its gate decision, calls the provider, then
        records the job in a third transaction. If a human ``stop``/``reject``
        wins the run's row lock in that window, the agent is live but unwanted.
        Best-effort cancel it (failure-isolated like :meth:`_cancel_active_job`)
        and reflect the outcome on the freshly-recorded ``job_row`` so the trail
        is honest about what ran and what we did about it."""
        provider = self._provider_for(job.provider)
        if provider is None or not job.job_id:
            return
        try:
            provider.cancel_job(job.job_id)
            job_row.status = AgentJobStatus.CANCELLED
            job_row.completed_at = datetime.now(timezone.utc)
        except Exception as exc:  # never let cancellation break the commit
            job_row.error = str(exc) or exc.__class__.__name__
            _log.exception(
                "cancel of superseded job %s (run %s) failed; it may keep running",
                job.job_id,
                run_id,
            )

    # -- read helpers (used by the API) ---------------------------------------

    def find_run_id_for_issue(self, linear_issue_id: str) -> str | None:
        with self._sf() as session:
            run = (
                session.query(FoundryRun)
                .filter(FoundryRun.linear_issue_id == linear_issue_id)
                .order_by(FoundryRun.created_at.desc())
                .first()
            )
            return run.id if run else None

    def find_active_run_id_for_issue(self, linear_issue_id: str) -> str | None:
        """The in-flight run for an issue, if any (None means restartable)."""
        with self._sf() as session:
            run = (
                session.query(FoundryRun)
                .filter(
                    FoundryRun.linear_issue_id == linear_issue_id,
                    FoundryRun.status.in_(ACTIVE_RUN_STATUSES),
                )
                .order_by(FoundryRun.created_at.desc())
                .first()
            )
            return run.id if run else None

    def find_run_id_for_branch(self, branch: str) -> str | None:
        """Associate an observed PR back to its run via the agent job's branch.

        Only runs in a PR-observable state match, mirroring the issue-key
        fallback in :meth:`correlate_pr`: a stale or terminal run that once used
        this branch must not be revived by a late webhook (``record_pr`` would
        reject it anyway, but filtering here keeps the contract honest and lets
        the issue-key fallback still find a live run on the same branch name).
        """
        if not branch:
            return None
        with self._sf() as session:
            job = (
                session.query(FoundryAgentJob)
                .join(FoundryRun, FoundryRun.id == FoundryAgentJob.run_id)
                .filter(FoundryAgentJob.branch == branch)
                .filter(FoundryRun.status.in_(_PR_OBSERVABLE_STATUSES))
                .order_by(FoundryAgentJob.started_at.desc())
                .first()
            )
            return job.run_id if job else None

    def correlate_pr(self, pr_state: PullRequestState) -> str | None:
        """Find the run an observed PR belongs to.

        Exact branch match first (direct providers control the branch name).
        Falls back to Linear issue keys found in the branch name or PR title,
        because delegated agents (e.g. Cursor via Linear) choose their own
        branch names but embed the issue key. Only runs in a PR-observable
        state are matched, so stale runs for the same issue are not revived.
        """
        run_id = self.find_run_id_for_branch(pr_state.branch)
        if run_id is not None:
            return run_id

        keys: list[str] = []
        for text in (pr_state.branch, pr_state.title):
            keys.extend(m.upper() for m in _ISSUE_KEY_RE.findall(text or ""))
        if not keys:
            return None
        with self._sf() as session:
            for key in dict.fromkeys(keys):  # de-dup, preserve order
                run = (
                    session.query(FoundryRun)
                    .filter(
                        FoundryRun.linear_issue_key == key,
                        FoundryRun.status.in_(_PR_OBSERVABLE_STATUSES),
                    )
                    .order_by(FoundryRun.created_at.desc())
                    .first()
                )
                if run is not None:
                    return run.id
        return None

    def get_run(self, run_id: str) -> FoundryRun | None:
        with self._sf() as session:
            return session.get(FoundryRun, run_id)

    def list_runs(self) -> list[FoundryRun]:
        with self._sf() as session:
            return list(session.query(FoundryRun).order_by(FoundryRun.created_at).all())

    # -- epics (parent/child runs) -------------------------------------------

    def _validate_epic_parent(self, parent_run_id: str) -> None:
        """Reject linking to a missing parent or nesting epics (issue #35).

        Epics are a single level in v1: a parent must exist and must itself be
        a root, so a child can never also be an epic. Raised before the
        analysis pipeline runs so a bad linkage fails fast.
        """
        with self._sf() as session:
            parent = session.get(FoundryRun, parent_run_id)
            if parent is None:
                raise OrchestratorError(
                    f"parent run {parent_run_id} does not exist"
                )
            if parent.parent_run_id is not None:
                raise OrchestratorError(
                    f"run {parent_run_id} is itself a child; epics are a "
                    "single level (no nesting)"
                )

    def child_runs(self, run_id: str) -> list[FoundryRun]:
        """The child runs decomposed from ``run_id``, oldest first."""
        with self._sf() as session:
            return list(
                session.query(FoundryRun)
                .filter(FoundryRun.parent_run_id == run_id)
                .order_by(FoundryRun.created_at)
                .all()
            )

    def list_epics(self) -> list[FoundryRun]:
        """Every epic *root* run, oldest first (issue #35).

        An epic is a parent run that other runs point at via ``parent_run_id``;
        an ordinary single-repo run (one with no children) is not an epic and is
        omitted. Resolving children for the rollup is left to :meth:`child_runs`
        / :func:`epics.compute_epic_rollup` so the lifecycle logic lives in one
        place. Because epics are a single level (the orchestrator refuses to
        nest), every referenced parent is itself a root.
        """
        with self._sf() as session:
            parent_ids = [
                pid
                for (pid,) in session.query(FoundryRun.parent_run_id)
                .filter(FoundryRun.parent_run_id.isnot(None))
                .distinct()
                .all()
            ]
            if not parent_ids:
                return []
            return list(
                session.query(FoundryRun)
                .filter(FoundryRun.id.in_(parent_ids))
                .order_by(FoundryRun.created_at)
                .all()
            )

    def epic_root_id(self, run_id: str) -> str | None:
        """Resolve the epic root for ``run_id``: its parent if it is a child,
        else itself. ``None`` if the run does not exist.
        """
        run = self.get_run(run_id)
        if run is None:
            return None
        return run.parent_run_id or run.id

    def epic_rollup(self, run_id: str) -> dict:
        """Rolled-up epic status + child summary for ``run_id``'s epic.

        Resolves the epic root (so calling on a child returns the whole epic),
        then summarises its children via :func:`epics.compute_epic_rollup`.
        """
        children = self.child_runs(run_id)
        return compute_epic_rollup(c.status for c in children)

    # -- agent dispatch -------------------------------------------------------

    @traced("foundry.dispatch_agent")
    def dispatch_agent(self, run_id: str) -> CodingAgentJob:
        """Re-check policy with the recorded approvals, then launch the provider.

        The dispatch is split into three phases so the audit trail can never be
        out of step with reality (issue #13):

        1. evaluate the gate and **commit** the policy decision *before* any
           provider side effect — a recorded authorisation must not be able to
           vanish just because the provider call later fails;
        2. call the provider; a failure here is captured as an ``AGENT_FAILED``
           event in its own transaction rather than silently rolling back;
        3. record the now-live job and flip the run to ``AGENT_RUNNING``.
        """
        # -- phase 1: gate decision, durably recorded ------------------------
        with self._sf() as session:
            run = self._require_run(session, run_id, lock=True)
            if run.status is not RunStatus.APPROVED:
                raise OrchestratorError(
                    f"run {run_id} is '{run.status.value}', not approved"
                )
            analysis = self._load(session, run_id, ArtifactType.TICKET_ANALYSIS)
            context = self._load(session, run_id, ArtifactType.CONTEXT_BUNDLE)
            risk = self._load(session, run_id, ArtifactType.RISK_ASSESSMENT)
            plan = self._load(session, run_id, ArtifactType.DELIVERY_PLAN)
            ticket = self._load(session, run_id, ArtifactType.TICKET_SNAPSHOT)
            # Aggregate every recorded approval (N-of-M, issue #31): the granted
            # roles are the union across all distinct approvers, and the run is
            # "approved" iff at least one human signed off. With the default
            # single-approval lifecycle there is exactly one record, so this is
            # byte-for-byte the previous behaviour.
            approval_users, granted = self._approval_summary(session, run_id)
            approval_present = bool(approval_users)
            # Enforce the budget cap at first dispatch too (issue #29): no job
            # has spent yet, so the projected cost is just this dispatch's
            # estimate - enough to refuse a run whose single attempt already
            # exceeds the cap.
            self._refresh_job_costs(session, run_id)
            payload = self._policy_input(
                PolicyAction.START_AGENT,
                analysis,
                context,
                risk,
                approvals=granted,
                # An approval record exists iff a human approved this run; the
                # gate now requires it for any autonomous action (issue #18). A
                # missing record (a path that bypassed approve()) is denied here
                # rather than slipping through ungoverned. The N-of-M count is
                # enforced in the lifecycle (a run only reaches APPROVED once the
                # minimum distinct approvers signed off), so by the time dispatch
                # runs the count is already satisfied.
                approval_present=approval_present,
                budget=PolicyBudget(
                    cost_usd=self._accumulated_cost(session, run_id),
                    pending_cost_usd=self._estimated_cost_per_dispatch,
                    max_cost_usd=self._max_cost_per_run,
                ),
            )
            decision = self._policy.evaluate(payload)
            session.add(
                build_policy_decision_row(
                    run_id=run_id, payload=payload, decision=decision
                )
            )
            if not decision.allowed or decision.allowed_agent_mode is AgentMode.HUMAN_ONLY:
                run.status = RunStatus.BLOCKED
                session.add(
                    build_audit_event(
                        run_id=run_id,
                        event_type=AuditEventType.RUN_BLOCKED,
                        actor_type="foundry",
                        output_content=decision,
                        metadata={"category": "policy_denied"},
                    )
                )
                blocked_issue = run.linear_issue_id
                self._record_outcome_if_terminal(session, run)
                session.commit()
                self._notify_state(blocked_issue, RunStatus.BLOCKED)
                raise OrchestratorError(
                    "policy gate blocked agent dispatch: " + "; ".join(decision.reasons)
                )

            job_input = self._build_job_input(run_id, ticket, plan, context)
            # Choose the agent for this first dispatch (issue #33): the configured
            # provider by default, or the scorecard pick under agent.provider:
            # auto. A pure read, inside the gate transaction; nothing dispatches
            # here.
            provider, selection = self._select_dispatch_provider(
                session, analysis=analysis, context=context
            )
            issue_id = run.linear_issue_id
            # Commit the allow decision now; the provider call in phase 2 is a
            # side effect that must not be able to erase the recorded gate row.
            session.commit()

        # -- phase 2: the external side effect -------------------------------
        # A dispatch that never even starts the agent ends the run (failed runs
        # are re-triggerable by a fresh intake).
        job = self._dispatch_to_provider(
            run_id,
            job_input,
            provider=provider,
            failure_status=RunStatus.EXECUTION_FAILED,
        )

        # -- phase 3: record the running job ---------------------------------
        with self._sf() as session:
            run = self._require_run(session, run_id, lock=True)
            job_row = FoundryAgentJob(
                id=new_id("job"),
                run_id=run_id,
                provider=job.provider,
                provider_job_id=job.job_id,
                status=AgentJobStatus.RUNNING,
                repo=job_input.repo,
                branch=job_input.branch_name,
                started_at=datetime.now(timezone.utc),
            )
            session.add(job_row)
            started_metadata = {"provider": job.provider, "job_id": job.job_id}
            if selection is not None:
                # Learned dispatch (issue #33): record *why* this agent ran so the
                # routing is auditable, like repo routing's reason string.
                started_metadata["selection"] = selection
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.AGENT_STARTED,
                    actor_type="foundry",
                    metadata=started_metadata,
                )
            )
            # A human stop()/reject() can win the row lock between phase 1's
            # commit and here. The provider job is already live, but the run is
            # no longer ours to advance: record the job for the audit/cost trail,
            # cancel it so a stopped run stops spending, and never overwrite the
            # new status ("blocked stays blocked", issue #10).
            if run.status is RunStatus.APPROVED:
                run.status = RunStatus.AGENT_RUNNING
                run.current_step = "agent_running"
                final_status = RunStatus.AGENT_RUNNING
            else:
                self._cancel_superseded_job(run_id, job, job_row)
                final_status = run.status
                _log.warning(
                    "dispatch of run %s launched job %s but the run is now '%s'; "
                    "leaving the status unchanged and cancelling the job",
                    run_id,
                    job.job_id,
                    run.status.value,
                )
            try:
                session.commit()
            except Exception:
                # The provider job is already live; surface the orphan loudly so
                # it is discoverable for reconciliation (issue #13).
                _log.exception(
                    "dispatch of run %s created provider job %s (%s) but the "
                    "job/audit commit failed; the agent is running without a "
                    "DB record",
                    run_id,
                    job.job_id,
                    job.provider,
                )
                raise
        self._notify_state(issue_id, final_status)
        return job

    def _dispatch_to_provider(
        self,
        run_id: str,
        job_input: CodingAgentJobInput,
        *,
        provider: CodingAgentProvider,
        failure_status: RunStatus,
    ) -> CodingAgentJob:
        """Call the provider, recording an ``AGENT_FAILED`` event if it raises.

        The provider call is the one external side effect of a dispatch. Wrapping
        it here guarantees a provider exception leaves an audit trail and moves
        the run to a definite status, instead of rolling back the surrounding
        transaction and stranding the run with a recorded gate decision but no
        agent and no failure event (issue #13). The original exception is
        re-raised so callers see the failure unchanged. ``provider`` is the agent
        selected for this dispatch (issue #33) - the configured one by default,
        or the scorecard pick under ``agent.provider: auto``.
        """
        try:
            return provider.create_job(job_input)
        except Exception as exc:
            self._record_dispatch_failure(run_id, failure_status, reason=str(exc))
            raise

    def _record_dispatch_failure(
        self, run_id: str, failure_status: RunStatus, *, reason: str
    ) -> None:
        """Persist an ``AGENT_FAILED`` event for a provider dispatch that raised.

        Runs in its own transaction so the record survives the exception the
        caller is about to re-raise.
        """
        with self._sf() as session:
            run = self._require_run(session, run_id, lock=True)
            run.status = failure_status
            run.current_step = "dispatch_failed"
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.AGENT_FAILED,
                    actor_type="foundry",
                    metadata={"reason": "provider dispatch failed", "error": reason},
                )
            )
            issue_id = run.linear_issue_id
            self._record_outcome_if_terminal(session, run)
            session.commit()
        self._notify_state(issue_id, failure_status)

    # -- PR monitoring --------------------------------------------------------

    def mark_agent_failed(self, run_id: str, *, reason: str = "agent error") -> None:
        """Mark a run as failed when the agent crashes without creating a PR."""
        with self._sf() as session:
            run = self._require_run(session, run_id, lock=True)
            self._refuse_if_terminal(run)
            run.status = RunStatus.EXECUTION_FAILED
            run.current_step = "failed"
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.AGENT_FAILED,
                    actor_type="foundry",
                    metadata={"reason": reason},
                )
            )
            job = (
                session.query(FoundryAgentJob)
                .filter(FoundryAgentJob.run_id == run_id)
                .order_by(FoundryAgentJob.started_at.desc())
                .first()
            )
            if job is not None:
                job.status = "failed"
                job.error = reason
                job.completed_at = datetime.now(timezone.utc)
            issue_id = run.linear_issue_id
            self._record_outcome_if_terminal(session, run)
            session.commit()
        self._notify_state(issue_id, RunStatus.EXECUTION_FAILED)

    @traced("foundry.record_pr")
    def record_pr(self, run_id: str, pr_state: PullRequestState) -> RunStatus:
        """Record an observed PR event and decide the resulting run status.

        Called for *every* observed event (opened, synchronize, reviews, CI),
        not just the first - the guardrails are re-evaluated on every push so an
        agent cannot open a clean PR and add forbidden or sensitive files later.

        Outcomes, in precedence order:

        - merged                      -> COMPLETE
        - closed without merge        -> BLOCKED (a human must restart)
        - forbidden paths in the diff -> BLOCKED (sticky; human must intervene)
        - diff touches a sensitive area the upfront risk pass never flagged
                                      -> REVIEW_REQUIRED (risk escalation)
        - more files than allowed     -> REVIEW_REQUIRED
        - otherwise                   -> PR_OPEN

        Events that carry no file list (reviews, check suites) update CI/review
        state without weakening a prior file-based decision.
        """
        with self._sf() as session:
            run = self._require_run(session, run_id, lock=True)
            if run.status not in _PR_OBSERVABLE_STATUSES:
                raise OrchestratorError(
                    f"run {run_id} is '{run.status.value}'; PR events are only "
                    "recorded for runs with a dispatched agent"
                )
            # The first time we ever see a PR for this run emits PR_OPENED; every
            # later event (including pushes during remediation, when the status is
            # AGENT_RUNNING again) emits PR_UPDATED. Keying off the status alone
            # mis-fired PR_OPENED on each remediation push, so key off whether a
            # PR_STATE artifact already exists.
            first_observation = (
                session.query(FoundryArtifact.id)
                .filter(
                    FoundryArtifact.run_id == run_id,
                    FoundryArtifact.artifact_type == ArtifactType.PR_STATE,
                )
                .first()
                is None
            )
            self._add(session, run_id, ArtifactType.PR_STATE, pr_state)
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=(
                        AuditEventType.PR_OPENED
                        if first_observation
                        else AuditEventType.PR_UPDATED
                    ),
                    actor_type="agent",
                    output_content=pr_state,
                )
            )
            job = (
                session.query(FoundryAgentJob)
                .filter(FoundryAgentJob.run_id == run_id)
                .order_by(FoundryAgentJob.started_at.desc())
                .first()
            )
            if job is not None:
                job.pr_url = pr_state.url or job.pr_url
                if pr_state.branch:
                    # Delegated agents pick their own branch; record the actual
                    # one so subsequent events correlate by exact branch match.
                    job.branch = pr_state.branch

            run.status = self._next_status_for_pr(session, run, pr_state)
            if run.status is RunStatus.COMPLETE and job is not None:
                job.status = AgentJobStatus.SUCCEEDED
                job.completed_at = datetime.now(timezone.utc)

            if pr_state.ci_status is CIStatus.FAILING:
                session.add(
                    build_audit_event(
                        run_id=run_id,
                        event_type=AuditEventType.CI_FAILED,
                        actor_type="foundry",
                        metadata={"pr": pr_state.url},
                    )
                )

            run.current_step = run.status.value
            issue_id = run.linear_issue_id
            result_status = run.status
            self._record_outcome_if_terminal(session, run)
            session.commit()
        self._notify_state(issue_id, result_status)

        # Feedback loop: a failing check or a changes-requested review on an
        # otherwise-open PR re-dispatches the agent with the failure context,
        # still through the policy gate and bounded by the retry cap.
        reason = self._remediation_reason(pr_state)
        if result_status is RunStatus.PR_OPEN and reason is not None:
            return self._attempt_remediation(run_id, reason=reason, pr_state=pr_state)
        return result_status

    @staticmethod
    def _remediation_reason(pr_state: PullRequestState) -> str | None:
        if pr_state.ci_status is CIStatus.FAILING:
            return "ci_failed"
        if pr_state.review_status is ReviewStatus.CHANGES_REQUESTED:
            return "changes_requested"
        return None

    def _attempt_remediation(
        self, run_id: str, *, reason: str, pr_state: PullRequestState
    ) -> RunStatus:
        """Re-dispatch the agent to fix its own PR, governed and bounded.

        The attempt passes the policy gate as ``RETRY_AGENT`` (which re-checks
        approvals and the retry cap). A denied attempt parks the run at
        REVIEW_REQUIRED with a tracker comment - never silent, never unbounded.
        """
        if reason not in self._retry_on:
            return RunStatus.PR_OPEN

        with self._sf() as session:
            run = self._require_run(session, run_id, lock=True)
            # record_pr committed PR_OPEN in a *separate* transaction before
            # calling us, so the run may have moved since: a human stop(), a
            # closed PR, or a concurrent delivery's remediation that already
            # advanced it. Re-read under the row lock and bail unless it is
            # genuinely still an open PR awaiting remediation — counting jobs and
            # re-dispatching off a stale PR_OPEN is exactly the double-dispatch /
            # retry-cap-undercount / revive-a-stopped-run bug (issue #10).
            if run.status is not RunStatus.PR_OPEN:
                return run.status

            # Change-freeze windows (issue #31, "time windows"): during an active
            # freeze an *autonomous* re-dispatch is held and the PR is handed to a
            # human instead of the agent being fired again. Checked here, under
            # the row lock and before any spend/gate work, so a freeze short-
            # circuits the retry entirely. Strictly additive: a freeze can only
            # ever escalate to human review, never release a run (invariant #1).
            # The initial, human-*approved* dispatch is deliberately not gated -
            # a human is already in the loop there.
            frozen = active_freeze(self._change_freeze_windows, self._clock())
            if frozen is not None:
                window = describe_window(frozen)
                run.status = RunStatus.REVIEW_REQUIRED
                run.current_step = "change_freeze"
                session.add(
                    build_audit_event(
                        run_id=run_id,
                        event_type=AuditEventType.RISK_ESCALATED,
                        actor_type="foundry",
                        metadata={
                            "category": "change_freeze",
                            "reason": (
                                "an autonomous re-dispatch was held during a "
                                "configured change-freeze window"
                            ),
                            "trigger": reason,
                            "window": window,
                            "window_reason": frozen.reason,
                        },
                    )
                )
                issue_id = run.linear_issue_id
                session.commit()
                self._notify_state(issue_id, RunStatus.REVIEW_REQUIRED)
                self._notify_comment(
                    issue_id,
                    f"Foundry held the automatic retry "
                    f"({reason.replace('_', ' ')}) during a change-freeze window "
                    f"({window}). A human needs to decide whether to take this PR "
                    "forward during the freeze.",
                )
                return RunStatus.REVIEW_REQUIRED

            ticket = self._load(session, run_id, ArtifactType.TICKET_SNAPSHOT)
            analysis = self._load(session, run_id, ArtifactType.TICKET_ANALYSIS)
            context = self._load(session, run_id, ArtifactType.CONTEXT_BUNDLE)
            risk = self._load(session, run_id, ArtifactType.RISK_ASSESSMENT)
            plan = self._load(session, run_id, ArtifactType.DELIVERY_PLAN)
            # Union of granted roles across every approver (N-of-M, issue #31);
            # a single-approval run yields exactly the one record's roles.
            approval_users, granted = self._approval_summary(session, run_id)

            # The first job was the original dispatch; everything after is a
            # remediation attempt.
            run_jobs = (
                session.query(FoundryAgentJob)
                .filter(FoundryAgentJob.run_id == run_id)
                .order_by(
                    FoundryAgentJob.started_at.is_(None).desc(),
                    FoundryAgentJob.started_at,
                    FoundryAgentJob.id,
                )
                .all()
            )
            attempt = len(run_jobs)  # attempt N = N-th re-dispatch
            # A retry reuses the agent that opened the PR (issue #33): we never
            # re-route a run mid-flight. Resolve the original (earliest) job's
            # provider from the registry; if it is no longer configured, fall
            # back to the default so remediation can still proceed.
            original_provider = run_jobs[0].provider if run_jobs else None
            provider = self._provider_for(original_provider) or self._provider

            # Refresh provider-reported spend before the budget check so the
            # decision is made on the freshest numbers we can get. Jobs whose
            # provider reports no cost fall back to the per-dispatch estimate
            # (issue #29), and the upcoming retry counts as pending spend so the
            # gate blocks *before* overspending, not after.
            self._refresh_job_costs(session, run_id)
            run_cost = self._accumulated_cost(session, run_id)

            payload = self._policy_input(
                PolicyAction.RETRY_AGENT,
                analysis,
                context,
                risk,
                approvals=granted,
                # The original dispatch only happens after approval, so an
                # approval record exists for any run reaching a retry (issue #18).
                approval_present=bool(approval_users),
                retry=PolicyRetry(
                    attempt=attempt, max_attempts=self._max_agent_retries
                ),
                budget=PolicyBudget(
                    cost_usd=run_cost,
                    pending_cost_usd=self._estimated_cost_per_dispatch,
                    max_cost_usd=self._max_cost_per_run,
                ),
            )
            decision = self._policy.evaluate(payload)
            session.add(
                build_policy_decision_row(
                    run_id=run_id, payload=payload, decision=decision
                )
            )

            if not decision.allowed:
                run.status = RunStatus.REVIEW_REQUIRED
                run.current_step = "remediation_denied"
                session.add(
                    build_audit_event(
                        run_id=run_id,
                        event_type=AuditEventType.RUN_BLOCKED,
                        actor_type="foundry",
                        metadata={
                            "reason": f"remediation for '{reason}' denied",
                            "policy_reasons": decision.reasons,
                        },
                    )
                )
                issue_id = run.linear_issue_id
                session.commit()
                self._notify_state(issue_id, RunStatus.REVIEW_REQUIRED)
                self._notify_comment(
                    issue_id,
                    f"Foundry could not remediate ({reason.replace('_', ' ')}): "
                    + "; ".join(decision.reasons)
                    + "\n\nA human needs to take this PR over the line.",
                )
                return RunStatus.REVIEW_REQUIRED

            job_input = self._build_job_input(
                run_id,
                ticket,
                plan,
                context,
                branch=pr_state.branch or None,
                extra_instructions=self._remediation_instructions(reason, pr_state),
            )
            issue_id = run.linear_issue_id
            # Claim the run for this remediation *under the row lock*, before the
            # provider call. A concurrent delivery's remediation then re-reads a
            # non-PR_OPEN status at the top of this method and bails, so duplicate
            # CI-failure events can neither double-dispatch nor undercount the
            # retry cap by both counting the same prior_jobs (issue #10).
            run.status = RunStatus.AGENT_RUNNING
            run.current_step = "remediating"
            # Commit the allow decision (and the claim) before the provider side
            # effect so the recorded gate row survives even if create_job raises
            # (issue #13).
            session.commit()

        # The PR already exists, so a failed re-dispatch hands the PR back to a
        # human (REVIEW_REQUIRED) rather than ending the run.
        job = self._dispatch_to_provider(
            run_id,
            job_input,
            provider=provider,
            failure_status=RunStatus.REVIEW_REQUIRED,
        )

        with self._sf() as session:
            run = self._require_run(session, run_id, lock=True)
            job_row = FoundryAgentJob(
                id=new_id("job"),
                run_id=run_id,
                provider=job.provider,
                provider_job_id=job.job_id,
                status=AgentJobStatus.RUNNING,
                repo=job_input.repo,
                branch=job_input.branch_name,
                started_at=datetime.now(timezone.utc),
            )
            session.add(job_row)
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.AGENT_REMEDIATION_REQUESTED,
                    actor_type="foundry",
                    metadata={
                        "reason": reason,
                        "attempt": attempt,
                        "max_attempts": self._max_agent_retries,
                        "provider": job.provider,
                        "job_id": job.job_id,
                    },
                )
            )
            # phase 1 already flipped the run to AGENT_RUNNING under the lock. If
            # it is no longer AGENT_RUNNING, a human stop()/reject() won the race
            # while the provider call was in flight: keep the new status, cancel
            # the now-unwanted job, and never revert it (issue #10).
            if run.status is RunStatus.AGENT_RUNNING:
                run.current_step = "remediating"
                final_status = RunStatus.AGENT_RUNNING
            else:
                self._cancel_superseded_job(run_id, job, job_row)
                final_status = run.status
                _log.warning(
                    "remediation of run %s launched job %s but the run is now "
                    "'%s'; leaving the status unchanged and cancelling the job",
                    run_id,
                    job.job_id,
                    run.status.value,
                )
            try:
                session.commit()
            except Exception:
                _log.exception(
                    "remediation of run %s created provider job %s (%s) but the "
                    "job/audit commit failed; the agent is running without a "
                    "DB record",
                    run_id,
                    job.job_id,
                    job.provider,
                )
                raise
        self._notify_state(issue_id, final_status)
        return final_status

    def _record_outcome_if_terminal(self, session, run: FoundryRun) -> None:
        """Distill a finished run into its delivery-memory outcome row.

        Called inside the caller's session just before commit at every site
        that sets a terminal status. Best-effort: memory must never break the
        governance path that called it, and ``foundry-memory backfill`` can
        rebuild anything this misses.
        """
        if run.status not in TERMINAL_RUN_STATUSES:
            return
        try:
            # The session does not autoflush; make the terminal audit event
            # just added by the caller visible to the derivation queries.
            session.flush()
            record_outcome(session, run)
        except Exception:
            _log.exception("outcome recording failed for run %s", run.id)

    def _refresh_job_costs(self, session, run_id: str) -> None:
        """Pull provider-reported spend onto the job rows, best-effort.

        Providers that observe progress out-of-band report no usage; provider
        errors must never break the governance path that called this.
        """
        for job in session.query(FoundryAgentJob).filter_by(run_id=run_id):
            provider = self._provider_for(job.provider)
            if not job.provider_job_id or provider is None:
                continue
            try:
                status = provider.get_job_status(job.provider_job_id)
            except Exception:
                _log.debug("cost refresh failed for job %s", job.id, exc_info=True)
                continue
            if status.cost_usd is not None:
                job.cost_usd = status.cost_usd

    def _accumulated_cost(self, session, run_id: str) -> float:
        """Spend recorded across a run's jobs so far.

        Provider-reported ``cost_usd`` is authoritative; for any job whose
        provider reported nothing (``claude_code`` / ``webhook`` / ``manual``)
        the configured ``estimated_cost_per_dispatch`` stands in as a proxy so
        the budget cap can still bind. With the estimate at 0 (the default) an
        unreported job contributes nothing, preserving prior behaviour.
        """
        total = 0.0
        for job in session.query(FoundryAgentJob).filter_by(run_id=run_id):
            total += (
                job.cost_usd
                if job.cost_usd is not None
                else self._estimated_cost_per_dispatch
            )
        return total

    def budget_snapshot(self, run_id: str) -> dict[str, float | None]:
        """Recorded spend vs the configured cap, for the timeline / dashboard.

        Read-only: never calls the provider (no network), so it is safe on the
        token-gated timeline endpoint. ``consumed_usd`` uses the same
        estimate-fallback as the budget gate so the surfaced number matches the
        figure the gate would compare against.
        """
        with self._sf() as session:
            consumed = self._accumulated_cost(session, run_id)
        return {
            "consumed_usd": round(consumed, 4),
            "cap_usd": self._max_cost_per_run,
            "estimated_cost_per_dispatch": self._estimated_cost_per_dispatch,
        }

    @staticmethod
    def _remediation_instructions(reason: str, pr_state: PullRequestState) -> str:
        lines = [
            "",
            "---",
            "REMEDIATION REQUEST",
            f"Your previous work on PR {pr_state.url or f'#{pr_state.pr_number}'} "
            f"needs fixing: {reason.replace('_', ' ')}.",
            "Push fixes to the same branch. Do not open a new PR. Stay strictly "
            "within the original scope and constraints.",
        ]
        if pr_state.summary:
            lines += ["", "Failure details:", pr_state.summary]
        return "\n".join(lines)

    def _next_status_for_pr(
        self, session, run: FoundryRun, pr_state: PullRequestState
    ) -> RunStatus:
        run_id = run.id
        if pr_state.status is PRStatus.MERGED:
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.RUN_COMPLETED,
                    actor_type="foundry",
                    metadata={"pr": pr_state.url},
                )
            )
            return RunStatus.COMPLETE
        if pr_state.status is PRStatus.CLOSED:
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.RUN_BLOCKED,
                    actor_type="foundry",
                    metadata={
                        "category": "pr_closed_unmerged",
                        "reason": "PR closed without merge",
                        "pr": pr_state.url,
                    },
                )
            )
            return RunStatus.BLOCKED

        if not pr_state.files_changed:
            # No diff information on this event; keep the current file-based
            # decision rather than silently downgrading it.
            return (
                RunStatus.PR_OPEN
                if run.status is RunStatus.AGENT_RUNNING
                else run.status
            )

        violations = self._forbidden_violations(
            pr_state.files_changed, self._run_repo(session, run_id)
        )
        if violations:
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.RUN_BLOCKED,
                    actor_type="foundry",
                    metadata={
                        "category": "forbidden_paths",
                        "forbidden_files": violations,
                    },
                )
            )
            return RunStatus.BLOCKED

        unexpected, evidence = self._unexpected_sensitive_areas(
            session, run_id, pr_state.files_changed
        )
        if unexpected:
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.RISK_ESCALATED,
                    actor_type="foundry",
                    metadata={
                        "reason": (
                            "diff touches sensitive areas the upfront risk "
                            "assessment did not flag"
                        ),
                        "areas": unexpected,
                        "evidence": [e.model_dump() for e in evidence],
                    },
                )
            )
            return RunStatus.REVIEW_REQUIRED

        unapproved_paths = self._unapproved_path_roles(
            session, run_id, pr_state.files_changed
        )
        if unapproved_paths:
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.RISK_ESCALATED,
                    actor_type="foundry",
                    metadata={
                        "category": "path_required_roles",
                        "reason": (
                            "diff touches paths that require approval roles not "
                            "granted by the run's approvers"
                        ),
                        "required_roles": sorted(unapproved_paths),
                        "paths": {
                            role: sorted(set(paths))
                            for role, paths in unapproved_paths.items()
                        },
                    },
                )
            )
            return RunStatus.REVIEW_REQUIRED

        unapproved_custom, fired_categories = self._unapproved_custom_category_roles(
            session, run_id, pr_state.files_changed
        )
        if unapproved_custom:
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.RISK_ESCALATED,
                    actor_type="foundry",
                    metadata={
                        "category": "custom_risk_category",
                        "reason": (
                            "diff touches paths in operator-defined custom risk "
                            "categories whose approval roles the run's approvers "
                            "did not grant"
                        ),
                        "categories": sorted(fired_categories),
                        "required_roles": sorted(unapproved_custom),
                        "paths": {
                            role: sorted(set(paths))
                            for role, paths in unapproved_custom.items()
                        },
                    },
                )
            )
            return RunStatus.REVIEW_REQUIRED

        if len(pr_state.files_changed) > self._max_files_changed:
            # The diff is larger than the allowed cap - hand it to a human.
            # Record the escalation so the trail says *why* the run went to
            # review (it was previously a silent status flip) and so the
            # approval-queue clock can date the wait from this transition.
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.RISK_ESCALATED,
                    actor_type="foundry",
                    metadata={
                        "category": "diff_too_large",
                        "reason": (
                            "diff changes more files than the configured cap"
                        ),
                        "files_changed": len(pr_state.files_changed),
                        "max_files_changed": self._max_files_changed,
                    },
                )
            )
            return RunStatus.REVIEW_REQUIRED

        off_limits = self._unexpected_out_of_scope_files(
            session, run_id, pr_state.files_changed
        )
        if off_limits:
            # The agent's PR touched a path/area the approved plan explicitly
            # promised *not* to change (``out_of_scope``) - a stronger off-plan
            # signal than mere scope drift, so it is checked first and hands the
            # run to a human. Recorded as a RISK_ESCALATED event (like the other
            # diff-aware escalations) so the trail says *why* and the
            # approval-queue clock dates the wait from this transition.
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.RISK_ESCALATED,
                    actor_type="foundry",
                    metadata={
                        "category": "plan_out_of_scope",
                        "reason": (
                            "diff changes files the approved plan explicitly "
                            "marked out of scope"
                        ),
                        "out_of_scope_files": sorted(off_limits),
                    },
                )
            )
            return RunStatus.REVIEW_REQUIRED

        drift = self._unexpected_plan_files(
            session, run_id, pr_state.files_changed
        )
        if drift:
            # The agent's PR changed files outside the approved plan's declared
            # scope - hand it to a human rather than letting unplanned changes
            # ride through. Recorded as a RISK_ESCALATED event (like the other
            # diff-aware escalations) so the trail says *why* and the
            # approval-queue clock dates the wait from this transition.
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.RISK_ESCALATED,
                    actor_type="foundry",
                    metadata={
                        "category": "plan_scope_drift",
                        "reason": (
                            "diff changes files outside the approved plan's "
                            "expected files/areas"
                        ),
                        "unexpected_files": sorted(drift),
                    },
                )
            )
            return RunStatus.REVIEW_REQUIRED

        if self._plan_tests_missing(session, run_id, pr_state.files_changed):
            # The approved plan promised tests but the diff shipped none (issue
            # #169 slice 2). A deterministic plan-satisfaction signal, checked
            # before the (optional) LLM judge so the cheap heuristic short-circuits
            # first. Escalate-only and audited like the other diff-aware
            # escalations so the trail says *why* and the approval-queue clock
            # dates the wait from this transition.
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.RISK_ESCALATED,
                    actor_type="foundry",
                    metadata={
                        "category": "plan_tests_missing",
                        "reason": (
                            "the approved plan promised tests but the diff "
                            "touches no test file"
                        ),
                    },
                )
            )
            return RunStatus.REVIEW_REQUIRED

        unsatisfied = self._plan_unsatisfied(session, run_id, pr_state)
        if unsatisfied is not None:
            # The headline plan-aware gate (issue #169 slice 3): the diff stayed
            # inside the plan's declared file scope, but the LLM judge found it
            # does not plausibly *satisfy* the plan's intent (goal/scope/steps).
            # Checked last so the cheap deterministic gates short-circuit first
            # and the LLM call only fires on an otherwise-clean diff. Escalate-only
            # and audited like the other diff-aware escalations so the trail says
            # *why* and the approval-queue clock dates the wait from this transition.
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.RISK_ESCALATED,
                    actor_type="foundry",
                    metadata={
                        "category": "plan_unsatisfied",
                        "reason": (
                            unsatisfied.reason
                            or "the approved plan's intent is not satisfied by the diff"
                        ),
                        "source": "llm",
                    },
                )
            )
            return RunStatus.REVIEW_REQUIRED
        return RunStatus.PR_OPEN

    def _unapproved_path_roles(
        self, session, run_id: str, files: list[str]
    ) -> dict[str, list[str]]:
        """Per-path approval roles a diff demands that no approver has granted.

        For each configured ``policy.path_required_roles`` rule, a changed file
        matching the path glob demands that rule's roles. A role is *satisfied*
        if it is already in the union of roles granted across the run's approvers
        (:meth:`_approval_summary`) - so a path whose role the upfront risk pass
        already forced a human to sign with does **not** re-escalate, mirroring
        how an *anticipated* sensitive area is not flagged again.

        Returns the role -> matched paths for every still-*unsatisfied* role; an
        empty mapping (the default with no rules configured) means nothing to
        escalate. Strictly additive: this can only ever surface a role to require,
        never drop one (invariant #1).
        """
        if not self._path_required_roles:
            return {}
        _, granted = self._approval_summary(session, run_id)
        unapproved: dict[str, list[str]] = {}
        for path in files:
            for pattern, roles in self._path_required_roles.items():
                if not glob_match(path, pattern):
                    continue
                for role in roles:
                    if role.value in granted:
                        continue
                    unapproved.setdefault(role.value, []).append(path)
        return unapproved

    def _unapproved_custom_category_roles(
        self, session, run_id: str, files: list[str]
    ) -> tuple[dict[str, list[str]], list[str]]:
        """Custom-category diff roles a diff demands that no approver has granted.

        The diff-path twin of :meth:`_unapproved_path_roles`, for operator-defined
        custom risk categories (issue #155): for each category, a changed file
        matching one of its ``path_globs`` demands that category's roles. A role
        already in the union granted across the run's approvers is *satisfied* -
        so a category whose role the upfront ticket-text pass already forced a
        human to sign with does **not** re-escalate, mirroring how an
        *anticipated* sensitive area is not flagged again.

        Returns ``(role -> matched paths, fired category names)``; an empty
        mapping (the default with no categories configured) means nothing to
        escalate. Strictly additive: it can only ever surface a role to require,
        never drop one (invariant #1).
        """
        if not self._custom_risk_categories:
            return {}, []
        _, granted = self._approval_summary(session, run_id)
        unapproved: dict[str, list[str]] = {}
        fired: list[str] = []
        for path in files:
            for category in self._custom_risk_categories:
                if not category.matches_path(path):
                    continue
                category_fired = False
                for role in category.required_roles:
                    if role in granted:
                        continue
                    unapproved.setdefault(role, []).append(path)
                    category_fired = True
                if category_fired and category.name not in fired:
                    fired.append(category.name)
        return unapproved, fired

    def _unexpected_plan_files(
        self, session, run_id: str, files: list[str]
    ) -> list[str]:
        """Changed files that fall outside the approved plan's declared scope.

        The consumer of the LLM planner's
        :attr:`DeliveryPlan.expected_files_or_areas`: a diff straying outside
        every file/area the plan named is the "agent went off-plan" signal and
        escalates the run to a human. Returns the offending files (empty = no
        drift). Inert - returns ``[]`` - when the kill switch is off, the plan
        artifact is missing, or the plan declared no expected files/areas (the
        template planner's default), so the only runs it engages are ones a
        code-aware planner actually scoped. Strictly additive (escalate-only),
        so it never releases a run (invariant #1).
        """
        if not self._enforce_plan_scope:
            return []
        try:
            plan: DeliveryPlan = self._load(
                session, run_id, ArtifactType.DELIVERY_PLAN
            )
        except OrchestratorError:
            return []
        return files_outside_scope(plan.expected_files_or_areas, files)

    def _unexpected_out_of_scope_files(
        self, session, run_id: str, files: list[str]
    ) -> list[str]:
        """Changed files that hit a path/area the plan marked ``out_of_scope``.

        The out-of-scope twin of :meth:`_unexpected_plan_files`: it consumes the
        approved plan's ``out_of_scope`` (paths/areas the plan promised *not* to
        touch) and returns any changed file matching one of them - a stronger
        off-plan signal than mere scope drift, so the run escalates to a human.
        Returns the offending files (empty = none). Inert - returns ``[]`` - when
        the kill switch is off, the plan artifact is missing, or the plan declared
        no out-of-scope entries (the template planner's default), so the only runs
        it engages are ones a code-aware planner actually scoped. Strictly additive
        (escalate-only), so it never releases a run (invariant #1).
        """
        if not self._enforce_plan_out_of_scope:
            return []
        try:
            plan: DeliveryPlan = self._load(
                session, run_id, ArtifactType.DELIVERY_PLAN
            )
        except OrchestratorError:
            return []
        return files_matching_scope(plan.out_of_scope, files)

    def _plan_tests_missing(
        self, session, run_id: str, files: list[str]
    ) -> bool:
        """True when the approved plan promised tests but the diff ships none.

        The deterministic test-plan satisfaction signal (issue #169, slice 2): if
        the approved :class:`DeliveryPlan`'s ``test_plan`` declares any
        unit/integration/e2e tests yet no changed file matches the configured
        ``test_path_globs`` convention, the run is escalated to a human - the "the
        plan promised tests, the diff shipped none" signal. Returns ``False`` (no
        escalation) when the kill switch is off (the default - so historical
        behaviour is unchanged), the plan artifact is missing, or the plan promised
        no tests. Strictly additive (escalate-only), so it never releases a run
        (invariant #1).
        """
        if not self._enforce_plan_tests:
            return False
        try:
            plan: DeliveryPlan = self._load(
                session, run_id, ArtifactType.DELIVERY_PLAN
            )
        except OrchestratorError:
            return False
        tp = plan.test_plan
        promised = bool(tp.unit_tests or tp.integration_tests or tp.e2e_tests)
        if not promised:
            return False
        return not diff_touches_tests(files, self._test_path_globs)

    def _plan_unsatisfied(
        self, session, run_id: str, pr_state: PullRequestState
    ):
        """LLM judgement that the diff does not satisfy the approved plan's intent.

        The headline plan-aware gate (issue #169 slice 3), the consumer of the
        approved :class:`DeliveryPlan`'s prose intent (goal / scope /
        implementation steps) that the deterministic file-containment checks
        ignore. Returns the escalating :class:`PlanSatisfactionVerdict` when the
        judge found the change does not plausibly satisfy the plan, else ``None``.

        Inert - returns ``None`` - when no judge is injected (the default,
        ``plan_satisfaction.provider: none``) or the plan artifact is missing, so
        offline deployments are byte-for-byte unchanged. Degrade-to-noop: a
        ``degraded`` verdict (LLM failure) or any unexpected error from the judge
        is treated as "nothing to escalate", so an outage never blocks *or*
        releases a run. Escalate-only, so it can only ever raise REVIEW_REQUIRED
        (invariant #1).
        """
        if self._plan_satisfaction_judge is None:
            return None
        try:
            plan: DeliveryPlan = self._load(
                session, run_id, ArtifactType.DELIVERY_PLAN
            )
        except OrchestratorError:
            return None
        try:
            verdict = self._plan_satisfaction_judge.judge(plan, pr_state)
        except Exception:
            # Defence in depth: the LLM judge already degrades on LLMError, but a
            # bug in a custom judge must never break PR-event processing - treat
            # it as a no-op (the deterministic gates above stay in charge).
            return None
        if verdict.degraded or verdict.satisfied:
            return None
        return verdict

    def _unexpected_sensitive_areas(
        self, session, run_id: str, files: list[str]
    ) -> tuple[dict[str, list[str]], list[RiskEvidence]]:
        """Sensitive areas the diff touches that intake never flagged.

        Areas flagged upfront already had their approval requirements enforced
        by the policy gate at dispatch; an area appearing *only* in the diff has
        had no human look at it, so it escalates the run. Returns the unexpected
        areas plus the classifier's cited evidence for them.
        """
        ticket: RawTicket | None = None
        if self._diff_risk_needs_ticket:
            try:
                ticket = self._load(session, run_id, ArtifactType.TICKET_SNAPSHOT)
            except OrchestratorError:
                ticket = None
        findings = self._diff_risk.classify_diff(files, ticket)
        if not findings.areas:
            return {}, []
        try:
            risk: RiskAssessment = self._load(
                session, run_id, ArtifactType.RISK_ASSESSMENT
            )
            anticipated = set(risk.sensitive_areas.names())
        except OrchestratorError:
            anticipated = set()
        unexpected = {
            area: paths
            for area, paths in findings.areas.items()
            if area not in anticipated
        }
        evidence = [e for e in findings.evidence if e.area in unexpected]
        return unexpected, evidence

    # -- helpers --------------------------------------------------------------

    def _build_job_input(
        self,
        run_id: str,
        ticket: RawTicket,
        plan: DeliveryPlan,
        context: ContextBundle,
        *,
        branch: str | None = None,
        extra_instructions: str = "",
    ) -> CodingAgentJobInput:
        best_repo = context.best_repository
        assert best_repo is not None  # guaranteed by policy/readiness gating
        return CodingAgentJobInput(
            run_id=run_id,
            repo=best_repo.repo,
            branch_name=branch or branch_name_for(ticket),
            ticket_url=f"https://linear.app/issue/{ticket.issue_key}",
            delivery_plan=plan,
            agent_instructions=(plan.agent_instructions or "") + extra_instructions,
            constraints=JobConstraints(
                do_not_modify=list(self._forbidden_globs_for(best_repo.repo)),
                required_tests=list(context.test_commands),
                max_files_changed=self._max_files_changed,
            ),
            tracker_issue_id=ticket.issue_id,
        )

    def _policy_input(
        self,
        action: PolicyAction,
        analysis: TicketAnalysis,
        context: ContextBundle,
        risk: RiskAssessment,
        approvals: set[str] | None = None,
        retry: PolicyRetry | None = None,
        budget: PolicyBudget | None = None,
        approval_present: bool = False,
    ) -> PolicyInput:
        best_repo = context.best_repository
        repo_name = best_repo.repo if best_repo else None
        return PolicyInput(
            action=action,
            ticket=PolicyTicket(
                work_type=analysis.work_type.value,
                readiness=analysis.implementation_readiness,
            ),
            risk=PolicyRisk(
                overall_risk=risk.overall_risk,
                **risk.sensitive_areas.model_dump(),
            ),
            repo=PolicyRepo(
                name=repo_name,
                confidence=best_repo.confidence if best_repo else 0,
                # Stamp the run's resolved extra approval roles so the gate (and
                # OPA, via the same field) requires them: the routed repo's
                # configured roles (issue #31) unioned with any custom risk
                # category whose ticket-text keywords fired (issue #155).
                required_roles=self._resolved_required_roles(repo_name, risk),
            ),
            retry=retry or PolicyRetry(),
            budget=budget or PolicyBudget(),
            approval={role: True for role in (approvals or set())},
            approval_present=approval_present,
        )

    def _repo_required_roles_for(self, repo: str | None) -> list[ApprovalRole]:
        """Approval roles scoped to ``repo`` via ``policy.repo_required_roles``.

        Empty unless an operator configured extra roles for this repo. The
        result is unioned with the risk-derived roles by
        :func:`policy.engine.required_approvals`; it can only *add* a required
        approval, never remove one (invariant #1).
        """
        if not repo:
            return []
        return list(self._repo_required_roles.get(repo, []))

    def _resolved_required_roles(
        self, repo: str | None, risk: RiskAssessment
    ) -> list[ApprovalRole]:
        """Resolved extra approval roles for a run: per-repo plus custom-category.

        The single source of the roles stamped into ``PolicyInput.repo.required_roles``
        and pre-validated in :meth:`approve`, so the gate and the approval
        lifecycle never disagree on what a run requires. It unions the per-repo
        roles (``policy.repo_required_roles``) with the roles demanded by any
        operator-defined custom risk category whose ticket-text keywords fired at
        intake (``risk.custom_risk_categories``, issue #155). Both are strictly
        additive - they can only *add* a required approval, never drop the
        risk-derived ones the gate computes from the built-in area booleans
        (invariant #1). Routing custom-category roles through this existing
        resolved-roles field means no new gate rule and no ``foundry.rego`` change
        (invariant #2 stays satisfied for free).
        """
        roles = self._repo_required_roles_for(repo)
        for role in risk.custom_required_approvals:
            if role not in roles:
                roles.append(role)
        return roles

    def _apply_custom_risk_categories(
        self, ticket: RawTicket, risk: RiskAssessment
    ) -> RiskAssessment:
        """Fold ticket-text custom-category hits into a RiskAssessment (#155).

        For every configured custom category whose keywords appear in the
        ticket's title/description, record its demanded approval roles in
        :attr:`RiskAssessment.custom_required_approvals` and append a cited
        reason + evidence entry. Strictly additive: it only ever *adds* roles
        and evidence (de-duplicated), never removes a built-in area's role or
        lowers the risk level. Returns the assessment unchanged when no category
        is configured or none fires, so the default path is byte-for-byte the
        same artifact.
        """
        if not self._custom_risk_categories:
            return risk
        blob = ticket.risk_blob()
        extra_roles: list[ApprovalRole] = list(risk.custom_required_approvals)
        reasons = list(risk.risk_reasons)
        evidence = list(risk.evidence)
        fired = False
        for category in self._custom_risk_categories:
            matched = category.matched_keywords(blob)
            if not matched:
                continue
            fired = True
            reasons.append(
                f"Ticket text matches custom risk category '{category.name}'."
            )
            evidence.append(
                RiskEvidence(
                    area=category.name,
                    detail="keyword(s) in ticket title/description: "
                    + ", ".join(f"'{k}'" for k in matched),
                    source="heuristic",
                )
            )
            for role in category.required_roles:
                resolved = ApprovalRole(role)
                if resolved not in extra_roles:
                    extra_roles.append(resolved)
        if not fired:
            return risk
        return risk.model_copy(
            update={
                "risk_reasons": reasons,
                "evidence": evidence,
                "custom_required_approvals": extra_roles,
            }
        )

    def _min_approvals_for(self, repo: str | None) -> int:
        """Minimum distinct approvers for a run routed to ``repo`` (issue #31).

        The effective minimum is the global floor raised by any per-repo
        override - ``max(global, per-repo)`` - so a repo can only ever demand
        *more* sign-offs than the floor, never fewer (invariant #1). Defaults to
        1 (single approval), which is the historical behaviour byte-for-byte.
        """
        floor = self._min_approvals
        if repo and repo in self._repo_min_approvals:
            return max(floor, self._repo_min_approvals[repo])
        return floor

    def _approval_records(self, session, run_id: str) -> list[dict]:
        """Every recorded ``APPROVAL_RECORD`` artifact for a run, oldest first.

        Each :meth:`approve` writes one such artifact, so the full set is the
        run's accumulated approvals - the basis for the N-of-M distinct-approver
        count and the union of granted roles across approvers (issue #31).
        """
        import json

        rows = (
            session.query(FoundryArtifact)
            .filter(
                FoundryArtifact.run_id == run_id,
                FoundryArtifact.artifact_type == ArtifactType.APPROVAL_RECORD,
            )
            .order_by(FoundryArtifact.version)
            .all()
        )
        return [json.loads(row.content_json) for row in rows]

    def _approval_summary(
        self, session, run_id: str
    ) -> tuple[list[str], set[str]]:
        """Distinct approver identities (oldest first) and the union of granted
        roles across all of a run's recorded approvals (issue #31).

        With the default ``min_approvals`` of 1 there is exactly one approval
        record, so the union is that record's roles - byte-for-byte the previous
        single-record behaviour. With N-of-M the role coverage is the union over
        every distinct approver, so several approvers can jointly satisfy the
        required roles.
        """
        users: list[str] = []
        roles: set[str] = set()
        for record in self._approval_records(session, run_id):
            user = record.get("user")
            if user is not None and user not in users:
                users.append(user)
            roles.update(record.get("granted_roles", []))
        return users, roles

    def _forbidden_globs_for(self, repo: str | None) -> list[str]:
        """Global forbidden globs plus any extra ones scoped to ``repo``.

        Per-repo globs (issue #35) are strictly additive: they only ever *add*
        protected paths for a given repo on top of the global floor, so the
        sticky forbidden-path block can never be weakened by repo scoping - only
        made stricter (AGENTS.md invariant #1).
        """
        extra = self._repo_forbidden_globs.get(repo) if repo else None
        return self._forbidden_globs + extra if extra else self._forbidden_globs

    def _run_repo(self, session, run_id: str) -> str | None:
        """The repo this run was routed to, read from its context bundle.

        The canonical "which repo is this run for" used to scope per-repo
        forbidden globs - the same source the agent was dispatched against, so
        the dispatch-time ``do_not_modify`` constraints and the PR-time block
        agree on which repo's protected paths apply.
        """
        try:
            context: ContextBundle = self._load(
                session, run_id, ArtifactType.CONTEXT_BUNDLE
            )
        except OrchestratorError:
            return None
        return context.best_repository.repo if context.best_repository else None

    def _forbidden_violations(
        self, files: list[str], repo: str | None = None
    ) -> list[str]:
        globs = self._forbidden_globs_for(repo)
        violations: list[str] = []
        for path in files:
            for pattern in globs:
                if glob_match(path, pattern):
                    violations.append(path)
                    break
        return violations

    def _add(self, session, run_id: str, artifact_type: ArtifactType, content) -> None:
        session.add(
            build_artifact(run_id=run_id, artifact_type=artifact_type, content=content)
        )

    @staticmethod
    def _require_run(session, run_id: str, *, lock: bool = False) -> FoundryRun:
        """Load the run row, optionally taking a ``SELECT ... FOR UPDATE`` lock.

        Every method that does a check-then-write on run status passes
        ``lock=True`` so concurrent transitions serialise on the row instead of
        racing (issue #10). The lock is held until the surrounding session
        commits. On SQLite the dialect ignores ``FOR UPDATE``, which is correct:
        the dev DB is single-connection, the production guarantee is Postgres's.
        """
        run = session.get(FoundryRun, run_id, with_for_update=lock)
        if run is None:
            raise OrchestratorError(f"run {run_id} not found")
        return run

    @staticmethod
    def _refuse_if_terminal(run: FoundryRun) -> None:
        """Terminal runs never re-enter any state (schemas/common.py): allowing a
        second termination would overwrite the single recorded outcome and poison
        the routing priors."""
        if run.status in TERMINAL_RUN_STATUSES:
            raise OrchestratorError(
                f"run {run.id} is already terminal ('{run.status.value}')"
            )

    def _latest_artifact(
        self, session, run_id: str, artifact_type: ArtifactType
    ) -> FoundryArtifact | None:
        return (
            session.query(FoundryArtifact)
            .filter(
                FoundryArtifact.run_id == run_id,
                FoundryArtifact.artifact_type == artifact_type,
            )
            .order_by(FoundryArtifact.version.desc())
            .first()
        )

    def _load(self, session, run_id: str, artifact_type: ArtifactType):
        row = self._latest_artifact(session, run_id, artifact_type)
        if row is None:
            raise OrchestratorError(f"missing artifact {artifact_type.value} for {run_id}")
        model = _ARTIFACT_MODELS[artifact_type]
        return model.model_validate_json(row.content_json)

    def _load_raw(self, session, run_id: str, artifact_type: ArtifactType):
        import json

        row = self._latest_artifact(session, run_id, artifact_type)
        return json.loads(row.content_json) if row else None
