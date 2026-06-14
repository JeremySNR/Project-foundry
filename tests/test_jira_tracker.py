"""Jira as the issue tracker: connector, mapping, and the webhook loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.api.mapping import jira_payload_to_ticket
from foundry.connectors.jira import JiraConnector
from foundry.db import create_all, make_engine, make_session_factory
from foundry.orchestrator import FoundryOrchestrator

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "jira-shared-token"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# -- connector over a fake transport ----------------------------------------------


class FakeJira:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.issue = {
            "key": "ACME-42",
            "fields": {
                "summary": "Add customer favourites",
                "description": "Body.\n\nAcceptance Criteria:\n- works",
                "labels": ["foundry:candidate", "repo:customer-web"],
            },
        }
        self.transitions = {
            "transitions": [
                {"id": "11", "name": "Start work", "to": {"name": "In Progress"}},
                {"id": "31", "name": "Done", "to": {"name": "Done"}},
            ]
        }

    def __call__(self, method: str, path: str, body: object | None = None):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/transitions"):
            return self.transitions
        if method == "GET":
            return self.issue
        return None


def test_connector_get_issue_maps_ticket() -> None:
    ticket = JiraConnector(transport=FakeJira()).get_issue("ACME-42")
    assert ticket.issue_key == "ACME-42"  # Jira keys need no synthesis
    assert ticket.title == "Add customer favourites"
    assert ticket.known_repositories == ["customer-web"]


def test_connector_post_comment() -> None:
    jira = FakeJira()
    JiraConnector(transport=jira).post_comment("ACME-42", "hello")
    assert ("POST", "/rest/api/2/issue/ACME-42/comment", {"body": "hello"}) in jira.calls


def test_set_state_fires_matching_transition() -> None:
    jira = FakeJira()
    JiraConnector(transport=jira).set_state("ACME-42", "Foundry: In Progress")
    assert jira.calls[-1] == (
        "POST",
        "/rest/api/2/issue/ACME-42/transitions",
        {"transition": {"id": "11"}},
    )


def test_set_state_does_not_match_blocked_to_unblocked() -> None:
    """Substring matching used to fire 'Unblocked' when asked for 'Blocked'."""
    jira = FakeJira()
    jira.transitions = {
        "transitions": [
            {"id": "41", "name": "Unblock", "to": {"name": "Unblocked"}},
            {"id": "42", "name": "Block", "to": {"name": "Blocked"}},
        ]
    }
    JiraConnector(transport=jira).set_state("ACME-42", "Blocked")
    # The Blocked transition fires, never the Unblocked one whose name happens
    # to contain "blocked".
    assert jira.calls[-1] == (
        "POST",
        "/rest/api/2/issue/ACME-42/transitions",
        {"transition": {"id": "42"}},
    )


def test_set_state_phrase_match_still_works() -> None:
    """Word-boundary containment keeps matching decorated workflow names."""
    jira = FakeJira()
    jira.transitions = {
        "transitions": [
            {"id": "51", "name": "Start", "to": {"name": "In Progress (dev)"}},
        ]
    }
    JiraConnector(transport=jira).set_state("ACME-42", "Foundry: In Progress")
    assert jira.calls[-1] == (
        "POST",
        "/rest/api/2/issue/ACME-42/transitions",
        {"transition": {"id": "51"}},
    )


def test_set_state_without_matching_transition_is_a_noop() -> None:
    jira = FakeJira()
    JiraConnector(transport=jira).set_state("ACME-42", "Foundry: Waiting Approval")
    # Only the GET; Foundry never invents workflow states in someone's Jira.
    assert all(method == "GET" for method, _, _ in jira.calls)


# -- payload mapping ---------------------------------------------------------------


def test_issue_payload_maps_to_ticket() -> None:
    ticket = jira_payload_to_ticket(load("jira_issue_labeled.json"))
    assert ticket.issue_id == "ACME-42"
    assert ticket.issue_key == "ACME-42"
    assert ticket.known_repositories == ["customer-web"]
    assert "foundry:candidate" in ticket.labels


# -- the webhook loop ---------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    return TestClient(
        create_app(
            webhook_secret="linear-secret-unused-here",
            session_factory=sf,
            orchestrator=orch,
            approvers={"lead@example.com": ["engineering"]},
            jira_webhook_secret=SECRET,
        )
    )


def post_jira(client, payload, *, token=SECRET, via_query=False):
    url = f"/webhooks/jira?token={token}" if via_query else "/webhooks/jira"
    headers = {} if via_query else {"X-Foundry-Webhook-Token": token}
    return client.post(url, json=payload, headers=headers)


def test_endpoint_fails_closed_without_secret() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    bare = TestClient(
        create_app(webhook_secret="x", session_factory=sf), raise_server_exceptions=False
    )
    resp = bare.post("/webhooks/jira", json=load("jira_issue_labeled.json"))
    assert resp.status_code == 403


def test_bad_token_rejected(client) -> None:
    resp = post_jira(client, load("jira_issue_labeled.json"), token="wrong")
    assert resp.status_code == 401


def test_labeled_issue_starts_run(client) -> None:
    resp = post_jira(client, load("jira_issue_labeled.json"))
    assert resp.json()["status"] == "started"
    run = resp.json()["run"]
    assert run["linear_issue_key"] == "ACME-42"
    assert run["status"] == "waiting_approval"
    assert run["created_by"] == "priya@example.com"


def test_token_via_query_param_rejected_by_default(client) -> None:
    # The Jira token is an approver-level credential and query-string secrets
    # leak into access logs/proxies; header-only is the default posture.
    resp = post_jira(client, load("jira_issue_labeled.json"), via_query=True)
    assert resp.status_code == 401
    assert client.get("/runs").json()["runs"] == []


def _query_token_client() -> TestClient:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    return TestClient(
        create_app(
            webhook_secret="linear-secret-unused-here",
            session_factory=sf,
            orchestrator=orch,
            approvers={"lead@example.com": ["engineering"]},
            jira_webhook_secret=SECRET,
            jira_allow_query_token=True,
        )
    )


def test_token_via_query_param_accepted_when_opted_in() -> None:
    client = _query_token_client()
    resp = post_jira(client, load("jira_issue_labeled.json"), via_query=True)
    assert resp.json()["status"] == "started"


def test_header_token_still_works_when_query_opted_in() -> None:
    # Opting into query delivery does not disable the header path.
    client = _query_token_client()
    resp = post_jira(client, load("jira_issue_labeled.json"))
    assert resp.json()["status"] == "started"


def test_comment_approval_dispatches_agent(client) -> None:
    post_jira(client, load("jira_issue_labeled.json"))
    resp = post_jira(client, load("jira_comment_approve.json"))
    assert resp.json()["status"] == "applied"
    assert resp.json()["dispatched"] is True


def test_unauthorised_email_cannot_approve(client) -> None:
    post_jira(client, load("jira_issue_labeled.json"))
    payload = load("jira_comment_approve.json")
    payload["comment"]["author"]["emailAddress"] = "rando@example.com"
    resp = post_jira(client, payload)
    assert resp.json()["status"] == "ignored"
    assert "not an authorised approver" in resp.json()["reason"]


def test_plain_comment_does_not_start_run(client) -> None:
    payload = load("jira_comment_approve.json")
    payload["comment"]["body"] = "can't wait!"
    resp = post_jira(client, payload)
    assert resp.json()["status"] == "ignored"
    assert client.get("/runs").json()["runs"] == []
