"""GET /metrics/integrity: the in-app twin of the ``foundry-evidence verify``
audit-integrity CI gate (#132).

Read-only and offline (#3): the audit chain is recomputed against in-memory
SQLite, no network. The endpoint *reports* a tampered chain; it blocks no run.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.audit import build_audit_event
from foundry.db import create_all, make_engine, make_session_factory
from foundry.db.models import AuditEventType, FoundryAuditEvent, FoundryRun
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.common import RunStatus

API_TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def client(session_factory) -> TestClient:
    orch = FoundryOrchestrator(session_factory, provider=InMemoryFakeProvider())
    return TestClient(
        create_app(
            webhook_secret="test-secret",
            session_factory=session_factory,
            orchestrator=orch,
            api_token=API_TOKEN,
        )
    )


def _seed_run(session_factory, run_id: str) -> None:
    """A run with a couple of chained audit events - a valid trail to verify."""
    with session_factory() as session:
        session.add(
            FoundryRun(
                id=run_id,
                linear_issue_id=f"issue-{run_id}",
                linear_issue_key=f"ENG-{run_id}",
                status=RunStatus.COMPLETE,
                trigger_type="label",
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


def _tamper(session_factory, run_id: str) -> None:
    """Mutate an event field after the chain hash was assigned: breaks the chain."""
    with session_factory() as session:
        evt = (
            session.query(FoundryAuditEvent)
            .filter_by(run_id=run_id)
            .order_by(FoundryAuditEvent.sequence)
            .first()
        )
        evt.metadata_json = json.dumps({"tampered": True})
        session.commit()


def test_integrity_requires_auth(client) -> None:
    assert client.get("/metrics/integrity").status_code == 401


def test_empty_db_is_vacuously_verified(client) -> None:
    resp = client.get("/metrics/integrity", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert body["run_count"] == 0
    assert body["failed"] == []
    assert body["runs"] == []


def test_untampered_runs_verify(client, session_factory) -> None:
    _seed_run(session_factory, "a")
    _seed_run(session_factory, "b")
    body = client.get("/metrics/integrity", headers=AUTH).json()
    assert body["verified"] is True
    assert body["run_count"] == 2
    assert body["failed"] == []
    assert all(r["verified"] for r in body["runs"])


def test_a_tampered_chain_is_reported_not_blocked(client, session_factory) -> None:
    _seed_run(session_factory, "clean")
    _seed_run(session_factory, "dirty")
    _tamper(session_factory, "dirty")

    body = client.get("/metrics/integrity", headers=AUTH).json()
    assert body["verified"] is False
    assert body["run_count"] == 2
    assert body["failed"] == ["dirty"]
    by_id = {r["run_id"]: r for r in body["runs"]}
    assert by_id["clean"]["verified"] is True
    assert by_id["dirty"]["verified"] is False
    # The chain check is what failed - the audit-trail tamper signal.
    assert by_id["dirty"]["integrity"]["audit_chain"]["ok"] is False

    # Read-only: the run row itself is untouched (a tampered chain blocks nothing).
    with session_factory() as session:
        assert session.get(FoundryRun, "dirty").status == RunStatus.COMPLETE


def test_verdict_matches_the_cli_gate(client, session_factory) -> None:
    """The endpoint must not drift from ``foundry-evidence verify``: same builder."""
    from datetime import datetime, timezone

    from foundry.compliance import build_integrity_archive

    _seed_run(session_factory, "x")
    _tamper(session_factory, "x")
    api = client.get("/metrics/integrity", headers=AUTH).json()
    with session_factory() as session:
        cli = build_integrity_archive(
            session, since=None, until=datetime.now(timezone.utc)
        )
    assert api["verified"] == cli["verified"] is False
    assert api["failed"] == cli["failed"] == ["x"]


def test_window_validation(client) -> None:
    assert client.get("/metrics/integrity?days=0", headers=AUTH).status_code == 422
    bad = client.get(
        "/metrics/integrity?from=2026-06-10&to=2026-06-01", headers=AUTH
    )
    assert bad.status_code == 422


def test_window_filters_by_created_at(client, session_factory) -> None:
    """A run outside the window is not verified (so an old tamper can be scoped out)."""
    from datetime import datetime, timezone

    _seed_run(session_factory, "recent")
    with session_factory() as session:
        run = session.get(FoundryRun, "recent")
        run.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        session.commit()

    # Default 90-day window excludes the 2020 run.
    body = client.get("/metrics/integrity", headers=AUTH).json()
    assert body["run_count"] == 0

    # An explicit window that spans 2020 includes it.
    wide = client.get(
        "/metrics/integrity?from=2019-01-01&to=2030-01-01", headers=AUTH
    ).json()
    assert wide["run_count"] == 1
