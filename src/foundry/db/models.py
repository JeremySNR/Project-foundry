"""Foundry core data model.

Tables (from the build plan):

- ``foundry_runs``             - one row per Ticket-to-PR run.
- ``foundry_artifacts``        - versioned, content-hashed run artifacts.
- ``foundry_audit_events``     - append-only audit trail.
- ``foundry_policy_decisions`` - every policy gate decision.
- ``foundry_agent_jobs``       - coding-agent jobs dispatched for a run.

Artifact and audit rows carry a content hash so the immutable input snapshot and
every decision can be verified after the fact.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from foundry.schemas.common import AgentMode, OverallRisk, RunStatus

from .base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArtifactType(str, enum.Enum):
    TICKET_SNAPSHOT = "ticket_snapshot"
    TICKET_ANALYSIS = "ticket_analysis"
    CONTEXT_BUNDLE = "context_bundle"
    RISK_ASSESSMENT = "risk_assessment"
    DELIVERY_PLAN = "delivery_plan"
    APPROVAL_RECORD = "approval_record"
    AGENT_JOB = "agent_job"
    PR_STATE = "pr_state"
    FINAL_SUMMARY = "final_summary"


class AgentJobStatus(str, enum.Enum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AuditEventType(str, enum.Enum):
    RUN_STARTED = "run.started"
    TICKET_FETCHED = "ticket.fetched"
    ANALYSIS_COMPLETED = "analysis.completed"
    CONTEXT_COMPLETED = "context.completed"
    POLICY_EVALUATED = "policy.evaluated"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    AGENT_STARTED = "agent.started"
    AGENT_FAILED = "agent.failed"
    AGENT_REMEDIATION_REQUESTED = "agent.remediation_requested"
    PR_OPENED = "pr.opened"
    PR_UPDATED = "pr.updated"
    RISK_ESCALATED = "risk.escalated"
    CI_FAILED = "ci.failed"
    REVIEW_COMPLETED = "review.completed"
    RUN_COMPLETED = "run.completed"
    RUN_BLOCKED = "run.blocked"


class FoundryRun(Base):
    __tablename__ = "foundry_runs"

    # NOTE: deliberately no unique constraint on linear_issue_id - a ticket may
    # be re-analysed after clarification, rejection or failure. "At most one
    # *active* run per issue" is enforced at intake, not by the schema.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    linear_issue_id: Mapped[str] = mapped_column(String(128), index=True)
    linear_issue_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus), default=RunStatus.ANALYSING
    )
    trigger_type: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_level: Mapped[OverallRisk | None] = mapped_column(
        Enum(OverallRisk), nullable=True
    )
    agent_mode: Mapped[AgentMode | None] = mapped_column(
        Enum(AgentMode), nullable=True
    )
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    artifacts: Mapped[list["FoundryArtifact"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    audit_events: Mapped[list["FoundryAuditEvent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    policy_decisions: Mapped[list["FoundryPolicyDecision"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    agent_jobs: Mapped[list["FoundryAgentJob"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class FoundryArtifact(Base):
    __tablename__ = "foundry_artifacts"
    __table_args__ = (Index("idx_artifact_run_type", "run_id", "artifact_type"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("foundry_runs.id"), index=True)
    artifact_type: Mapped[ArtifactType] = mapped_column(Enum(ArtifactType))
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_json: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    run: Mapped[FoundryRun] = relationship(back_populates="artifacts")


class FoundryAuditEvent(Base):
    __tablename__ = "foundry_audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("foundry_runs.id"), index=True)
    # Monotonic per-run sequence number so audit events have a guaranteed order
    # independent of sub-millisecond timestamp ties.
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    event_type: Mapped[AuditEventType] = mapped_column(Enum(AuditEventType))
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    run: Mapped[FoundryRun] = relationship(back_populates="audit_events")


class FoundryPolicyDecision(Base):
    __tablename__ = "foundry_policy_decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("foundry_runs.id"), index=True)
    policy_name: Mapped[str] = mapped_column(String(128))
    input_json: Mapped[str] = mapped_column(Text)
    decision_json: Mapped[str] = mapped_column(Text)
    allowed: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    run: Mapped[FoundryRun] = relationship(back_populates="policy_decisions")


class FoundryAgentJob(Base):
    __tablename__ = "foundry_agent_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("foundry_runs.id"), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    provider_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[AgentJobStatus] = mapped_column(
        Enum(AgentJobStatus), default=AgentJobStatus.CREATED
    )
    repo: Mapped[str | None] = mapped_column(String(128), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Provider-reported spend; None = the provider does not expose usage.
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    run: Mapped[FoundryRun] = relationship(back_populates="agent_jobs")
