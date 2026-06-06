"""API skeleton tests: signed intake, idempotency, approvals, run status."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from foundry.api import create_app
from foundry.api.security import compute_signature

SECRET = "test-secret"
APPROVERS = {"lead@example.com"}


@pytest.fixture
def client() -> TestClient:
    app = create_app(webhook_secret=SECRET, authorised_approvers=APPROVERS)
    return TestClient(app)


def _post_webhook(client: TestClient, payload: dict, *, delivery: str, sign: bool = True):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Linear-Delivery": delivery, "Content-Type": "application/json"}
    if sign:
        headers["Linear-Signature"] = "sha256=" + compute_signature(SECRET, body)
    else:
        headers["Linear-Signature"] = "sha256=deadbeef"
    return client.post("/webhooks/linear", content=body, headers=headers)


def _trigger_payload(issue_id: str = "issue-1", key: str = "LIN-123") -> dict:
    return {
        "data": {
            "id": issue_id,
            "issueId": issue_id,
            "identifier": key,
            "labels": [{"name": "foundry:candidate"}],
            "actor": {"name": "po@example.com"},
        }
    }


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_unauthorised_webhook_rejected_no_run(client: TestClient) -> None:
    resp = _post_webhook(client, _trigger_payload(), delivery="d1", sign=False)
    assert resp.status_code == 401
    # No workflow started.
    assert client.get("/runs").json()["runs"] == []


def test_duplicate_webhook_creates_one_run(client: TestClient) -> None:
    payload = _trigger_payload()
    first = _post_webhook(client, payload, delivery="d-dup")
    second = _post_webhook(client, payload, delivery="d-dup")

    assert first.json()["status"] == "started"
    assert second.json()["status"] == "duplicate"
    assert len(client.get("/runs").json()["runs"]) == 1


def test_same_issue_different_delivery_does_not_duplicate(client: TestClient) -> None:
    payload = _trigger_payload()
    _post_webhook(client, payload, delivery="d-a")
    second = _post_webhook(client, payload, delivery="d-b")
    assert second.json()["status"] == "exists"
    assert len(client.get("/runs").json()["runs"]) == 1


def test_non_trigger_event_is_ignored(client: TestClient) -> None:
    payload = {"data": {"id": "i9", "issueId": "i9", "labels": []}}
    resp = _post_webhook(client, payload, delivery="d-ignore")
    assert resp.json()["status"] == "ignored"
    assert client.get("/runs").json()["runs"] == []


def test_comment_command_triggers_run(client: TestClient) -> None:
    payload = {
        "data": {
            "id": "i-cmd",
            "issueId": "i-cmd",
            "identifier": "LIN-9",
            "body": "/foundry start",
        }
    }
    resp = _post_webhook(client, payload, delivery="d-cmd")
    assert resp.json()["status"] == "started"
    assert resp.json()["run"]["trigger_type"] == "comment_command"


def test_run_status_404_for_unknown(client: TestClient) -> None:
    assert client.get("/runs/nope").status_code == 404


def _start_run(client: TestClient) -> str:
    resp = _post_webhook(client, _trigger_payload(), delivery="d-run")
    return resp.json()["run"]["id"]


def test_only_authorised_user_can_approve(client: TestClient) -> None:
    run_id = _start_run(client)
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "stranger@example.com", "text": "/foundry approve"},
    )
    assert resp.status_code == 403


def test_authorised_approve_transitions_run(client: TestClient) -> None:
    run_id = _start_run(client)
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
    )
    assert resp.status_code == 200
    run = resp.json()["run"]
    assert run["status"] == "approved"
    assert run["approved_by"] == "lead@example.com"


def test_reject_terminates_run(client: TestClient) -> None:
    run_id = _start_run(client)
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "reject"},
    )
    assert resp.json()["run"]["status"] == "rejected"


def test_unrecognised_command_is_rejected(client: TestClient) -> None:
    run_id = _start_run(client)
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry frobnicate"},
    )
    assert resp.status_code == 400
