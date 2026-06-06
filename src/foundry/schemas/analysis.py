"""TicketAnalysis - structured output of the Ticket Intelligence Engine.

The analysis classifies the work and decides whether the ticket is ready for
implementation. Per the operating rules, it must NOT produce implementation
instructions unless acceptance criteria are sufficient.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .common import ImplementationReadiness, WorkType


class TicketAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    title: str
    work_type: WorkType
    summary: str
    user_problem: str | None = None
    business_value: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    ambiguity_score: int = Field(ge=0, le=100)
    implementation_readiness: ImplementationReadiness
    confidence: int = Field(ge=0, le=100)

    @property
    def is_ready_to_build(self) -> bool:
        """A ticket is buildable only when ready AND it has acceptance criteria.

        This encodes the hard rule "if acceptance criteria are missing, do not
        start coding" directly into the artifact, independent of the LLM's own
        ``implementation_readiness`` claim.
        """
        return (
            self.implementation_readiness is ImplementationReadiness.READY
            and len(self.acceptance_criteria) > 0
        )
