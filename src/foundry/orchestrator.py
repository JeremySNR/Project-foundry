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

import fnmatch

from foundry.agents.manual import ManualProvider
from foundry.agents.provider import CodingAgentProvider
from foundry.audit.events import (
    build_artifact,
    build_audit_event,
    build_policy_decision_row,
    new_id,
)
from foundry.db.models import (
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
from foundry.engines.risk import HeuristicRiskClassifier, RiskClassifier
from foundry.policy.engine import (
    LocalPolicyEngine,
    PolicyEngine,
    PolicyInput,
    PolicyRepo,
    PolicyRisk,
    PolicyTicket,
)
from foundry.schemas.agent import CodingAgentJob, CodingAgentJobInput, JobConstraints
from foundry.schemas.analysis import TicketAnalysis
from foundry.schemas.common import (
    AgentMode,
    ApprovalRole,
    OverallRisk,
    PolicyAction,
    PRStatus,
    RunStatus,
)
from foundry.schemas.context import ContextBundle
from foundry.schemas.plan import DeliveryPlan
from foundry.schemas.pr import PullRequestState
from foundry.schemas.risk import RiskAssessment
from foundry.schemas.ticket import RawTicket

# Which Pydantic model each loadable artifact type deserialises back into.
_ARTIFACT_MODELS: dict[ArtifactType, type] = {
    ArtifactType.TICKET_SNAPSHOT: RawTicket,
    ArtifactType.TICKET_ANALYSIS: TicketAnalysis,
    ArtifactType.CONTEXT_BUNDLE: ContextBundle,
    ArtifactType.RISK_ASSESSMENT: RiskAssessment,
    ArtifactType.DELIVERY_PLAN: DeliveryPlan,
}


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
        planner: DeliveryPlanner | None = None,
        policy_engine: PolicyEngine | None = None,
        provider: CodingAgentProvider | None = None,
        max_files_changed: int = 12,
    ) -> None:
        self._sf = session_factory
        self._analyzer = analyzer or HeuristicAnalyzer()
        self._enricher = enricher or StaticContextEnricher()
        self._risk = risk_classifier or HeuristicRiskClassifier()
        self._planner = planner or TemplatePlanner()
        self._policy = policy_engine or LocalPolicyEngine()
        self._provider = provider or ManualProvider()
        self._max_files_changed = max_files_changed

    # -- intake + planning ----------------------------------------------------

    def intake_and_plan(
        self, ticket: RawTicket, *, trigger_type: str, created_by: str | None = None
    ) -> str:
        """Run analysis -> context -> risk -> plan -> policy gate; persist all."""
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
                    )
                )
            session.commit()
        return run_id

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
            from datetime import datetime, timezone

            run.approved_at = datetime.now(timezone.utc)
            run.current_step = "approved"
            session.commit()

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
            run.status = status
            run.current_step = status.value
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=event_type,
                    actor_type="human",
                    actor_id=user,
                )
            )
            session.commit()

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

    def get_run(self, run_id: str) -> FoundryRun | None:
        with self._sf() as session:
            return session.get(FoundryRun, run_id)

    def list_runs(self) -> list[FoundryRun]:
        with self._sf() as session:
            return list(session.query(FoundryRun).order_by(FoundryRun.created_at).all())

    # -- agent dispatch -------------------------------------------------------

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
                    )
                )
                session.commit()
                raise OrchestratorError(
                    "policy gate blocked agent dispatch: " + "; ".join(decision.reasons)
                )

            job_input = self._build_job_input(run_id, ticket, plan, context)
            job = self._provider.create_job(job_input)

            from datetime import datetime, timezone

            session.add(
                FoundryAgentJob(
                    id=new_id("job"),
                    run_id=run_id,
                    provider=job.provider,
                    provider_job_id=job.job_id,
                    status=job.status.value,
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
            session.commit()
        return job

    # -- PR monitoring --------------------------------------------------------

    def record_pr(self, run_id: str, pr_state: PullRequestState) -> RunStatus:
        """Record an observed PR and decide the resulting run status.

        Forbidden-path changes block the run; oversized changes require human
        review; otherwise the PR is simply open.
        """
        violations = self._forbidden_violations(pr_state.files_changed)
        too_big = len(pr_state.files_changed) > self._max_files_changed

        with self._sf() as session:
            run = self._require_run(session, run_id)
            self._add(session, run_id, ArtifactType.PR_STATE, pr_state)
            session.add(
                build_audit_event(
                    run_id=run_id,
                    event_type=AuditEventType.PR_OPENED,
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
                job.pr_url = pr_state.url

            if violations:
                run.status = RunStatus.BLOCKED
                session.add(
                    build_audit_event(
                        run_id=run_id,
                        event_type=AuditEventType.RUN_BLOCKED,
                        actor_type="foundry",
                        metadata={"forbidden_files": violations},
                    )
                )
            elif too_big:
                run.status = RunStatus.REVIEW_REQUIRED
            else:
                run.status = RunStatus.PR_OPEN
            run.current_step = "pr_open"
            session.commit()
            return run.status

    # -- helpers --------------------------------------------------------------

    def _build_job_input(
        self,
        run_id: str,
        ticket: RawTicket,
        plan: DeliveryPlan,
        context: ContextBundle,
    ) -> CodingAgentJobInput:
        best_repo = context.best_repository
        assert best_repo is not None  # guaranteed by policy/readiness gating
        return CodingAgentJobInput(
            run_id=run_id,
            repo=best_repo.repo,
            branch_name=branch_name_for(ticket),
            ticket_url=f"https://linear.app/issue/{ticket.issue_key}",
            delivery_plan=plan,
            agent_instructions=plan.agent_instructions or "",
            constraints=JobConstraints(
                do_not_modify=list(DEFAULT_FORBIDDEN_GLOBS),
                required_tests=list(context.test_commands),
                max_files_changed=self._max_files_changed,
            ),
        )

    @staticmethod
    def _policy_input(
        action: PolicyAction,
        analysis: TicketAnalysis,
        context: ContextBundle,
        risk: RiskAssessment,
        approvals: set[str] | None = None,
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
            approval={role: True for role in (approvals or set())},
        )

    @staticmethod
    def _forbidden_violations(files: list[str]) -> list[str]:
        violations: list[str] = []
        for path in files:
            for pattern in DEFAULT_FORBIDDEN_GLOBS:
                if fnmatch.fnmatch(path, pattern):
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
