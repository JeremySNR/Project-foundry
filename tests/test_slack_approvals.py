"""Slack approval surface: signed interactivity -> the same policy-gated decision.

The inbound Slack endpoint must behave exactly like the other approval surfaces
(Linear/GitHub/Jira comments, the REST endpoint): authenticate the request,
identify the actor from a source the actor cannot forge, look roles up in config,
and drive the one ``submit_decision`` path. These tests pin the auth boundary
(signature + replay age + fail-closed), the actor->roles mapping, and the
fixture payload shape.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.api.security import compute_signature, compute_slack_signature
from foundry.api.slack import SlackInteraction, parse_slack_interaction
from foundry.db import create_all, make_engine, make_session_factory
from foundry.orchestrator import FoundryOrchestrator

SECRET = "test-secret"
SLACK_SECRET = "slack-signing-secret"
# Approvers are keyed by Slack user id here (as GitHub Issues keys them by login).
APPROVER = "U07APPROVER"
APPROVERS = {APPROVER: []}

FIXTURES = Path(__file__).parent / "fixtures"

READY_DESC = (
    "Customers want to favourite items.\n\n"
    "Acceptance Criteria:\n"
    "- A favourites button exists\n"
    "- Favourites persist across sessions\n"
)


def _make_client(**overrides) -> TestClient:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    kwargs = dict(
        webhook_secret=SECRET,
        session_factory=sf,
        orchestrator=orch,
        approvers=APPROVERS,
        slack_signing_secret=SLACK_SECRET,
    )
    kwargs.update(overrides)
    return TestClient(create_app(**kwargs))


@pytest.fixture
def client() -> TestClient:
    return _make_client()


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


def _start_ready_run(client, issue_id="issue-r") -> str:
    body = json.dumps(_ready_payload(issue_id=issue_id)).encode("utf-8")
    sig = "sha256=" + compute_signature(SECRET, body)
    resp = client.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Delivery": f"d-{issue_id}", "Linear-Signature": sig},
    )
    run = resp.json()["run"]
    assert run["status"] == "waiting_approval"
    return run["id"]


def _block_action(command: str, issue_id: str, user: str = APPROVER) -> dict:
    return {
        "type": "block_actions",
        "user": {"id": user, "username": "lead"},
        "actions": [
            {
                "type": "button",
                "action_id": f"foundry_{command}",
                "value": issue_id,
            }
        ],
    }


def _post_slack(client, payload: dict, *, secret=SLACK_SECRET, ts=None, sign=True):
    if ts is None:
        ts = str(int(time.time()))
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode("utf-8")
    signature = compute_slack_signature(secret, ts, body) if sign else "v0=deadbeef"
    return client.post(
        "/webhooks/slack",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": signature,
        },
    )


# -- auth boundary -------------------------------------------------------------


def test_slack_disabled_without_signing_secret() -> None:
    c = _make_client(slack_signing_secret=None)
    resp = _post_slack(c, _block_action("approve", "issue-r"))
    # Fail-closed, same posture as the Jira/GitLab token endpoints.
    assert resp.status_code == 403


def test_slack_invalid_signature_rejected(client) -> None:
    run_id = _start_ready_run(client)
    resp = _post_slack(client, _block_action("approve", "issue-r"), sign=False)
    assert resp.status_code == 401
    assert client.get(f"/runs/{run_id}").json()["status"] == "waiting_approval"


def test_slack_wrong_secret_rejected(client) -> None:
    run_id = _start_ready_run(client)
    resp = _post_slack(client, _block_action("approve", "issue-r"), secret="nope")
    assert resp.status_code == 401
    assert client.get(f"/runs/{run_id}").json()["status"] == "waiting_approval"


def test_slack_missing_timestamp_rejected(client) -> None:
    body = urllib.parse.urlencode(
        {"payload": json.dumps(_block_action("approve", "issue-r"))}
    ).encode("utf-8")
    # No X-Slack-Request-Timestamp header => fail closed.
    resp = client.post(
        "/webhooks/slack",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Signature": "v0=whatever",
        },
    )
    assert resp.status_code == 401


def test_slack_stale_request_rejected(client) -> None:
    run_id = _start_ready_run(client)
    old_ts = str(int(time.time()) - 60 * 60)  # an hour old, correctly signed
    resp = _post_slack(client, _block_action("approve", "issue-r"), ts=old_ts)
    assert resp.status_code == 401
    assert client.get(f"/runs/{run_id}").json()["status"] == "waiting_approval"


# -- decision behaviour --------------------------------------------------------


def test_slack_authorised_approve_dispatches(client) -> None:
    _start_ready_run(client)
    resp = _post_slack(client, _block_action("approve", "issue-r"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "applied"
    assert body["dispatched"] is True
    assert body["run"]["status"] == "agent_running"
    # The actor is the Slack-signed user id, not anything from the message body.
    assert body["run"]["approved_by"] == APPROVER


def test_slack_unauthorised_user_is_ignored(client) -> None:
    run_id = _start_ready_run(client)
    resp = _post_slack(client, _block_action("approve", "issue-r", user="U_STRANGER"))
    # Acknowledged (200) so Slack does not retry, but refused.
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert client.get(f"/runs/{run_id}").json()["status"] == "waiting_approval"


def test_slack_reject_terminates_run(client) -> None:
    run_id = _start_ready_run(client)
    resp = _post_slack(client, _block_action("reject", "issue-r"))
    assert resp.status_code == 200
    assert resp.json()["run"]["status"] == "rejected"
    assert client.get(f"/runs/{run_id}").json()["status"] == "rejected"


def test_slack_non_foundry_action_is_ignored(client) -> None:
    _start_ready_run(client)
    payload = {
        "type": "block_actions",
        "user": {"id": APPROVER},
        "actions": [{"type": "button", "action_id": "unrelated_button", "value": "x"}],
    }
    resp = _post_slack(client, payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_slack_no_active_run_is_ignored(client) -> None:
    # Signed and from an authorised user, but no run for that issue.
    resp = _post_slack(client, _block_action("approve", "issue-absent"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


# -- fixture-pinned payload mapping --------------------------------------------


def test_slack_fixture_payload_drives_a_decision(client) -> None:
    """The recorded block_actions fixture maps to an approve on its issue id."""
    payload = json.loads((FIXTURES / "slack_block_action_approve.json").read_text())
    # The fixture's user id is the approver, and its button value is the issue.
    c = _make_client(approvers={payload["user"]["id"]: []})
    body = json.dumps(_ready_payload(issue_id=payload["actions"][0]["value"])).encode()
    sig = "sha256=" + compute_signature(SECRET, body)
    c.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Delivery": "d-fix", "Linear-Signature": sig},
    )
    resp = _post_slack(c, payload)
    assert resp.status_code == 200
    out = resp.json()
    assert out["status"] == "applied"
    assert out["command"] == "approve"
    assert out["run"]["approved_by"] == payload["user"]["id"]


# -- parse_slack_interaction unit contracts ------------------------------------


def test_parse_extracts_each_decision() -> None:
    for command in ("approve", "reject", "stop"):
        got = parse_slack_interaction(_block_action(command, "issue-9", user="U1"))
        assert got == SlackInteraction(command=command, issue_id="issue-9", user="U1")


def test_parse_ignores_non_block_actions() -> None:
    assert parse_slack_interaction({"type": "view_submission"}) is None
    assert parse_slack_interaction({}) is None


def test_parse_ignores_unknown_or_non_foundry_actions() -> None:
    assert parse_slack_interaction(_action("foundry_frobnicate", "i", "U1")) is None
    assert parse_slack_interaction(_action("approve", "i", "U1")) is None  # no prefix


def test_parse_requires_user_and_issue_id() -> None:
    assert parse_slack_interaction(_block_action("approve", "issue-9", user="")) is None
    assert parse_slack_interaction(_action("foundry_approve", "", "U1")) is None
    assert parse_slack_interaction(_action("foundry_approve", "  ", "U1")) is None


def _action(action_id: str, value: str, user: str) -> dict:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "actions": [{"type": "button", "action_id": action_id, "value": value}],
    }
