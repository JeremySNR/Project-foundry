"""Rendering Foundry's view back into the tracker.

Two things the orchestrator writes to Linear:

- a concise analysis/plan comment when a run is planned, and
- a ``Foundry: ...`` workflow state that mirrors the run status.

Comments are kept short and skimmable - the ticket is the system of record for
delivery status, not a wall of text.
"""

from __future__ import annotations

from foundry.schemas.analysis import TicketAnalysis
from foundry.schemas.common import RunStatus
from foundry.schemas.plan import DeliveryPlan
from foundry.schemas.risk import RiskAssessment

# Suggested Linear workflow states, mapped from the run status.
_STATE_NAMES: dict[RunStatus, str] = {
    RunStatus.ANALYSING: "Foundry: Analysing",
    RunStatus.NEEDS_CLARIFICATION: "Foundry: Needs Clarification",
    RunStatus.PLAN_READY: "Foundry: Plan Ready",
    RunStatus.WAITING_APPROVAL: "Foundry: Waiting Approval",
    RunStatus.APPROVED: "Foundry: Approved",
    RunStatus.AGENT_RUNNING: "Foundry: Agent Running",
    RunStatus.PR_OPEN: "Foundry: PR Open",
    RunStatus.REVIEW_REQUIRED: "Foundry: Review Required",
    RunStatus.COMPLETE: "Foundry: Complete",
    RunStatus.BLOCKED: "Foundry: Blocked",
    RunStatus.EXECUTION_FAILED: "Foundry: Blocked",
    RunStatus.REJECTED: "Foundry: Blocked",
}


def state_for(status: RunStatus) -> str:
    return _STATE_NAMES.get(status, f"Foundry: {status.value.replace('_', ' ').title()}")


def _bullets(items: list[str], *, empty: str = "_none_") -> str:
    return "\n".join(f"- {i}" for i in items) if items else empty


def format_analysis_comment(
    analysis: TicketAnalysis,
    risk: RiskAssessment,
    plan: DeliveryPlan,
    status: RunStatus,
) -> str:
    """Render the planning summary comment posted to the issue."""
    repo = plan.affected_repositories[0] if plan.affected_repositories else "_unknown_"
    approvals = [r.value for r in risk.required_approvals]
    lines = [
        "**Foundry analysis complete.**",
        "",
        f"- Work type: `{analysis.work_type.value}`",
        f"- Readiness: `{analysis.implementation_readiness.value}`",
        f"- Risk: `{risk.overall_risk.value}` "
        f"(suggested mode: `{risk.allowed_agent_mode.value}`)",
        f"- Affected repo: `{repo}`",
        "",
        "**Acceptance criteria**",
        _bullets(analysis.acceptance_criteria),
    ]
    if analysis.missing_information:
        lines += ["", "**Missing information**", _bullets(analysis.missing_information)]
    if plan.implementation_steps:
        lines += [
            "",
            "**Plan**",
            "\n".join(f"{s.step}. {s.description}" for s in plan.implementation_steps),
        ]
    if approvals:
        lines += ["", f"**Required approval:** {', '.join(approvals)}"]

    if status is RunStatus.WAITING_APPROVAL:
        lines += [
            "",
            "Reply to proceed:",
            "`/foundry approve` · `/foundry reject` · `/foundry stop`",
        ]
    elif status is RunStatus.NEEDS_CLARIFICATION:
        lines += ["", "_Needs clarification before an agent can start._"]
    elif status is RunStatus.BLOCKED:
        lines += ["", "_Blocked: " + "; ".join(risk.risk_reasons or ["see policy"]) + "._"]
    return "\n".join(lines)


def format_cursor_delegation(agent_instructions: str) -> str:
    """The @Cursor delegation comment that hands approved work to Cursor.

    Foundry has already gathered context, classified risk and obtained approval;
    this passes the *governed* instructions to Cursor's Linear integration, which
    runs the cloud agent, reports status in Linear and opens the PR.
    """
    return (
        "@Cursor please implement this. Work strictly within the scope below; "
        "Foundry has approved it.\n\n"
        f"{agent_instructions}"
    )
