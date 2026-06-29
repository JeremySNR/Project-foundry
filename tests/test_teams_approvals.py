"""Teams approval surface: signed Outgoing-Webhook -> the same policy-gated decision.

The Teams twin of ``test_slack_approvals.py``. The inbound Teams endpoint must
behave exactly like every other approval surface (Linear/GitHub/Jira comments,
the REST endpoint, Slack): authenticate the request, identify the actor from a
source the actor cannot forge, look roles up in config, and drive the one
``submit_decision`` path. These tests pin the auth boundary (HMAC signature +
fail-closed), the actor->roles mapping, the typed-command parsing, and the
fixture payload shape.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.api.security import compute_signature, compute_teams_signature
from foundry.api.teams import TeamsInteraction, parse_teams_interaction
from foundry.db import create_all, make_engine, make_session_factory
from foundry.orchestrator import FoundryOrchestrator

SECRET = "test-secret"
# Teams issues the shared token as base64; signing keys off its decoded bytes.
TEAMS_SECRET = base64.b64encode(b"teams-shared-secret").decode("ascii")
# Approvers are keyed by the Teams/AAD object id (as Slack keys by user.id).
APPROVER = "00000000-0000-0000-0000-0000000000aa"
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
        teams_security_token=TEAMS_SECRET,
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


def _activity(command: str, issue_id: str, user: str = APPROVER) -> dict:
    return {
        "type": "message",
        "from": {"id": "29:abc", "aadObjectId": user, "name": "Lead"},
        "text": f"<at>Foundry</at> {command} {issue_id}",
    }


def _post_teams(client, payload: dict, *, secret=TEAMS_SECRET, sign=True):
    body = json.dumps(payload).encode("utf-8")
    auth = "HMAC " + compute_teams_signature(secret, body) if sign else "HMAC deadbeef"
    return client.post(
        "/webhooks/teams",
        content=body,
        headers={"Content-Type": "application/json", "Authorization": auth},
    )


# -- auth boundary -------------------------------------------------------------


def test_teams_disabled_without_security_token() -> None:
    c = _make_client(teams_security_token=None)
    resp = _post_teams(c, _activity("approve", "issue-r"))
    # Fail-closed, same posture as the Slack/Jira/GitLab token endpoints.
    assert resp.status_code == 403


def test_teams_invalid_signature_rejected(client) -> None:
    run_id = _start_ready_run(client)
    resp = _post_teams(client, _activity("approve", "issue-r"), sign=False)
    assert resp.status_code == 401
    assert client.get(f"/runs/{run_id}").json()["status"] == "waiting_approval"


def test_teams_wrong_secret_rejected(client) -> None:
    run_id = _start_ready_run(client)
    other = base64.b64encode(b"nope").decode("ascii")
    resp = _post_teams(client, _activity("approve", "issue-r"), secret=other)
    assert resp.status_code == 401
    assert client.get(f"/runs/{run_id}").json()["status"] == "waiting_approval"


def test_teams_missing_authorization_header_rejected(client) -> None:
    body = json.dumps(_activity("approve", "issue-r")).encode("utf-8")
    resp = client.post(
        "/webhooks/teams",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


# -- decision behaviour --------------------------------------------------------


def test_teams_authorised_approve_dispatches(client) -> None:
    _start_ready_run(client)
    resp = _post_teams(client, _activity("approve", "issue-r"))
    assert resp.status_code == 200
    body = resp.json()
    # The response is a Teams message activity rendered back into the channel.
    assert body["type"] == "message"
    assert "approve applied" in body["text"]
    assert "agent_running" in body["text"]
    # The actor is the Teams-signed AAD id, not anything from the message body.
    assert client.get("/runs").json()["runs"][0]["approved_by"] == APPROVER


def test_teams_unauthorised_user_is_ignored(client) -> None:
    run_id = _start_ready_run(client)
    stranger = "ffffffff-0000-0000-0000-000000000000"
    resp = _post_teams(client, _activity("approve", "issue-r", user=stranger))
    # Acknowledged (200) so Teams does not retry, but refused.
    assert resp.status_code == 200
    assert "no action" in resp.json()["text"]
    assert client.get(f"/runs/{run_id}").json()["status"] == "waiting_approval"


def test_teams_reject_terminates_run(client) -> None:
    run_id = _start_ready_run(client)
    resp = _post_teams(client, _activity("reject", "issue-r"))
    assert resp.status_code == 200
    assert "reject applied" in resp.json()["text"]
    assert client.get(f"/runs/{run_id}").json()["status"] == "rejected"


def test_teams_non_foundry_message_is_ignored(client) -> None:
    _start_ready_run(client)
    payload = {
        "type": "message",
        "from": {"aadObjectId": APPROVER},
        "text": "<at>Foundry</at> what is the status?",
    }
    resp = _post_teams(client, payload)
    assert resp.status_code == 200
    assert "no action" in resp.json()["text"]


def test_teams_no_active_run_is_ignored(client) -> None:
    resp = _post_teams(client, _activity("approve", "issue-absent"))
    assert resp.status_code == 200
    assert "no action" in resp.json()["text"]


def test_teams_unparseable_body_is_ignored(client) -> None:
    # Signed (so it passes auth) but not JSON => acknowledged, no action.
    body = b"not json"
    auth = "HMAC " + compute_teams_signature(TEAMS_SECRET, body)
    resp = client.post(
        "/webhooks/teams",
        content=body,
        headers={"Content-Type": "application/json", "Authorization": auth},
    )
    assert resp.status_code == 200
    assert "no action" in resp.json()["text"]


# -- fixture-pinned payload mapping --------------------------------------------


def test_teams_fixture_payload_drives_a_decision() -> None:
    """The recorded outgoing-webhook fixture maps to an approve on its issue id."""
    payload = json.loads((FIXTURES / "teams_outgoing_webhook_approve.json").read_text())
    user = payload["from"]["aadObjectId"]
    c = _make_client(approvers={user: []})
    _start_ready_run(c)  # fixture targets issue-r
    resp = _post_teams(c, payload)
    assert resp.status_code == 200
    assert "approve applied" in resp.json()["text"]
    assert c.get("/runs").json()["runs"][0]["approved_by"] == user


# -- parse_teams_interaction unit contracts ------------------------------------


def test_parse_extracts_each_decision() -> None:
    for command in ("approve", "reject", "stop"):
        got = parse_teams_interaction(_activity(command, "issue-9", user="u1"))
        assert got == TeamsInteraction(command=command, issue_id="issue-9", user="u1")


def test_parse_prefers_aad_object_id_then_falls_back_to_id() -> None:
    with_aad = {"type": "message", "from": {"id": "29:x", "aadObjectId": "aad-1"},
                "text": "approve i"}
    assert parse_teams_interaction(with_aad).user == "aad-1"
    id_only = {"type": "message", "from": {"id": "29:x"}, "text": "approve i"}
    assert parse_teams_interaction(id_only).user == "29:x"


def test_parse_tolerates_foundry_prefix_and_entities() -> None:
    p = {"type": "message", "from": {"id": "u"},
         "text": "<at>Foundry</at>&nbsp;/foundry stop ISSUE-7"}
    assert parse_teams_interaction(p) == TeamsInteraction("stop", "ISSUE-7", "u")


def test_parse_ignores_non_message_activities() -> None:
    assert parse_teams_interaction({"type": "conversationUpdate"}) is None
    assert parse_teams_interaction({}) is None


def test_parse_ignores_unknown_verbs_and_missing_parts() -> None:
    assert parse_teams_interaction(_activity("frobnicate", "i", "u1")) is None
    # verb but no issue id
    assert parse_teams_interaction(
        {"type": "message", "from": {"id": "u"}, "text": "<at>Foundry</at> approve"}
    ) is None
    # no actor
    assert parse_teams_interaction(
        {"type": "message", "from": {}, "text": "approve i"}
    ) is None
