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

# Actions that may never run autonomously in this version, regardless of risk
# level or approvals. Evaluating them produces a recorded deny decision.
_FORBIDDEN_ACTIONS = frozenset(
    {PolicyAction.AUTO_MERGE, PolicyAction.PRODUCTION_DEPLOY}
)


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


class PolicyRetry(BaseModel):
    """Remediation attempt counters; only meaningful for ``retry_agent``."""

    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=2, ge=0)


class PolicyBudget(BaseModel):
    """Run spend vs the configured cap; checked for every spending action.

    The gate evaluates *projected* spend - ``cost_usd`` (recorded so far) plus
    ``pending_cost_usd`` (the estimated cost of the dispatch about to happen) -
    so a run can be stopped before it blows the budget, not just after. With
    ``pending_cost_usd`` at its default of 0 the check is the historical
    "already-spent vs cap" comparison, so existing behaviour is unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    cost_usd: float = Field(default=0.0, ge=0)
    pending_cost_usd: float = Field(default=0.0, ge=0)
    # None = no budget cap configured.
    max_cost_usd: float | None = Field(default=None, gt=0)


class PolicyInput(BaseModel):
    """The full context handed to the policy gate for a single action."""

    model_config = ConfigDict(extra="forbid")

    action: PolicyAction
    actor: PolicyActor = Field(default_factory=PolicyActor)
    ticket: PolicyTicket = Field(default_factory=PolicyTicket)
    risk: PolicyRisk = Field(default_factory=PolicyRisk)
    repo: PolicyRepo = Field(default_factory=PolicyRepo)
    retry: PolicyRetry = Field(default_factory=PolicyRetry)
    budget: PolicyBudget = Field(default_factory=PolicyBudget)
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


# Actions that actually launch or progress autonomous work.
_AUTONOMOUS_ACTIONS = frozenset(
    {
        PolicyAction.START_AGENT,
        PolicyAction.CREATE_BRANCH,
        PolicyAction.OPEN_PR,
        PolicyAction.RETRY_AGENT,
        PolicyAction.MARK_COMPLETE,
    }
)

# Actions that launch a coding agent and therefore spend against the run
# budget. The budget cap is enforced for these at *every* attempt, including
# the first dispatch (issue #29) - not only on retries.
_SPEND_ACTIONS = frozenset(
    {PolicyAction.START_AGENT, PolicyAction.RETRY_AGENT}
)

# Read-only / advisory actions: always allowed, still recorded. This is an
# explicit allowlist - anything not listed here or in _AUTONOMOUS_ACTIONS is
# denied (default-deny), so a new action cannot slip through ungoverned.
_ADVISORY_ACTIONS = frozenset(
    {
        PolicyAction.ANALYSE_TICKET,
        PolicyAction.CREATE_PLAN,
        PolicyAction.REQUEST_APPROVAL,
        PolicyAction.REQUEST_CHANGES,
    }
)


def _action_str(action: object) -> str:
    """Render an action for a reason string.

    ``PolicyInput.action`` is a typed :class:`PolicyAction` on the happy path,
    but the default-deny branch must also cope with an unrecognised value that
    bypassed the enum (e.g. ``model_construct`` or a future non-enum caller) -
    otherwise the safety net would crash on ``.value`` instead of denying.
    """
    return action.value if isinstance(action, PolicyAction) else str(action)


def required_approvals(risk: PolicyRisk) -> list[ApprovalRole]:
    """Derive required approval roles from the sensitive areas in play.

    Shared by the gate and the orchestrator: ``approve()`` uses it to refuse an
    approval from someone lacking a required role *before* recording it, so the
    audit trail never shows an approval the gate will only reject at dispatch.
    """
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

    def __init__(
        self, *, repo_confidence_threshold: int = REPO_CONFIDENCE_THRESHOLD
    ) -> None:
        self._repo_confidence_threshold = repo_confidence_threshold

    def evaluate(self, payload: PolicyInput) -> PolicyDecision:
        reasons: list[str] = []
        required = required_approvals(payload.risk)
        threshold = self._repo_confidence_threshold

        # Hard-forbidden actions are denied unconditionally - no risk level or
        # approval can unlock them in this version.
        if payload.action in _FORBIDDEN_ACTIONS:
            return PolicyDecision(
                policy_name=self.policy_name,
                allowed=False,
                reasons=[
                    f"action '{_action_str(payload.action)}' may never run "
                    "autonomously in this version"
                ],
                allowed_agent_mode=AgentMode.HUMAN_ONLY,
                required_approvals=required,
            )

        # Read-only actions never need the autonomous-work gate, but we still
        # surface required approvals so the UI can plan ahead.
        if payload.action in _ADVISORY_ACTIONS:
            return PolicyDecision(
                policy_name=self.policy_name,
                allowed=True,
                reasons=[
                    f"action '{_action_str(payload.action)}' is read-only / advisory"
                ],
                allowed_agent_mode=self._allowed_mode(payload, blocked=False),
                required_approvals=required,
            )

        # Default-deny: an action this policy does not recognise is refused.
        if payload.action not in _AUTONOMOUS_ACTIONS:
            return PolicyDecision(
                policy_name=self.policy_name,
                allowed=False,
                reasons=[
                    f"action '{_action_str(payload.action)}' is not covered by this "
                    "policy; denying by default"
                ],
                allowed_agent_mode=AgentMode.HUMAN_ONLY,
                required_approvals=required,
            )

        # --- hard blocks (MVP) ---
        if payload.risk.production_deploy:
            reasons.append("production deployment is blocked in the MVP")
        if payload.risk.database_migration:
            reasons.append("database migrations are blocked in the MVP")
        if payload.repo.confidence < threshold:
            reasons.append(
                f"repository confidence {payload.repo.confidence} is below the "
                f"threshold of {threshold}"
            )
        if payload.ticket.readiness is not ImplementationReadiness.READY:
            reasons.append(
                f"ticket readiness is '{payload.ticket.readiness.value}', not 'ready'"
            )
        if payload.risk.overall_risk is OverallRisk.BLOCKED:
            reasons.append("risk assessment marked the work as blocked")
        if (
            payload.action is PolicyAction.RETRY_AGENT
            and payload.retry.attempt > payload.retry.max_attempts
        ):
            reasons.append(
                f"remediation attempt {payload.retry.attempt} exceeds the "
                f"maximum of {payload.retry.max_attempts}"
            )
        if (
            payload.action in _SPEND_ACTIONS
            and payload.budget.max_cost_usd is not None
        ):
            projected = payload.budget.cost_usd + payload.budget.pending_cost_usd
            if projected >= payload.budget.max_cost_usd:
                reasons.append(
                    f"projected run spend ${projected:.2f} would reach the "
                    f"budget cap of ${payload.budget.max_cost_usd:.2f}"
                )

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


def _urllib_http_post(url: str, body: dict) -> dict:  # pragma: no cover - network
    """Minimal stdlib JSON POST used as the default OPA transport.

    Kept on stdlib (``urllib``) so wiring an OPA server adds no dependency. Tests
    never reach this path - they inject a fake ``http_post`` (see invariant #3).
    """
    import json
    import urllib.request

    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


class OpaPolicyEngine:
    """Delegates decisions to an OPA HTTP server (production backend).

    The OPA bundle lives alongside this module (``foundry.rego``). This class is
    intentionally thin; the network client is injected so it stays testable and
    so the core foundation has no hard dependency on a running OPA instance.

    Wired via ``policy.provider: opa`` (+ ``policy.opa_url``) in config; the
    Python ``LocalPolicyEngine`` remains the default. The configurable
    ``repo_confidence_threshold`` is injected into the payload so the Rego bundle
    evaluates against the *same* threshold as the Python engine instead of its
    hardcoded fallback - the two backends cannot silently diverge on it.
    """

    policy_name = "foundry.ticket_to_pr.v1"

    def __init__(
        self,
        *,
        base_url: str,
        repo_confidence_threshold: int = REPO_CONFIDENCE_THRESHOLD,
        http_post=None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._repo_confidence_threshold = repo_confidence_threshold
        self._http_post = http_post or _urllib_http_post

    def evaluate(self, payload: PolicyInput) -> PolicyDecision:
        url = f"{self._base_url}/v1/data/foundry/ticket_to_pr/decision"
        opa_input = payload.model_dump(mode="json")
        opa_input["repo_confidence_threshold"] = self._repo_confidence_threshold
        try:
            response = self._http_post(url, {"input": opa_input})
            result = response.get("result")
            if not result:
                raise ValueError("OPA returned no decision result")
            return PolicyDecision(
                policy_name=self.policy_name,
                allowed=bool(result["allow"]),
                reasons=list(result.get("reasons", [])),
                allowed_agent_mode=AgentMode(result.get("allowed_agent_mode", "human_only")),
                required_approvals=[
                    ApprovalRole(r) for r in result.get("required_approvals", [])
                ],
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise RuntimeError(f"OPA policy evaluation failed: {exc}") from exc


def default_engine() -> PolicyEngine:
    """The engine used when no OPA server is configured."""
    return LocalPolicyEngine()
