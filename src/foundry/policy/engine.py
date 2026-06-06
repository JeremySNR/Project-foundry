"""Foundry policy gate.

Policy decisions are *hard rules*, not prompts. Every risky action passes
through here before it is allowed to proceed.

Two backends are provided:

- :class:`LocalPolicyEngine` - a pure-Python evaluator that mirrors the Rego
  bundle in ``foundry.rego``. It is the default so the foundation is testable
  with no OPA server running.
- :class:`OpaPolicyEngine` - delegates to an OPA HTTP endpoint for production.
  It shares the same input/decision contracts.

Both return a :class:`PolicyDecision`, which the audit layer persists as a
``foundry_policy_decisions`` row.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from foundry.schemas.common import (
    REPO_CONFIDENCE_THRESHOLD,
    AgentMode,
    ApprovalRole,
    ImplementationReadiness,
    OverallRisk,
    PolicyAction,
)

# Actions that, in the MVP, may never run autonomously regardless of approvals.
_MVP_FORBIDDEN_ACTIONS = frozenset({"auto_merge", "production_deploy"})


class PolicyTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_type: str = "unknown"
    readiness: ImplementationReadiness = ImplementationReadiness.NEEDS_CLARIFICATION


class PolicyRisk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall_risk: OverallRisk = OverallRisk.MEDIUM
    auth: bool = False
    payments: bool = False
    customer_data: bool = False
    pii: bool = False
    database_migration: bool = False
    infrastructure: bool = False
    production_deploy: bool = False


class PolicyRepo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    confidence: int = Field(default=0, ge=0, le=100)


class PolicyActor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "foundry"
    user: str = "agent-system"


class PolicyInput(BaseModel):
    """The full context handed to the policy gate for a single action."""

    model_config = ConfigDict(extra="forbid")

    action: PolicyAction
    actor: PolicyActor = Field(default_factory=PolicyActor)
    ticket: PolicyTicket = Field(default_factory=PolicyTicket)
    risk: PolicyRisk = Field(default_factory=PolicyRisk)
    repo: PolicyRepo = Field(default_factory=PolicyRepo)
    # Map of approval role -> granted. Missing keys are treated as not granted.
    approval: dict[str, bool] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    policy_name: str
    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    # The strongest agent mode permitted given the inputs.
    allowed_agent_mode: AgentMode = AgentMode.HUMAN_ONLY
    required_approvals: list[ApprovalRole] = Field(default_factory=list)


class PolicyEngine(Protocol):
    def evaluate(self, payload: PolicyInput) -> PolicyDecision: ...


# Actions that actually launch or progress autonomous work. Read-only actions
# (analysis, planning) are always allowed but still recorded.
_AUTONOMOUS_ACTIONS = frozenset(
    {
        PolicyAction.START_AGENT,
        PolicyAction.CREATE_BRANCH,
        PolicyAction.OPEN_PR,
        PolicyAction.RETRY_AGENT,
        PolicyAction.MARK_COMPLETE,
    }
)


def _required_approvals(risk: PolicyRisk) -> list[ApprovalRole]:
    """Derive required approval roles from the sensitive areas in play."""
    required: list[ApprovalRole] = []
    if risk.auth or risk.infrastructure:
        required.append(ApprovalRole.ENGINEERING)
    if risk.customer_data or risk.pii or risk.payments:
        required.append(ApprovalRole.SECURITY)
    # De-duplicate while preserving order.
    seen: set[ApprovalRole] = set()
    ordered: list[ApprovalRole] = []
    for role in required:
        if role not in seen:
            seen.add(role)
            ordered.append(role)
    return ordered


class LocalPolicyEngine:
    """Pure-Python implementation of the minimum policy rules.

    Kept deliberately close to ``foundry.rego`` so the two stay in lock-step.
    """

    policy_name = "foundry.ticket_to_pr.v1"

    def evaluate(self, payload: PolicyInput) -> PolicyDecision:
        reasons: list[str] = []
        required = _required_approvals(payload.risk)

        # Read-only actions never need the autonomous-work gate, but we still
        # surface required approvals so the UI can plan ahead.
        if payload.action not in _AUTONOMOUS_ACTIONS:
            return PolicyDecision(
                policy_name=self.policy_name,
                allowed=True,
                reasons=[f"action '{payload.action.value}' is read-only / advisory"],
                allowed_agent_mode=self._allowed_mode(payload, blocked=False),
                required_approvals=required,
            )

        # --- hard blocks (MVP) ---
        if payload.risk.production_deploy:
            reasons.append("production deployment is blocked in the MVP")
        if payload.risk.database_migration:
            reasons.append("database migrations are blocked in the MVP")
        if payload.repo.confidence < REPO_CONFIDENCE_THRESHOLD:
            reasons.append(
                f"repository confidence {payload.repo.confidence} is below the "
                f"threshold of {REPO_CONFIDENCE_THRESHOLD}"
            )
        if payload.ticket.readiness is not ImplementationReadiness.READY:
            reasons.append(
                f"ticket readiness is '{payload.ticket.readiness.value}', not 'ready'"
            )
        if payload.risk.overall_risk is OverallRisk.BLOCKED:
            reasons.append("risk assessment marked the work as blocked")

        # --- sensitive areas require explicit approval ---
        for role in required:
            if not payload.approval.get(role.value, False):
                reasons.append(
                    f"sensitive work requires '{role.value}' approval, which is missing"
                )

        allowed = len(reasons) == 0
        if allowed:
            reasons.append("all minimum policy checks passed")

        return PolicyDecision(
            policy_name=self.policy_name,
            allowed=allowed,
            reasons=reasons,
            allowed_agent_mode=self._allowed_mode(payload, blocked=not allowed),
            required_approvals=required,
        )

    @staticmethod
    def _allowed_mode(payload: PolicyInput, *, blocked: bool) -> AgentMode:
        """Draft PR is permitted only for low/medium risk; otherwise human-only."""
        if blocked:
            return AgentMode.HUMAN_ONLY
        if payload.risk.overall_risk in (OverallRisk.LOW, OverallRisk.MEDIUM):
            return AgentMode.DRAFT_PR
        return AgentMode.HUMAN_ONLY


class OpaPolicyEngine:
    """Delegates decisions to an OPA HTTP server (production backend).

    The OPA bundle lives alongside this module (``foundry.rego``). This class is
    intentionally thin; the network client is injected so it stays testable and
    so the core foundation has no hard dependency on a running OPA instance.
    """

    policy_name = "foundry.ticket_to_pr.v1"

    def __init__(self, *, base_url: str, http_post=None) -> None:
        self._base_url = base_url.rstrip("/")
        self._http_post = http_post

    def evaluate(self, payload: PolicyInput) -> PolicyDecision:
        if self._http_post is None:  # pragma: no cover - requires injected client
            raise RuntimeError(
                "OpaPolicyEngine requires an injected http_post callable to reach OPA"
            )
        url = f"{self._base_url}/v1/data/foundry/ticket_to_pr/decision"
        response = self._http_post(url, {"input": payload.model_dump(mode="json")})
        result = response["result"]
        return PolicyDecision(
            policy_name=self.policy_name,
            allowed=bool(result["allow"]),
            reasons=list(result.get("reasons", [])),
            allowed_agent_mode=AgentMode(result.get("allowed_agent_mode", "human_only")),
            required_approvals=[
                ApprovalRole(r) for r in result.get("required_approvals", [])
            ],
        )


def default_engine() -> PolicyEngine:
    """The engine used when no OPA server is configured."""
    return LocalPolicyEngine()
