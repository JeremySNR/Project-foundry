"""RiskAssessment - classification produced before any agent is launched.

Risk classification is advisory input to the policy gate. The *hard* decisions
(what is allowed) live in ``foundry.policy``, not in the LLM that fills this in.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import AgentMode, ApprovalRole, OverallRisk


class RiskEvidence(BaseModel):
    """A single cited reason a risk flag was raised.

    ``source`` records which pass produced it: the deterministic keyword/glob
    heuristics ("heuristic"/"diff") or the LLM risk pass ("llm").
    """

    model_config = ConfigDict(extra="forbid")

    # A sensitive-area name (see SensitiveAreas), or "overall" for findings
    # that are not tied to one area.
    area: str
    # The citation itself, e.g. "keyword 'jwt' in ticket title/description"
    # or "touches session issuance in auth/tokens.py".
    detail: str
    source: Literal["heuristic", "llm", "diff"]


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
        """Names of sensitive areas that are flagged true, in deterministic order."""
        return sorted(name for name, flagged in self.model_dump().items() if flagged)


class RiskAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall_risk: OverallRisk
    risk_reasons: list[str] = Field(default_factory=list)
    sensitive_areas: SensitiveAreas = Field(default_factory=SensitiveAreas)
    allowed_agent_mode: AgentMode
    required_approvals: list[ApprovalRole] = Field(default_factory=list)
    # Cited evidence per flag. Defaulted so artifacts recorded before this
    # field existed still validate when loaded back.
    evidence: list[RiskEvidence] = Field(default_factory=list)
    # Links back to the recorded OPA/policy decision that produced this view.
    policy_decision_id: str | None = None


class DiffRiskFindings(BaseModel):
    """Diff-stage classification result: which sensitive areas a set of
    changed file paths touches, with cited evidence."""

    model_config = ConfigDict(extra="forbid")

    # area -> sorted list of matching changed files
    areas: dict[str, list[str]] = Field(default_factory=dict)
    evidence: list[RiskEvidence] = Field(default_factory=list)
