"""Compliance evidence packs: assembly, integrity verification, control mapping.

Endpoint-level coverage (auth, formats, a real driven run) lives in
``tests/test_api.py``; this file unit-tests the packer, the integrity check, and
the config seam directly against the DB.
"""

from __future__ import annotations

import json

import pytest

from datetime import datetime, timedelta, timezone

from foundry.audit import build_artifact, build_audit_event
from foundry.compliance import (
    DEFAULT_CONTROL_MAPPINGS,
    ControlMapping,
    build_evidence_archive,
    build_evidence_pack,
    render_archive_html,
    render_evidence_html,
    verify_integrity,
)
from foundry.config import Settings
from foundry.db import create_all, make_engine, make_session_factory
from foundry.db.models import (
    ArtifactType,
    AuditEventType,
    FoundryArtifact,
    FoundryAuditEvent,
    FoundryRun,
)
from foundry.schemas.common import RunStatus


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


def _seed_run(session_factory, *, with_pr: bool = True) -> str:
    """A run with the artifacts/events a real run accumulates, persisted."""
    run_id = "run-ev-1"
    with session_factory() as session:
        session.add(
            FoundryRun(
                id=run_id,
                linear_issue_id="issue-1",
                linear_issue_key="ENG-1",
                status=RunStatus.COMPLETE,
                trigger_type="label",
                approved_by="lead@example.com",
            )
        )
        session.add(
            build_artifact(
                run_id=run_id,
                artifact_type=ArtifactType.TICKET_SNAPSHOT,
                content={"title": "Add favourites", "body": "AC: a button"},
            )
        )
        session.add(
            build_artifact(
                run_id=run_id,
                artifact_type=ArtifactType.DELIVERY_PLAN,
                content={"steps": ["Satisfy acceptance criterion: a button"]},
            )
        )
        session.add(
            build_artifact(
                run_id=run_id,
                artifact_type=ArtifactType.RISK_ASSESSMENT,
                content={"overall": "medium", "required_approvals": ["engineering"]},
            )
        )
        session.add(
            build_artifact(
                run_id=run_id,
                artifact_type=ArtifactType.APPROVAL_RECORD,
                content={"user": "lead@example.com", "granted_roles": ["engineering"]},
            )
        )
        if with_pr:
            session.add(
                build_artifact(
                    run_id=run_id,
                    artifact_type=ArtifactType.PR_STATE,
                    content={"url": "https://example/pr/1", "state": "merged"},
                )
            )
        session.add(
            build_audit_event(
                run_id=run_id,
                event_type=AuditEventType.RUN_STARTED,
                actor_type="system",
            )
        )
        session.add(
            build_audit_event(
                run_id=run_id,
                event_type=AuditEventType.APPROVAL_GRANTED,
                actor_type="human",
                actor_id="lead@example.com",
                output_content={"user": "lead@example.com"},
            )
        )
        session.commit()
    return run_id


def _build(session_factory, run_id, **kwargs) -> dict:
    with session_factory() as session:
        run = session.get(FoundryRun, run_id)
        return build_evidence_pack(session, run, **kwargs)


def test_pack_assembles_every_section(session_factory) -> None:
    run_id = _seed_run(session_factory)
    pack = _build(
        session_factory, run_id, control_mappings=DEFAULT_CONTROL_MAPPINGS
    )

    assert pack["run"]["id"] == run_id
    assert pack["run"]["status"] == "complete"
    assert pack["ticket"]["content"]["title"] == "Add favourites"
    assert pack["plan"]["content"]["steps"]
    assert pack["risk_assessment"]["content"]["overall"] == "medium"
    assert pack["pr"]["content"]["state"] == "merged"

    # Approvals surface the identity and the granted roles.
    assert pack["approvals"] == [
        {
            "approver": "lead@example.com",
            "granted_roles": ["engineering"],
            "recorded_at": pack["approvals"][0]["recorded_at"],
            "artifact_id": pack["approvals"][0]["artifact_id"],
            "content_hash": pack["approvals"][0]["content_hash"],
        }
    ]

    event_types = [e["event_type"] for e in pack["audit_trail"]]
    assert "run.started" in event_types
    assert "approval.granted" in event_types


