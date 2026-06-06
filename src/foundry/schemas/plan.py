"""DeliveryPlan - the coding-agent-ready plan produced from a ready ticket.

Hard rules from the build plan: include scope and out-of-scope, a test plan,
stop conditions, forbidden changes, and a PR description template. The plan must
not contain ``agent_instructions`` when acceptance criteria are insufficient.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ImplementationStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: int = Field(ge=1)
    description: str
    expected_output: str


class TestPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_tests: list[str] = Field(default_factory=list)
    integration_tests: list[str] = Field(default_factory=list)
    e2e_tests: list[str] = Field(default_factory=list)
    manual_checks: list[str] = Field(default_factory=list)


class DeliveryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    affected_repositories: list[str] = Field(default_factory=list)
    expected_files_or_areas: list[str] = Field(default_factory=list)
    implementation_steps: list[ImplementationStep] = Field(default_factory=list)
    test_plan: TestPlan = Field(default_factory=TestPlan)
    rollback_considerations: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    # Must be None until the ticket is genuinely ready to build.
    agent_instructions: str | None = None
