"""Foundry core data model.

Tables (from the build plan):

- ``foundry_runs``             - one row per Ticket-to-PR run.
- ``foundry_artifacts``        - versioned, content-hashed run artifacts.
- ``foundry_audit_events``     - append-only audit trail.
- ``foundry_policy_decisions`` - every policy gate decision.
- ``foundry_agent_jobs``       - coding-agent jobs dispatched for a run.
- ``foundry_repo_catalog``     - per-repo metadata synced from the GitHub org.
- ``foundry_run_outcomes``     - one denormalized row per *finished* run.

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
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from foundry.schemas.common import (
    ACTIVE_RUN_STATUSES,
    AgentMode,
    OverallRisk,
    RunStatus,
)

from .base import Base
from .tenant import DEFAULT_ORG_ID


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TenantScoped:
    """Mixin giving a table its ``org_id`` for row-level isolation (issue #156).

    Every tenant-scoped table inherits this column. It is ``NOT NULL`` with a
    ``server_default`` of :data:`~foundry.db.tenant.DEFAULT_ORG_ID` so existing
    rows (and any raw insert) land in the default org, and a Python-side default
    as a safety net. In practice the value is stamped from the active tenant
    context at flush time (``db/base.py``), so a row created while serving an
    authenticated org's request is written under *that* org. Indexed because
    every tenant-scoped read filters on it.
    """

    org_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=DEFAULT_ORG_ID,
        server_default=DEFAULT_ORG_ID,
        index=True,
    )


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
    AGENT_CANCELLED = "agent.cancelled"
    AGENT_REMEDIATION_REQUESTED = "agent.remediation_requested"
    PR_OPENED = "pr.opened"
    PR_UPDATED = "pr.updated"
    RISK_ESCALATED = "risk.escalated"
    CI_FAILED = "ci.failed"
    REVIEW_COMPLETED = "review.completed"
    RUN_COMPLETED = "run.completed"
    RUN_BLOCKED = "run.blocked"


# SQLAlchemy's Enum persists member *names* ('ANALYSING', ...), so the index
# predicate below must use names, not values. Derived from ACTIVE_RUN_STATUSES
# so the schema can never drift from the lifecycle definition.
_ACTIVE_STATUS_PREDICATE = "status IN ({})".format(
    ", ".join(sorted(f"'{s.name}'" for s in ACTIVE_RUN_STATUSES))
)


class FoundryRun(TenantScoped, Base):
    __tablename__ = "foundry_runs"

    # NOTE: linear_issue_id is deliberately not unique on its own - a ticket may
    # be re-analysed after clarification, rejection or failure. "At most one
    # *active* run per issue" is enforced by the partial unique index below
    # (migration 0006 on Postgres; create_all on SQLite dev), which backstops
    # the intake pre-check against concurrent webhook deliveries. Scoped by
    # org_id (issue #156) so two tenants can each have an active run for the
    # same upstream issue id; within one org the constraint is unchanged (and a
    # single-tenant deployment, all rows under the default org, is identical).
    __table_args__ = (
        Index(
            "uq_foundry_runs_one_active_per_issue",
            "org_id",
            "linear_issue_id",
            unique=True,
            sqlite_where=text(_ACTIVE_STATUS_PREDICATE),
            postgresql_where=text(_ACTIVE_STATUS_PREDICATE),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    linear_issue_id: Mapped[str] = mapped_column(String(128), index=True)
    linear_issue_key: Mapped[str] = mapped_column(String(64), index=True)
    # Epic decomposition (issue #35): a child run points at the parent run it
    # was split out of. Nullable and self-referential; single-level only (a
    # parent is itself a root - the orchestrator refuses to nest). Indexed so
    # listing an epic's children is a cheap lookup.
    parent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("foundry_runs.id"), nullable=True, index=True
    )
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
    # Self-referential epic linkage. ``children`` are the runs decomposed from
    # this one; ``parent`` is the epic a child belongs to (None for a root).
    children: Mapped[list["FoundryRun"]] = relationship(
        "FoundryRun", back_populates="parent"
    )
    parent: Mapped["FoundryRun | None"] = relationship(
        "FoundryRun", back_populates="children", remote_side=lambda: [FoundryRun.id]
    )


class FoundryArtifact(TenantScoped, Base):
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


class FoundryAuditEvent(TenantScoped, Base):
    __tablename__ = "foundry_audit_events"
    # The per-run sequence is the audit trail's guaranteed order. A unique
    # constraint makes a duplicate sequence (e.g. two unlocked sessions both
    # computing max(sequence)+1 under concurrency) fail loudly at insert rather
    # than silently corrupting the order (issue #10). State transitions now take
    # a row lock so this should never trip; if it does, that is a bug surfacing,
    # not data to keep.
    __table_args__ = (
        Index(
            "uq_audit_event_run_sequence", "run_id", "sequence", unique=True
        ),
    )

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
    # Cross-row tamper-evidence: sha256 linking this event to the previous event
    # in the run's trail (assigned at flush time in db.base, verified in
    # compliance.evidence). Nullable so legacy rows written before the chain
    # existed read back cleanly (issue #36).
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    run: Mapped[FoundryRun] = relationship(back_populates="audit_events")


class FoundryPolicyDecision(TenantScoped, Base):
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


class FoundryAgentJob(TenantScoped, Base):
    __tablename__ = "foundry_agent_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("foundry_runs.id"), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    provider_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[AgentJobStatus] = mapped_column(
        Enum(AgentJobStatus), default=AgentJobStatus.CREATED
    )
    # 255 to match FoundryRunOutcome.repo / FoundryRepoCatalog.repo - a long
    # "org/name" must not fail insert here mid-run when it fits everywhere else.
    repo: Mapped[str | None] = mapped_column(String(255), nullable=True)
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


class FoundryRunOutcome(TenantScoped, Base):
    """Delivery memory: one denormalized row per finished run.

    Every field is *derived* from rows that already exist (audit events, agent
    jobs, artifacts), so the row is a reproducible cache, never the source of
    truth - ``foundry-memory backfill`` can rebuild it for any terminal run.
    Priors mining and delivery metrics read this table instead of re-joining
    the audit trail on every request.

    ``outcome`` and the taxonomy columns are plain strings (not sa.Enum) so
    new values never need a Postgres ALTER TYPE.
    """

    __tablename__ = "foundry_run_outcomes"
    __table_args__ = (
        Index("idx_outcome_priors", "issue_key_prefix", "work_type", "repo", "outcome"),
        Index("idx_outcome_completed", "completed_at"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("foundry_runs.id"), primary_key=True
    )
    linear_issue_id: Mapped[str] = mapped_column(String(128), index=True)
    # Team proxy: the "ENG" in "ENG-123". RawTicket carries no team field.
    issue_key_prefix: Mapped[str] = mapped_column(String(16))
    # Terminal RunStatus mapped to a stable vocabulary:
    # merged / blocked / rejected / failed / needs_clarification.
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    # Where the work landed (latest agent job). NULL = never routed/dispatched.
    repo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Which agent shipped it (latest agent job's provider). NULL = never
    # dispatched. Feeds per-provider scorecards (which agent, by work type/repo).
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Best-candidate confidence at routing time, from the context_bundle
    # artifact - the raw material for precision-by-confidence-band calibration.
    routed_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    work_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    labels_json: Mapped[str] = mapped_column(Text, default="[]")
    risk_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    agent_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trigger_type: Mapped[str] = mapped_column(String(64))
    created_at_run: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the run actually finished (terminal audit event time).
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    time_to_merge_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Retries consumed = max(jobs_count - 1, 0).
    jobs_count: Mapped[int] = mapped_column(Integer, default=0)
    escalations_count: Mapped[int] = mapped_column(Integer, default=0)
    ci_failures_count: Mapped[int] = mapped_column(Integer, default=0)
    # From the latest pr_state artifact; seeds the future plan-vs-diff gate.
    files_changed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Block taxonomy: forbidden_paths / pr_closed_unmerged / policy_denied /
    # human_stopped / unroutable. NULL unless outcome is blocked.
    blocked_reason_category: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    # Deliberately NULL in v1: justification is derived on read (a later run on
    # the same issue merging is the supersession proxy), never guessed at write.
    block_justified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    run: Mapped[FoundryRun] = relationship()


class FoundryWebhookDelivery(TenantScoped, Base):
    """Durable, bounded webhook idempotency / replay guard.

    One row per processed inbound delivery. The unique ``(provider,
    delivery_id)`` constraint makes dedup *atomic* (concurrent workers race to
    INSERT; the loser gets an IntegrityError and treats the delivery as a
    duplicate) and *durable* (survives a restart) - unlike the old in-process
    ``set``, which was per-process, lost on restart, and grew without bound.

    Rows older than the configured TTL are pruned opportunistically so the
    table stays small; the TTL is required to be >= the replay-age window so a
    delivery can never age out of this table while still being fresh enough to
    replay.

    Not tied to a run: a duplicate may arrive before the run row exists (or for
    a delivery that never starts a run at all), so this is a standalone table.
    """

    __tablename__ = "foundry_webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("provider", "delivery_id", name="uq_webhook_delivery"),
        Index("idx_webhook_delivery_received", "received_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # "linear" / "github" / ... - the same delivery id from two providers is
    # not a collision, so the uniqueness is per (provider, delivery_id).
    provider: Mapped[str] = mapped_column(String(32))
    delivery_id: Mapped[str] = mapped_column(String(255))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class FoundryRepoCatalogEntry(TenantScoped, Base):
    """One row per repository in the org: the metadata the enricher scores against.

    Metadata plus, when code-facts sync is enabled, narrowly-scoped code facts:
    tree paths (capped), CODEOWNERS rules, and root dependency manifests - never
    a clone, never arbitrary file contents. ``synced_at`` is when we last
    deep-fetched; ``pushed_at`` is GitHub's last-push time refreshed on every
    sweep. ``pushed_at > synced_at`` means the entry is stale.
    """

    __tablename__ = "foundry_repo_catalog"

    repo: Mapped[str] = mapped_column(String(255), primary_key=True)  # "org/name"
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    topics: Mapped[str] = mapped_column(Text, default="[]")            # JSON list[str]
    primary_language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    default_branch: Mapped[str | None] = mapped_column(String(128), nullable=True)
    readme_head: Mapped[str | None] = mapped_column(Text, nullable=True)   # first 4096 chars
    top_dirs: Mapped[str] = mapped_column(Text, default="[]")          # JSON list[str]
    recent_pr_titles: Mapped[str] = mapped_column(Text, default="[]")  # JSON list[str]
    top_contributors: Mapped[str] = mapped_column(Text, default="[]")  # JSON list[str] of logins
    # Code facts (populated only when sync runs with code facts enabled).
    tree_paths: Mapped[str] = mapped_column(Text, default="[]")        # JSON list[str], capped
    tree_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    test_layout: Mapped[str] = mapped_column(Text, default="[]")       # JSON list[str]
    codeowners: Mapped[str] = mapped_column(Text, default="[]")        # JSON list[{pattern, owners}]
    manifests: Mapped[str] = mapped_column(Text, default="[]")         # JSON list[ManifestFacts-shaped]
    languages: Mapped[str] = mapped_column(Text, default="{}")         # JSON dict ext -> file count
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(128), nullable=True)  # reserved, unused
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# Every table carrying an ``org_id`` (issue #156). The session machinery in
# ``db/base.py`` reads this tuple to stamp the active org on new rows at flush
# time and to filter every ORM SELECT to the current org, so a unit of work
# scoped to one tenant can neither read nor write another tenant's rows. Adding
# a new tenant-scoped table means adding it here (and inheriting TenantScoped).
TENANT_SCOPED_MODELS: tuple[type[TenantScoped], ...] = (
    FoundryRun,
    FoundryArtifact,
    FoundryAuditEvent,
    FoundryPolicyDecision,
    FoundryAgentJob,
    FoundryRunOutcome,
    FoundryWebhookDelivery,
    FoundryRepoCatalogEntry,
)
