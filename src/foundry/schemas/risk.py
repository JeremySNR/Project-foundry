"""RiskAssessment - classification produced before any agent is launched.

Risk classification is advisory input to the policy gate. The *hard* decisions
(what is allowed) live in ``foundry.policy``, not in the LLM that fills this in.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .common import AgentMode, ApprovalRole, OverallRisk


class SensitiveAreas(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth: bool = False
    payments: bool = False
    customer_data: bool = False
    pii: bool = False
    database_migration: bool = False
    infrastructure: bool = False
    production_deploy: bool = False

    def any_set(self) -> bool:
        return any(self.model_dump().values())

    def names(self) -> list[str]:
        """Names of sensitive areas that are flagged true."""
        return [name for name, flagged in self.model_dump().items() if flagged]


class RiskAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall_risk: OverallRisk
    risk_reasons: list[str] = Field(default_factory=list)
    sensitive_areas: SensitiveAreas = Field(default_factory=SensitiveAreas)
    allowed_agent_mode: AgentMode
    required_approvals: list[ApprovalRole] = Field(default_factory=list)
    # Links back to the recorded OPA/policy decision that produced this view.
    policy_decision_id: str | None = None
