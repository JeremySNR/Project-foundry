"""Coding-agent job contracts.

These are provider-agnostic: every ``CodingAgentProvider`` (Cursor, Claude Code,
OpenAI agent, manual) accepts the same ``CodingAgentJobInput`` and reports the
same ``CodingAgentJob`` shape. Secrets must never travel in these objects.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .common import AgentJobStatus
from .plan import DeliveryPlan


class JobConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    do_not_modify: list[str] = Field(default_factory=list)
    required_tests: list[str] = Field(default_factory=list)
    max_files_changed: int = Field(default=12, ge=1)
    allow_new_dependencies: bool = False


class CodingAgentJobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    repo: str
    base_branch: str = "main"
    branch_name: str
    ticket_url: str
    delivery_plan: DeliveryPlan
    agent_instructions: str
    constraints: JobConstraints = Field(default_factory=JobConstraints)


class CodingAgentJob(BaseModel):
    """Handle returned when a job is created with a provider."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    provider: str
    status: AgentJobStatus = AgentJobStatus.CREATED


class CodingAgentJobStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    provider: str
    status: AgentJobStatus
    branch: str | None = None
    pr_url: str | None = None
    error: str | None = None
