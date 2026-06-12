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
from typing import Mapping

from foundry.agents.manual import ManualProvider
from foundry.agents.provider import CodingAgentProvider
from foundry.connectors.base import IssueTracker
from foundry.connectors.comments import format_analysis_comment, state_for
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
from foundry.engines.planner import (
    DEFAULT_FORBIDDEN_GLOBS,
    DeliveryPlanner,
    TemplatePlanner,
    branch_name_for,
)
from foundry.engines.risk import (
    DiffRiskClassifier,
    GlobDiffRiskClassifier,
    HeuristicRiskClassifier,
    RiskClassifier,
    glob_match,
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
)
from foundry.schemas.agent import CodingAgentJob, CodingAgentJobInput, JobConstraints
from foundry.schemas.analysis import TicketAnalysis
from foundry.memory.outcomes import record_outcome
from foundry.schemas.common import (
    ACTIVE_RUN_STATUSES,
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

# Run states in which PR webhook events are meaningful for the run.
_PR_OBSERVABLE_STATUSES = frozenset(
    {RunStatus.AGENT_RUNNING, RunStatus.PR_OPEN, RunStatus.REVIEW_REQUIRED}
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
        policy_engine: PolicyEngine | None = None,
        provider: CodingAgentProvider | None = None,
        issue_tracker: IssueTracker | None = None,
        max_files_changed: int = 12,
        forbidden_globs: tuple[str, ...] | list[str] | None = None,
        sensitive_path_globs: Mapping[str, tuple[str, ...]] | None = None,
        max_agent_retries: int = 2,
        retry_on: tuple[str, ...] | list[str] = ("ci_failed", "changes_requested"),
        max_cost_per_run: float | None = None,
    ) -> None:
        self._sf = session_factory
        self._analyzer = analyzer or HeuristicAnalyzer()
        self._enricher = enricher or StaticContextEnricher()
        self._risk = risk_classifier or HeuristicRiskClassifier()
        self._planner = planner or TemplatePlanner()
        self._policy = policy_engine or LocalPolicyEngine()
        self._provider = provider or ManualProvider()
        # Optional: when set, Foundry writes progress/state back to the tracker.
        self._tracker = issue_tracker
        self._max_files_changed = max_files_changed
        self._forbidden_globs = list(
            forbidden_globs if forbidden_globs is not None else DEFAULT_FORBIDDEN_GLOBS
        )
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
        self._max_agent_retries = max_agent_retries
        self._retry_on = frozenset(retry_on)
        self._max_cost_per_run = max_cost_per_run

    # -- intake + planning ----------------------------------------------------

    @traced("foundry.intake_and_plan")
    def intake_and_plan(
        self, ticket: RawTicket, *, trigger_type: str, created_by: str | None = None
    ) -> str:
        """Run analysis -> context -> risk -> plan -> policy gate; persist all."""
        # At most one *active* run per issue; finished/blocked runs may be
        # superseded by a fresh trigger (e.g. after the ticket is clarified).
        active = self.find_active_run_id_for_issue(ticket.issue_id)
        if active is not None:
            raise OrchestratorError(
                f"issue {ticket.issue_id} already has an active run ({active})"
            )
        run_id = new_id("run")
        analysis = self._analyzer.analyse(ticket)
        context = self._enricher.enrich(ticket, analysis)
        risk = self._risk.classify(ticket, analysis, context)
        plan = self._planner.plan(ticket, analysis, context, risk)
        payload = self._policy_input(PolicyAction.START_AGENT, analysis, context, risk)
        decision = self._policy.evaluate(payload)

        status = self._post_plan_status(analysis, risk, decision.allowed)

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
                agent_mode=decision.allowed_agent_mode,
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
                session.add(
                    build_audit_event(
                        run_id=run_id,
                        event_type=AuditEventType.RUN_BLOCKED,
                        actor_type="foundry",
                        metadata={"category": "unroutable"},
                    )
                )
            self._record_outcome_if_terminal(session, run)
            session.commit()

        # Mirror the outcome back to the tracker (Linear) if one is configured.
        if self._tracker is not None:
            try:
                self._tracker.post_comment(
                    ticket.issue_id,
                    format_analysis_comment(analysis, risk, plan, status),
                )
                self._tracker.set_state(ticket.issue_id, state_for(status))
            except Exception:
                _log.exception(
                    "tracker write-back failed for issue %s; Foundry state is "
                    "authoritative but Linear may be stale",
                    ticket.issue_id,
                )
        return run_id

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

    def _notify_comment(self, issue_id: str, body: str) -> None:
        if self._tracker is not None:
            try:
                self._tracker.post_comment(issue_id, body)
            except Exception:
                _log.exception("tracker comment failed for issue %s", issue_id)

    @staticmethod
    def _post_plan_status(
        analysis: TicketAnalysis, risk: RiskAssessment, policy_allowed: bool
    ) -> RunStatus:
        # Readiness first: an unclear ticket should be clarified before we worry
        # about anything downstream (it usually also lacks a resolvable repo).
        if not analysis.is_ready_to_build:
            return RunStatus.NEEDS_CLARIFICATION
        # The ticket is clear, but the work still can't be scoped to a repo.
        if risk.overall_risk is OverallRisk.BLOCKED:
            return RunStatus.BLOCKED
        # A ready, scoped plan awaits human approval before any agent runs.
        return RunStatus.WAITING_APPROVAL

    # -- approval -------------------------------------------------------------

    def approve(
        self, run_id: str, *, user: str, granted_roles: set[ApprovalRole] | None = None
    ) -> None:
        granted_roles = granted_roles or set()
        with self._sf() as session:
            run = self._require_run(session, run_id)
            if run.status is not RunStatus.WAITING_APPROVAL:
                raise OrchestratorError(
                    f"run {run_id} is '{run.status.value}', not awaiting approval"
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
            run.status = RunStatus.APPROVED
            run.approved_by = user
            run.approved_at = datetime.now(timezone.utc)
            run.current_step = "approved"
            issue_id = run.linear_issue_id
            session.commit()
        self._notify_state(issue_id, RunStatus.APPROVED)

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
            run = self._require_run(session, run_id)
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
            issue_id = run.linear_issue_id
            self._record_outcome_if_terminal(session, run)
            session.commit()
        self._notify_state(issue_id, status)

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
        """Associate an observed PR back to its run via the agent job's branch."""
        if not branch:
            return None
        with self._sf() as session:
            job = (
                session.query(FoundryAgentJob)
                .filter(FoundryAgentJob.branch == branch)
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

    # -- agent dispatch -------------------------------------------------------

    @traced("foundry.dispatch_agent")
    def dispatch_agent(self, run_id: str) -> CodingAgentJob:
        """Re-check policy with the recorded approvals, then launch the provider."""
        with self._sf() as session:
            run = self._require_run(session, run_id)
            if run.status is not RunStatus.APPROVED:
                raise OrchestratorError(
                    f"run {run_id} is '{run.status.value}', not approved"
                )
            analysis = self._load(session, run_id, ArtifactType.TICKET_ANALYSIS)
            context = self._load(session, run_id, ArtifactType.CONTEXT_BUNDLE)
            risk = self._load(session, run_id, ArtifactType.RISK_ASSESSMENT)
            plan = self._load(session, run_id, ArtifactType.DELIVERY_PLAN)
            ticket = self._load(session, run_id, ArtifactType.TICKET_SNAPSHOT)
            approval = self._load_raw(session, run_id, ArtifactType.APPROVAL_RECORD)

            granted = set(approval.get("granted_roles", [])) if approval else set()
            payload = self._policy_input(
                PolicyAction.START_AGENT, analysis, context, risk, approvals=granted
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
            job = self._provider.create_job(job_input)

            session.add(
                FoundryAgentJob(
                    id=new_id("job"),
                    run_id=run_id,
                    provider=job.provider,
                    provider_job_id=job.job_id,
                    status=AgentJobStatus.RUNNING,
                    repo=job_input.repo,
                    branch=job_input.branch_name,
                    started_at=datetime.now(timezone.utc),
                )
            )
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.AGENT_STARTED,
                    actor_type="foundry",
                    metadata={"provider": job.provider, "job_id": job.job_id},
                )
            )
            run.status = RunStatus.AGENT_RUNNING
            run.current_step = "agent_running"
            issue_id = run.linear_issue_id
            session.commit()
        self._notify_state(issue_id, RunStatus.AGENT_RUNNING)
        return job

    # -- PR monitoring --------------------------------------------------------

    def mark_agent_failed(self, run_id: str, *, reason: str = "agent error") -> None:
        """Mark a run as failed when the agent crashes without creating a PR."""
        with self._sf() as session:
            run = self._require_run(session, run_id)
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
            run = self._require_run(session, run_id)
            if run.status not in _PR_OBSERVABLE_STATUSES:
                raise OrchestratorError(
                    f"run {run_id} is '{run.status.value}'; PR events are only "
                    "recorded for runs with a dispatched agent"
                )
            first_observation = run.status is RunStatus.AGENT_RUNNING
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
            run = self._require_run(session, run_id)
            ticket = self._load(session, run_id, ArtifactType.TICKET_SNAPSHOT)
            analysis = self._load(session, run_id, ArtifactType.TICKET_ANALYSIS)
            context = self._load(session, run_id, ArtifactType.CONTEXT_BUNDLE)
            risk = self._load(session, run_id, ArtifactType.RISK_ASSESSMENT)
            plan = self._load(session, run_id, ArtifactType.DELIVERY_PLAN)
            approval = self._load_raw(session, run_id, ArtifactType.APPROVAL_RECORD)
            granted = set(approval.get("granted_roles", [])) if approval else set()

            # The first job was the original dispatch; everything after is a
            # remediation attempt.
            prior_jobs = (
                session.query(FoundryAgentJob)
                .filter(FoundryAgentJob.run_id == run_id)
                .count()
            )
            attempt = prior_jobs  # attempt N = N-th re-dispatch

            # Refresh provider-reported spend before the budget check so the
            # decision is made on the freshest numbers we can get.
            self._refresh_job_costs(session, run_id)
            run_cost = sum(
                job.cost_usd or 0.0
                for job in session.query(FoundryAgentJob).filter_by(run_id=run_id)
            )

            payload = self._policy_input(
                PolicyAction.RETRY_AGENT,
                analysis,
                context,
                risk,
                approvals=granted,
                retry=PolicyRetry(
                    attempt=attempt, max_attempts=self._max_agent_retries
                ),
                budget=PolicyBudget(
                    cost_usd=run_cost, max_cost_usd=self._max_cost_per_run
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
            job = self._provider.create_job(job_input)
            session.add(
                FoundryAgentJob(
                    id=new_id("job"),
                    run_id=run_id,
                    provider=job.provider,
                    provider_job_id=job.job_id,
                    status=AgentJobStatus.RUNNING,
                    repo=job_input.repo,
                    branch=job_input.branch_name,
                    started_at=datetime.now(timezone.utc),
                )
            )
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
            run.status = RunStatus.AGENT_RUNNING
            run.current_step = "remediating"
            issue_id = run.linear_issue_id
            session.commit()
        self._notify_state(issue_id, RunStatus.AGENT_RUNNING)
        return RunStatus.AGENT_RUNNING

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
            if not job.provider_job_id or job.provider != self._provider.name:
                continue
            try:
                status = self._provider.get_job_status(job.provider_job_id)
            except Exception:
                _log.debug("cost refresh failed for job %s", job.id, exc_info=True)
                continue
            if status.cost_usd is not None:
                job.cost_usd = status.cost_usd

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

        violations = self._forbidden_violations(pr_state.files_changed)
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

        if len(pr_state.files_changed) > self._max_files_changed:
            return RunStatus.REVIEW_REQUIRED
        return RunStatus.PR_OPEN

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
                do_not_modify=list(self._forbidden_globs),
                required_tests=list(context.test_commands),
                max_files_changed=self._max_files_changed,
            ),
            tracker_issue_id=ticket.issue_id,
        )

    @staticmethod
    def _policy_input(
        action: PolicyAction,
        analysis: TicketAnalysis,
        context: ContextBundle,
        risk: RiskAssessment,
        approvals: set[str] | None = None,
        retry: PolicyRetry | None = None,
        budget: PolicyBudget | None = None,
    ) -> PolicyInput:
        best_repo = context.best_repository
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
                name=best_repo.repo if best_repo else None,
                confidence=best_repo.confidence if best_repo else 0,
            ),
            retry=retry or PolicyRetry(),
            budget=budget or PolicyBudget(),
            approval={role: True for role in (approvals or set())},
        )

    def _forbidden_violations(self, files: list[str]) -> list[str]:
        violations: list[str] = []
        for path in files:
            for pattern in self._forbidden_globs:
                if glob_match(path, pattern):
                    violations.append(path)
                    break
        return violations

    def _add(self, session, run_id: str, artifact_type: ArtifactType, content) -> None:
        session.add(
            build_artifact(run_id=run_id, artifact_type=artifact_type, content=content)
        )

    @staticmethod
    def _require_run(session, run_id: str) -> FoundryRun:
        run = session.get(FoundryRun, run_id)
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
