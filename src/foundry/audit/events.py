"""Audit helpers - content hashing and persistence of the run's trail.

Every decision, prompt input, output, approval and tool call should be storable
and verifiable. These helpers keep hashing consistent across artifact, audit and
policy rows.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from pydantic import BaseModel

from foundry.db.models import (
    ArtifactType,
    AuditEventType,
    FoundryArtifact,
    FoundryAuditEvent,
    FoundryPolicyDecision,
)
from foundry.policy.engine import PolicyDecision, PolicyInput


def _canonical(content: Any) -> str:
    """Deterministic JSON for stable hashing.

    Pydantic models are dumped in JSON mode; plain objects are serialised with
    sorted keys so the same logical content always hashes identically.
    """
    if isinstance(content, BaseModel):
        payload = content.model_dump(mode="json")
    else:
        payload = content
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def content_hash(content: Any) -> str:
    """SHA-256 of the canonical JSON for ``content``."""
    return hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def build_artifact(
    *,
    run_id: str,
    artifact_type: ArtifactType,
    content: Any,
    version: int = 1,
    created_by: str | None = None,
) -> FoundryArtifact:
    """Create a content-hashed artifact row (not yet persisted)."""
    canonical = _canonical(content)
    return FoundryArtifact(
        id=new_id("art"),
        run_id=run_id,
        artifact_type=artifact_type,
        version=version,
        content_json=canonical,
        content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        created_by=created_by,
    )


def build_audit_event(
    *,
    run_id: str,
    event_type: AuditEventType,
    actor_type: str,
    actor_id: str | None = None,
    input_content: Any | None = None,
    output_content: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> FoundryAuditEvent:
    """Create an audit event row with hashed input/output (not yet persisted)."""
    return FoundryAuditEvent(
        id=new_id("evt"),
        run_id=run_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        input_hash=content_hash(input_content) if input_content is not None else None,
        output_hash=content_hash(output_content) if output_content is not None else None,
        metadata_json=json.dumps(metadata, sort_keys=True) if metadata else None,
    )


def build_policy_decision_row(
    *,
    run_id: str,
    payload: PolicyInput,
    decision: PolicyDecision,
) -> FoundryPolicyDecision:
    """Persist-ready row capturing a single policy gate decision."""
    return FoundryPolicyDecision(
        id=decision.decision_id,
        run_id=run_id,
        policy_name=decision.policy_name,
        input_json=_canonical(payload),
        decision_json=_canonical(decision),
        allowed=decision.allowed,
        reason="; ".join(decision.reasons) if decision.reasons else None,
    )
