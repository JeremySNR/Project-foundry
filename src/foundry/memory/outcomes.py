"""Per-run outcome derivation - closing the loop when a run finishes.

``derive_outcome`` is a pure read over rows that already exist (the run, its
agent jobs, audit events and artifacts), so the outcome row is a reproducible
cache: the orchestrator writes it at terminal transitions and
``foundry-memory backfill`` rebuilds it for runs that finished before this
table existed. The two paths share this one derivation, by design.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import func

from foundry.audit.events import build_artifact
from foundry.db.models import (
    ArtifactType,
    AuditEventType,
    FoundryAgentJob,
    FoundryArtifact,
    FoundryAuditEvent,
    FoundryRun,
    FoundryRunOutcome,
)
from foundry.schemas.common import RunStatus, TERMINAL_RUN_STATUSES

# Terminal RunStatus -> the stable outcome vocabulary stored on the row.
_OUTCOME_FOR_STATUS = {
    RunStatus.COMPLETE: "merged",
    RunStatus.BLOCKED: "blocked",
    RunStatus.REJECTED: "rejected",
    RunStatus.EXECUTION_FAILED: "failed",
    RunStatus.NEEDS_CLARIFICATION: "needs_clarification",
}

# Audit events that mark the moment a run finished, by outcome.
_TERMINAL_EVENT_TYPES = (
    AuditEventType.RUN_COMPLETED,
    AuditEventType.RUN_BLOCKED,
    AuditEventType.APPROVAL_REJECTED,
    AuditEventType.AGENT_FAILED,
)


_BLOCK_CATEGORIES = frozenset(
    {
        "forbidden_paths",
        "pr_closed_unmerged",
        "policy_denied",
        "human_stopped",
        "unroutable",
    }
)


def _utc(dt: datetime | None) -> datetime | None:
    """Normalize to UTC-aware (SQLite returns naive datetimes)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def issue_key_prefix(issue_key: str | None) -> str:
    """The team proxy: the ``ENG`` in ``ENG-123``."""
    if not issue_key:
        return ""
    return issue_key.split("-")[0].upper()


