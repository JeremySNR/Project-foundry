"""API tests: signed intake -> orchestrator, idempotency, approvals, run status."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.api.security import compute_signature
from foundry.connectors import GitHubConnector
from foundry.db import create_all, make_engine, make_session_factory
from foundry.orchestrator import FoundryOrchestrator

SECRET = "test-secret"
APPROVERS = {"lead@example.com"}

READY_DESC = (
    "Customers want to favourite items.\n\n"
    "Acceptance Criteria:\n"
    "- A favourites button exists\n"
    "- Favourites persist across sessions\n"
)


@pytest.fixture
def client() -> TestClient:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    app = create_app(
        webhook_secret=SECRET,
        session_factory=sf,
        orchestrator=orch,
        authorised_approvers=APPROVERS,
    )
    return TestClient(app)


def _post_webhook(client, payload, *, delivery, sign=True):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Linear-Delivery": delivery, "Content-Type": "application/json"}
    headers["Linear-Signature"] = (
        "sha256=" + compute_signature(SECRET, body) if sign else "sha256=deadbeef"
    )
    return client.post("/webhooks/linear", content=body, headers=headers)


def _basic_payload(issue_id="issue-1", key="LIN-1") -> dict:
    """Triggers a run but is too thin to be buildable (-> needs clarification)."""
    return {
        "data": {
            "id": issue_id,
            "issueId": issue_id,
            "identifier": key,
            "title": "Do something",
            "labels": [{"name": "foundry:candidate"}],
            "actor": {"name": "po@example.com"},
        }
    }


def _ready_payload(issue_id="issue-r", key="LIN-123") -> dict:
    return {
        "data": {
            "id": issue_id,
            "issueId": issue_id,
            "identifier": key,
            "title": "Add customer favourites",
            "description": READY_DESC,
            "labels": [{"name": "foundry:candidate"}, {"name": "repo:customer-web"}],
            "actor": {"name": "po@example.com"},
        }
    }


def test_healthz(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_unauthorised_webhook_rejected_no_run(client) -> None:
    resp = _post_webhook(client, _basic_payload(), delivery="d1", sign=False)
    assert resp.status_code == 401
    assert client.get("/runs").json()["runs"] == []


def test_duplicate_delivery_creates_one_run(client) -> None:
    payload = _basic_payload()
    first = _post_webhook(client, payload, delivery="d-dup")
    second = _post_webhook(client, payload, delivery="d-dup")
    assert first.json()["status"] == "started"
    assert second.json()["status"] == "duplicate"
    assert len(client.get("/runs").json()["runs"]) == 1


def test_same_issue_different_delivery_does_not_duplicate(client) -> None:
    payload = _basic_payload()
    _post_webhook(client, payload, delivery="d-a")
    second = _post_webhook(client, payload, delivery="d-b")
    assert second.json()["status"] == "exists"
    assert len(client.get("/runs").json()["runs"]) == 1


def test_non_trigger_event_is_ignored(client) -> None:
    payload = {"data": {"id": "i9", "issueId": "i9", "labels": []}}
    resp = _post_webhook(client, payload, delivery="d-ignore")
    assert resp.json()["status"] == "ignored"
    assert client.get("/runs").json()["runs"] == []


def test_intake_runs_orchestrator_and_persists_status(client) -> None:
    resp = _post_webhook(client, _basic_payload(), delivery="d-int")
    run = resp.json()["run"]
    # A thin ticket is analysed and parked for clarification, not left "analysing".
    assert run["status"] == "needs_clarification"
    assert run["created_by"] == "po@example.com"


def test_comment_command_triggers_run(client) -> None:
    payload = {
        "data": {
            "id": "i-cmd",
            "issueId": "i-cmd",
            "identifier": "LIN-9",
            "title": "Investigate",
            "body": "/foundry start",
        }
    }
    resp = _post_webhook(client, payload, delivery="d-cmd")
    assert resp.json()["status"] == "started"
    assert resp.json()["run"]["trigger_type"] == "comment_command"


def test_run_status_404_for_unknown(client) -> None:
    assert client.get("/runs/nope").status_code == 404


def _start_ready_run(client) -> str:
    resp = _post_webhook(client, _ready_payload(), delivery="d-ready")
    run = resp.json()["run"]
    assert run["status"] == "waiting_approval"
    return run["id"]


def test_only_authorised_user_can_approve(client) -> None:
    run_id = _start_ready_run(client)
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "stranger@example.com", "text": "/foundry approve"},
    )
    assert resp.status_code == 403


def test_authorised_approve_dispatches_agent(client) -> None:
    run_id = _start_ready_run(client)
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dispatched"] is True
    assert body["run"]["status"] == "agent_running"
    assert body["run"]["approved_by"] == "lead@example.com"


def test_reject_terminates_run(client) -> None:
    run_id = _start_ready_run(client)
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "reject"},
    )
    assert resp.json()["run"]["status"] == "rejected"


def test_approve_on_unready_run_conflicts(client) -> None:
    # A needs-clarification run cannot be approved.
    resp = _post_webhook(client, _basic_payload(issue_id="i-x", key="LIN-X"), delivery="d-x")
    run_id = resp.json()["run"]["id"]
    out = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
    )
    assert out.status_code == 409


def test_unrecognised_command_is_rejected(client) -> None:
    run_id = _start_ready_run(client)
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry frobnicate"},
    )
    assert resp.status_code == 400


# -- GitHub webhook closes the loop -------------------------------------------


def _post_github(client, payload, *, event, sign=True):
    body = json.dumps(payload).encode("utf-8")
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    headers["X-Hub-Signature-256"] = (
        "sha256=" + compute_signature(SECRET, body) if sign else "sha256=bad"
    )
    return client.post("/webhooks/github", content=body, headers=headers)


def _approve_and_dispatch(client) -> str:
    run_id = _start_ready_run(client)
    client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
    )
    return run_id


def test_github_unauthorised_rejected(client) -> None:
    resp = _post_github(client, {}, event="pull_request", sign=False)
    assert resp.status_code == 401


def test_github_pr_for_unknown_branch_ignored(client) -> None:
    payload = {
        "pull_request": {
            "number": 1,
            "html_url": "u",
            "head": {"ref": "someone-elses-branch"},
            "state": "open",
        },
        "repository": {"full_name": "o/customer-web"},
    }
    resp = _post_github(client, payload, event="pull_request")
    assert resp.json()["status"] == "ignored"


def test_app_from_settings_boots_with_defaults() -> None:
    from foundry.api import app_from_settings
    from foundry.config import Settings

    app = app_from_settings(Settings.from_env({"FOUNDRY_LINEAR_WEBHOOK_SECRET": "s"}))
    c = TestClient(app)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/runs").json() == {"runs": []}


def test_app_from_settings_wires_connectors_when_tokens_present() -> None:
    from foundry.api import app_from_settings
    from foundry.config import Settings

    # Tokens present => Linear tracker + GitHub connector are constructed (lazily,
    # no network at construction time). The app should still boot cleanly.
    app = app_from_settings(
        Settings.from_env(
            {
                "FOUNDRY_LINEAR_WEBHOOK_SECRET": "s",
                "FOUNDRY_LINEAR_API_TOKEN": "lt",
                "FOUNDRY_GITHUB_API_TOKEN": "gt",
            }
        )
    )
    assert TestClient(app).get("/healthz").status_code == 200


def test_settings_custom_trigger_label_is_honored() -> None:
    from foundry.api import app_from_settings
    from foundry.config import Settings

    settings = Settings.from_env({"FOUNDRY_LINEAR_WEBHOOK_SECRET": SECRET})
    settings = settings._with({"trigger_label": "ai:go"})
    c = TestClient(app_from_settings(settings))

    # The default label no longer triggers...
    default_labelled = _basic_payload()
    body = json.dumps(default_labelled).encode("utf-8")
    sig = "sha256=" + compute_signature(SECRET, body)
    resp = c.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Delivery": "d1", "Linear-Signature": sig},
    )
    assert resp.json()["status"] == "ignored"

    # ...but the configured one does.
    custom = {
        "data": {
            "id": "i2",
            "issueId": "i2",
            "identifier": "LIN-2",
            "title": "x",
            "labels": [{"name": "ai:go"}],
        }
    }
    body2 = json.dumps(custom).encode("utf-8")
    sig2 = "sha256=" + compute_signature(SECRET, body2)
    resp2 = c.post(
        "/webhooks/linear",
        content=body2,
        headers={"Linear-Delivery": "d2", "Linear-Signature": sig2},
    )
    assert resp2.json()["status"] == "started"


def test_github_pr_closes_loop_to_pr_open(client) -> None:
    run_id = _approve_and_dispatch(client)
    run = client.get(f"/runs/{run_id}").json()
    branch = "foundry/lin-123-add-customer-favourites"
    assert run["status"] == "agent_running"

    payload = {
        "pull_request": {
            "number": 42,
            "html_url": "https://github.com/o/customer-web/pull/42",
            "head": {"ref": branch},
            "state": "open",
            "draft": False,
            "merged": False,
        },
        "repository": {"full_name": "o/customer-web"},
    }
    resp = _post_github(client, payload, event="pull_request")
    assert resp.json()["status"] == "recorded"
    assert resp.json()["run_status"] == "pr_open"
    assert client.get(f"/runs/{run_id}").json()["status"] == "pr_open"