def test_integrity_passes_for_untampered_run(session_factory) -> None:
    run_id = _seed_run(session_factory)
    pack = _build(session_factory, run_id)
    integrity = pack["integrity"]
    assert integrity["verified"] is True
    assert integrity["artifacts"]["ok"] is True
    assert integrity["artifacts"]["failed"] == []
    assert integrity["audit_sequence"]["ok"] is True
    assert integrity["audit_sequence"]["contiguous"] is True


def test_integrity_flags_a_tampered_artifact(session_factory) -> None:
    """Mutate content_json without touching content_hash: must fail integrity."""
    run_id = _seed_run(session_factory)
    with session_factory() as session:
        art = (
            session.query(FoundryArtifact)
            .filter_by(run_id=run_id, artifact_type=ArtifactType.TICKET_SNAPSHOT)
            .one()
        )
        art.content_json = json.dumps({"title": "TAMPERED"})
        session.commit()

    pack = _build(session_factory, run_id)
    integrity = pack["integrity"]
    assert integrity["verified"] is False
    assert integrity["artifacts"]["ok"] is False
    assert len(integrity["artifacts"]["failed"]) == 1


def test_integrity_flags_a_sequence_gap(session_factory) -> None:
    run_id = _seed_run(session_factory)
    with session_factory() as session:
        # Delete the first event, leaving a non-contiguous sequence (1, ...).
        first = (
            session.query(FoundryAuditEvent)
            .filter_by(run_id=run_id)
            .order_by(FoundryAuditEvent.sequence)
            .first()
        )
        session.delete(first)
        session.commit()

    pack = _build(session_factory, run_id)
    assert pack["integrity"]["audit_sequence"]["contiguous"] is False
    assert pack["integrity"]["verified"] is False


def test_control_satisfied_only_when_all_evidence_present(session_factory) -> None:
    # No PR_STATE => SOC 2 CC8.1 (which requires "pr") is not satisfied, but the
    # EU AI Act human-oversight control (risk + approvals + policy) still is...
    run_id = _seed_run(session_factory, with_pr=False)
    pack = _build(
        session_factory, run_id, control_mappings=DEFAULT_CONTROL_MAPPINGS
    )
    by_control = {c["control_id"]: c for c in pack["control_mappings"]}

    soc2 = by_control["CC8.1"]
    assert soc2["satisfied"] is False
    assert "pr" in soc2["missing_evidence"]
    # policy_decisions is also absent here (we seeded none).
    assert "policy_decisions" in soc2["missing_evidence"]


def test_custom_control_mappings_are_honoured(session_factory) -> None:
    run_id = _seed_run(session_factory)
    custom = (
        ControlMapping(
            framework="Internal",
            control_id="X-1",
            title="Ticket recorded",
            evidence=("ticket",),
        ),
    )
    pack = _build(session_factory, run_id, control_mappings=custom)
    assert len(pack["control_mappings"]) == 1
    assert pack["control_mappings"][0]["control_id"] == "X-1"
    assert pack["control_mappings"][0]["satisfied"] is True


def test_no_control_mappings_yields_empty_list(session_factory) -> None:
    run_id = _seed_run(session_factory)
    pack = _build(session_factory, run_id)
    assert pack["control_mappings"] == []


def test_render_html_is_standalone_and_escapes_content(session_factory) -> None:
    run_id = _seed_run(session_factory)
    pack = _build(
        session_factory, run_id, control_mappings=DEFAULT_CONTROL_MAPPINGS
    )
    html = render_evidence_html(pack)
    assert html.startswith("<!doctype html>")
    assert "INTEGRITY VERIFIED" in html
    assert "ENG-1" in html
    assert "CC8.1" in html


def test_render_html_marks_failed_integrity(session_factory) -> None:
    run_id = _seed_run(session_factory)
    with session_factory() as session:
        art = session.query(FoundryArtifact).filter_by(run_id=run_id).first()
        art.content_json = "{}"
        session.commit()
    pack = _build(session_factory, run_id)
    html = render_evidence_html(pack)
    assert "INTEGRITY CHECK FAILED" in html