def _latest_artifact_contents(
    session, run_id: str, artifact_types: tuple[ArtifactType, ...]
) -> dict[ArtifactType, dict]:
    """Latest (highest version, newest) parsed content per artifact type."""
    rows = (
        session.query(FoundryArtifact)
        .filter(
            FoundryArtifact.run_id == run_id,
            FoundryArtifact.artifact_type.in_(artifact_types),
        )
        .order_by(FoundryArtifact.version, FoundryArtifact.created_at)
        .all()
    )
    contents: dict[ArtifactType, dict] = {}
    for row in rows:  # ascending order: the last row per type wins
        try:
            content = json.loads(row.content_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(content, dict):
            contents[row.artifact_type] = content
    return contents


def _classify_block(session, run_id: str) -> str:
    """Taxonomy for a blocked run, from its last RUN_BLOCKED audit event.

    The orchestrator writes a structured ``category`` into the event metadata;
    the heuristics below only classify events recorded before that existed
    (so backfill still works on old audit trails). ``block_justified`` is
    deliberately not set here - justification is derived on read from
    supersession, never guessed.
    """
    event = (
        session.query(FoundryAuditEvent)
        .filter_by(run_id=run_id, event_type=AuditEventType.RUN_BLOCKED)
        .order_by(FoundryAuditEvent.sequence.desc())
        .first()
    )
    if event is None:
        return "unroutable"
    metadata: dict = {}
    if event.metadata_json:
        try:
            metadata = json.loads(event.metadata_json)
        except json.JSONDecodeError:
            metadata = {}
    if metadata.get("category") in _BLOCK_CATEGORIES:
        return metadata["category"]
    if event.actor_type == "human":
        return "human_stopped"
    if "forbidden_files" in metadata:
        return "forbidden_paths"
    if "closed without merge" in str(metadata.get("reason", "")):
        return "pr_closed_unmerged"
    if "policy_reasons" in metadata or event.output_hash is not None:
        # Dispatch-time policy denials attach the decision as output_content.
        return "policy_denied"
    return "unroutable"


def derive_outcome(session, run: FoundryRun) -> FoundryRunOutcome:
    """Derive the delivery-memory row for a terminal run. Pure read."""
    outcome = _OUTCOME_FOR_STATUS.get(run.status)
    if outcome is None:
        raise ValueError(f"run {run.id} is '{run.status.value}', not terminal")

    jobs = (
        session.query(FoundryAgentJob)
        .filter_by(run_id=run.id)
        # SQLite sorts NULLs first, Postgres last, so a plain ``started_at``
        # order makes "the latest job's repo" (the reversed() pick below)
        # backend-dependent. Sort unstarted jobs (NULL started_at) first
        # explicitly so a started job always counts as more recent, and break
        # ties on ``id`` - deterministic on both backends.
        .order_by(
            FoundryAgentJob.started_at.is_(None).desc(),
            FoundryAgentJob.started_at,
            FoundryAgentJob.id,
        )
        .all()
    )
    costs = [j.cost_usd for j in jobs if j.cost_usd is not None]
    repo = next((j.repo for j in reversed(jobs) if j.repo), None)
    # The agent that shipped it: the latest dispatched job's provider. NULL when
    # no agent ever ran (parked / rejected at intake), same as ``repo``.
    provider = next((j.provider for j in reversed(jobs) if j.provider), None)

    event_counts = dict(
        session.query(FoundryAuditEvent.event_type, func.count(FoundryAuditEvent.id))
        .filter_by(run_id=run.id)
        .group_by(FoundryAuditEvent.event_type)
        .all()
    )
    terminal_event = (
        session.query(FoundryAuditEvent)
        .filter(
            FoundryAuditEvent.run_id == run.id,
            FoundryAuditEvent.event_type.in_(_TERMINAL_EVENT_TYPES),
        )
        .order_by(FoundryAuditEvent.sequence.desc())
        .first()
    )
    completed_at = (
        _utc(terminal_event.created_at)
        if terminal_event is not None
        else _utc(run.updated_at) or datetime.now(timezone.utc)
    )

    created_at_run = _utc(run.created_at)
    time_to_merge = None
    if outcome == "merged" and completed_at is not None and created_at_run is not None:
        time_to_merge = max(int((completed_at - created_at_run).total_seconds()), 0)

    artifacts = _latest_artifact_contents(
        session,
        run.id,
        (
            ArtifactType.CONTEXT_BUNDLE,
            ArtifactType.TICKET_ANALYSIS,
            ArtifactType.TICKET_SNAPSHOT,
            ArtifactType.PR_STATE,
        ),
    )
    context = artifacts.get(ArtifactType.CONTEXT_BUNDLE)
    routed_confidence = None
    if context:
        confidences = [
            c.get("confidence")
            for c in context.get("candidate_repositories", [])
            if isinstance(c, dict) and isinstance(c.get("confidence"), int)
        ]
        routed_confidence = max(confidences, default=None)

    analysis = artifacts.get(ArtifactType.TICKET_ANALYSIS)
    snapshot = artifacts.get(ArtifactType.TICKET_SNAPSHOT)
    pr_state = artifacts.get(ArtifactType.PR_STATE)
    files_changed = pr_state.get("files_changed") if pr_state else None

    return FoundryRunOutcome(
        run_id=run.id,
        linear_issue_id=run.linear_issue_id,
        issue_key_prefix=issue_key_prefix(run.linear_issue_key),
        outcome=outcome,
        repo=repo,
        provider=provider,
        routed_confidence=routed_confidence,
        work_type=analysis.get("work_type") if analysis else None,
        labels_json=json.dumps(snapshot.get("labels", []) if snapshot else []),
        risk_level=run.risk_level.value if run.risk_level else None,
        agent_mode=run.agent_mode.value if run.agent_mode else None,
        trigger_type=run.trigger_type,
        created_at_run=created_at_run,
        approved_at=_utc(run.approved_at),
        completed_at=completed_at,
        time_to_merge_seconds=time_to_merge,
        jobs_count=len(jobs),
        escalations_count=event_counts.get(AuditEventType.RISK_ESCALATED, 0),
        ci_failures_count=event_counts.get(AuditEventType.CI_FAILED, 0),
        files_changed_count=(
            len(files_changed) if isinstance(files_changed, list) else None
        ),
        cost_usd=sum(costs) if costs else None,
        blocked_reason_category=(
            _classify_block(session, run.id) if outcome == "blocked" else None
        ),
        block_justified=None,
        recorded_at=datetime.now(timezone.utc),
    )


def _outcome_summary(row: FoundryRunOutcome) -> dict:
    """JSON-safe view of the outcome row for the final_summary artifact."""
    return {
        "run_id": row.run_id,
        "outcome": row.outcome,
        "repo": row.repo,
        "provider": row.provider,
        "routed_confidence": row.routed_confidence,
        "work_type": row.work_type,
        "issue_key_prefix": row.issue_key_prefix,
        "risk_level": row.risk_level,
        "time_to_merge_seconds": row.time_to_merge_seconds,
        "jobs_count": row.jobs_count,
        "retries_consumed": max(row.jobs_count - 1, 0),
        "escalations_count": row.escalations_count,
        "ci_failures_count": row.ci_failures_count,
        "files_changed_count": row.files_changed_count,
        "cost_usd": row.cost_usd,
        "blocked_reason_category": row.blocked_reason_category,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


def record_outcome(session, run: FoundryRun) -> FoundryRunOutcome:
    """Upsert the outcome row for a terminal run and close its audit timeline.

    Idempotent: ``session.merge`` upserts by run_id, and the ``final_summary``
    artifact is only written once, so the write-time hook and a later backfill
    or ``--recompute`` never duplicate anything.
    """
    if run.status not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"run {run.id} is '{run.status.value}', not terminal")
    row = session.merge(derive_outcome(session, run))
    has_summary = (
        session.query(FoundryArtifact.id)
        .filter_by(run_id=run.id, artifact_type=ArtifactType.FINAL_SUMMARY)
        .first()
        is not None
    )
    if not has_summary:
        session.add(
            build_artifact(
                run_id=run.id,
                artifact_type=ArtifactType.FINAL_SUMMARY,
                content=_outcome_summary(row),
                created_by="foundry",
            )
        )
    return row
