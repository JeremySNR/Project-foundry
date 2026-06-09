"""Audit hashing and row-building tests."""

from __future__ import annotations

from foundry.audit import build_artifact, build_policy_decision_row, content_hash
from foundry.db.models import ArtifactType
from foundry.policy import LocalPolicyEngine, PolicyInput
from foundry.schemas import TicketAnalysis


def test_content_hash_is_stable_and_order_independent() -> None:
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    assert content_hash(a) == content_hash(b)


def test_content_hash_changes_with_content() -> None:
    assert content_hash({"x": 1}) != content_hash({"x": 2})


def test_pydantic_model_hashes_consistently(ready_analysis: TicketAnalysis) -> None:
    first = content_hash(ready_analysis)
    second = content_hash(ready_analysis.model_copy(deep=True))
    assert first == second


def test_build_artifact_sets_hash(ready_analysis: TicketAnalysis) -> None:
    artifact = build_artifact(
        run_id="run-1",
        artifact_type=ArtifactType.TICKET_ANALYSIS,
        content=ready_analysis,
    )
    assert artifact.run_id == "run-1"
    assert artifact.artifact_type is ArtifactType.TICKET_ANALYSIS
    assert artifact.content_hash == content_hash(ready_analysis)


def test_build_policy_decision_row_records_outcome() -> None:
    payload = PolicyInput.model_validate(
        {
            "action": "start_agent",
            "ticket": {"readiness": "ready"},
            "risk": {"overall_risk": "low"},
            "repo": {"confidence": 90},
        }
    )
    decision = LocalPolicyEngine().evaluate(payload)
    row = build_policy_decision_row(run_id="run-1", payload=payload, decision=decision)
    assert row.id == decision.decision_id
    assert row.allowed is decision.allowed
    assert row.policy_name == decision.policy_name


def test_audit_events_get_monotonic_per_run_sequences() -> None:
    """The model promises a guaranteed per-run order; the session assigns it."""
    from foundry.audit import build_audit_event
    from foundry.db import create_all, make_engine, make_session_factory
    from foundry.db.models import AuditEventType, FoundryAuditEvent, FoundryRun

    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)

    def _event(run_id: str) -> object:
        return build_audit_event(
            run_id=run_id, event_type=AuditEventType.RUN_STARTED, actor_type="foundry"
        )

    with sf() as session:
        for rid in ("run-a", "run-b"):
            session.add(
                FoundryRun(id=rid, linear_issue_id=rid, linear_issue_key=rid,
                           trigger_type="test")
            )
        # Two events in one flush, then one more in a later commit.
        session.add(_event("run-a"))
        session.add(_event("run-a"))
        session.add(_event("run-b"))
        session.commit()
        session.add(_event("run-a"))
        session.commit()

        seq_a = [
            e.sequence
            for e in session.query(FoundryAuditEvent)
            .filter_by(run_id="run-a")
            .order_by(FoundryAuditEvent.sequence)
        ]
        seq_b = [
            e.sequence
            for e in session.query(FoundryAuditEvent).filter_by(run_id="run-b")
        ]
    assert seq_a == [0, 1, 2]  # monotonic per run, across separate commits
    assert seq_b == [0]  # independent counter per run