def test_verify_integrity_empty_run_is_trivially_verified() -> None:
    assert verify_integrity([], []) == {
        "verified": True,
        "method": verify_integrity([], [])["method"],
        "artifacts": {"checked": 0, "ok": True, "failed": [], "details": []},
        "audit_sequence": {
            "ok": True,
            "count": 0,
            "ordered": True,
            "unique": True,
            "contiguous": True,
        },
    }


# -- org-wide date-range archive -----------------------------------------------


def _seed_run_at(
    session_factory,
    run_id: str,
    *,
    created_at: datetime,
    status: RunStatus = RunStatus.COMPLETE,
    with_pr: bool = True,
    tamper: bool = False,
) -> None:
    """Persist a minimal run (ticket + plan + risk + approval [+ pr]) at a
    given ``created_at`` so date-range filtering and rollups can be exercised.
    """
    with session_factory() as session:
        session.add(
            FoundryRun(
                id=run_id,
                linear_issue_id=f"issue-{run_id}",
                linear_issue_key=f"ENG-{run_id}",
                status=status,
                trigger_type="label",
                created_at=created_at,
            )
        )
        ticket = build_artifact(
            run_id=run_id,
            artifact_type=ArtifactType.TICKET_SNAPSHOT,
            content={"title": f"Ticket {run_id}"},
        )
        if tamper:
            ticket.content_json = json.dumps({"title": "TAMPERED"})
        session.add(ticket)
        session.add(
            build_artifact(
                run_id=run_id,
                artifact_type=ArtifactType.DELIVERY_PLAN,
                content={"steps": ["do the thing"]},
            )
        )
        session.add(
            build_artifact(
                run_id=run_id,
                artifact_type=ArtifactType.RISK_ASSESSMENT,
                content={"overall": "low", "required_approvals": []},
            )
        )
        session.add(
            build_artifact(
                run_id=run_id,
                artifact_type=ArtifactType.APPROVAL_RECORD,
                content={"user": "lead@example.com", "granted_roles": ["engineering"]},
            )
        )
        if with_pr:
            session.add(
                build_artifact(
                    run_id=run_id,
                    artifact_type=ArtifactType.PR_STATE,
                    content={"url": f"https://example/pr/{run_id}", "state": "merged"},
                )
            )
        session.add(
            build_audit_event(
                run_id=run_id,
                event_type=AuditEventType.RUN_STARTED,
                actor_type="system",
            )
        )
        session.commit()


def _archive(session_factory, **kwargs) -> dict:
    with session_factory() as session:
        return build_evidence_archive(session, **kwargs)


def test_archive_filters_to_the_date_range(session_factory) -> None:
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    _seed_run_at(session_factory, "old", created_at=now - timedelta(days=40))
    _seed_run_at(session_factory, "in1", created_at=now - timedelta(days=5))
    _seed_run_at(session_factory, "in2", created_at=now - timedelta(days=1))

    archive = _archive(
        session_factory,
        since=now - timedelta(days=10),
        until=now,
        control_mappings=DEFAULT_CONTROL_MAPPINGS,
    )
    assert archive["run_count"] == 2
    ids = [p["run"]["id"] for p in archive["runs"]]
    assert ids == ["in1", "in2"]  # ordered by created_at
    assert archive["range"]["from"] == (now - timedelta(days=10)).isoformat()
    assert archive["range"]["to"] == now.isoformat()


def test_archive_until_is_exclusive_and_since_inclusive(session_factory) -> None:
    boundary = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    _seed_run_at(session_factory, "at-since", created_at=boundary)
    _seed_run_at(session_factory, "at-until", created_at=boundary + timedelta(days=1))

    archive = _archive(
        session_factory, since=boundary, until=boundary + timedelta(days=1)
    )
    assert [p["run"]["id"] for p in archive["runs"]] == ["at-since"]


def test_archive_open_bounds_include_everything(session_factory) -> None:
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    _seed_run_at(session_factory, "a", created_at=now - timedelta(days=400))
    _seed_run_at(session_factory, "b", created_at=now)
    archive = _archive(session_factory)
    assert archive["run_count"] == 2
    assert archive["range"] == {"from": None, "to": None}


