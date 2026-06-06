"""Pydantic contracts for every artifact in a Foundry run."""

from __future__ import annotations

from .agent import (
    CodingAgentJob,
    CodingAgentJobInput,
    CodingAgentJobStatus,
    JobConstraints,
)
from .analysis import TicketAnalysis
from .common import (
    REPO_CONFIDENCE_THRESHOLD,
    SENSITIVE_AREA_KEYS,
    AgentJobStatus,
    AgentMode,
    ApprovalRole,
    CIStatus,
    ImplementationReadiness,
    OverallRisk,
    PolicyAction,
    PRStatus,
    ReviewStatus,
    RunStatus,
    WorkType,
)
from .context import CandidateFile, CandidateRepository, ContextBundle
from .plan import DeliveryPlan, ImplementationStep, TestPlan
from .pr import PullRequestState
from .risk import RiskAssessment, SensitiveAreas
from .ticket import LinkedResource, RawTicket

__all__ = [
    # enums / constants
    "WorkType",
    "ImplementationReadiness",
    "OverallRisk",
    "AgentMode",
    "ApprovalRole",
    "RunStatus",
    "PRStatus",
    "CIStatus",
    "ReviewStatus",
    "AgentJobStatus",
    "PolicyAction",
    "REPO_CONFIDENCE_THRESHOLD",
    "SENSITIVE_AREA_KEYS",
    # analysis
    "TicketAnalysis",
    # context
    "ContextBundle",
    "CandidateRepository",
    "CandidateFile",
    # risk
    "RiskAssessment",
    "SensitiveAreas",
    # plan
    "DeliveryPlan",
    "ImplementationStep",
    "TestPlan",
    # ticket
    "RawTicket",
    "LinkedResource",
    # pr
    "PullRequestState",
    # agent
    "CodingAgentJobInput",
    "CodingAgentJob",
    "CodingAgentJobStatus",
    "JobConstraints",
]