def test_archive_rollup_summary(session_factory) -> None:
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    # Two complete runs with a PR (satisfy SOC 2 CC8.1 needs pr + policy...),
    # one blocked run without a PR.
    _seed_run_at(session_factory, "c1", created_at=now, status=RunStatus.COMPLETE)
    _seed_run_at(session_factory, "c2", created_at=now, status=RunStatus.COMPLETE)
    _seed_run_at(
        session_factory,
        "b1",
        created_at=now,
        status=RunStatus.BLOCKED,
        with_pr=False,
    )

    archive = _archive(session_factory, control_mappings=DEFAULT_CONTROL_MAPPINGS)
    summary = archive["summary"]
    assert summary["status_breakdown"] == {"complete": 2, "blocked": 1}
    assert summary["verified"] is True
    assert summary["runs_verified"] == 3
    assert summary["runs_failed_integrity"] == []

    by_control = {c["control_id"]: c for c in summary["control_coverage"]}
    # EU AI Act Art. 14 (risk + approvals + policy_decisions) - no run seeded a
    # policy decision, so zero runs satisfy it.
    assert by_control["Article 14"]["total_runs"] == 3
    assert by_control["Article 14"]["satisfied_runs"] == 0
    assert by_control["Article 14"]["fully_satisfied"] is False


def test_archive_aggregate_integrity_flags_a_tampered_run(session_factory) -> None:
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    _seed_run_at(session_factory, "good", created_at=now)
    _seed_run_at(session_factory, "bad", created_at=now, tamper=True)

    archive = _archive(session_factory)
    summary = archive["summary"]
    assert summary["verified"] is False
    assert summary["runs_failed_integrity"] == ["bad"]
    assert summary["runs_verified"] == 1


def test_archive_empty_range_is_well_formed(session_factory) -> None:
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    archive = _archive(
        session_factory,
        since=now - timedelta(days=1),
        until=now,
        control_mappings=DEFAULT_CONTROL_MAPPINGS,
    )
    assert archive["run_count"] == 0
    assert archive["runs"] == []
    assert archive["summary"]["verified"] is True  # vacuously
    assert archive["summary"]["control_coverage"] == []


def test_render_archive_html_is_standalone(session_factory) -> None:
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    _seed_run_at(session_factory, "r1", created_at=now)
    archive = _archive(
        session_factory,
        since=now - timedelta(days=1),
        until=now + timedelta(days=1),
        control_mappings=DEFAULT_CONTROL_MAPPINGS,
    )
    html = render_archive_html(archive)
    assert html.startswith("<!doctype html>")
    assert "Compliance evidence archive" in html
    assert "INTEGRITY VERIFIED" in html
    assert "ENG-r1" in html
    assert "CC8.1" in html


def test_render_archive_html_marks_failed_integrity(session_factory) -> None:
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    _seed_run_at(session_factory, "bad", created_at=now, tamper=True)
    archive = _archive(session_factory)
    html = render_archive_html(archive)
    assert "INTEGRITY CHECK FAILED" in html


# -- config seam ---------------------------------------------------------------


def test_default_mappings_cover_the_three_frameworks() -> None:
    frameworks = {m.framework for m in Settings().compliance_control_mappings}
    assert "SOC 2" in frameworks
    assert any("27001" in f for f in frameworks)
    assert "EU AI Act" in frameworks


def test_yaml_overrides_control_mappings(tmp_path) -> None:
    cfg = tmp_path / "foundry.yaml"
    cfg.write_text(
        "compliance:\n"
        "  control_mappings:\n"
        "    - framework: Internal\n"
        "      control_id: CHG-1\n"
        "      title: Change recorded\n"
        "      evidence: [ticket, approvals]\n"
    )
    settings = Settings.load(cfg, env={})
    assert len(settings.compliance_control_mappings) == 1
    mapping = settings.compliance_control_mappings[0]
    assert mapping.control_id == "CHG-1"
    assert mapping.evidence == ("ticket", "approvals")


def test_unknown_evidence_section_rejected(tmp_path) -> None:
    cfg = tmp_path / "foundry.yaml"
    cfg.write_text(
        "compliance:\n"
        "  control_mappings:\n"
        "    - framework: Internal\n"
        "      control_id: BAD-1\n"
        "      title: Bad\n"
        "      evidence: [ticket, not_a_real_section]\n"
    )
    with pytest.raises(ValueError, match="unknown evidence section"):
        Settings.load(cfg, env={})
