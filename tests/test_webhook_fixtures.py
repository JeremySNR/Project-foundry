"""Recorded webhook fixtures driven through the real signed endpoints.

The payloads in ``tests/fixtures/`` mirror what Linear and GitHub actually
send. These tests are the contract for the payload mappings: when a live
integration exposes a mapping bug, the fix starts with a (redacted) captured
payload landing in that directory and an assertion landing here. No
credentials required - which is the point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.api.mapping import linear_payload_to_ticket
from foundry.api.security import compute_signature
from foundry.connectors.github import GitHubConnector
from foundry.db import create_all, make_engine, make_session_factory
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.common import CIStatus, PRStatus, ReviewStatus

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "fixture-secret"
APPROVERS = {"lead@example.com": ["engineering", "security"]}


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def client() -> TestClient:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    return TestClient(
        create_app(
            webhook_secret=SECRET,
            session_factory=sf,
            orchestrator=orch,
            approvers=APPROVERS,
        )
    )


def post_linear(client, payload, *, delivery):
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhooks/linear",
        content=body,
        headers={
            "Linear-Delivery": delivery,
            "Content-Type": "application/json",
            "Linear-Signature": "sha256=" + compute_signature(SECRET, body),
        },
    )


def post_github(client, payload, *, event):
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=" + compute_signature(SECRET, body),
        },
    )


# -- pure mapping assertions -----------------------------------------------------


def test_linear_issue_fixture_maps_to_ticket() -> None:
    ticket = linear_payload_to_ticket(load("linear_issue_labeled.json"))
    assert ticket.issue_id == "a1b2c3d4-0000-4000-9000-1234567890ab"
    assert ticket.issue_key == "ACME-42"
    assert ticket.title == "Add customer favourites"
    assert "Acceptance Criteria:" in ticket.description
    assert "foundry:candidate" in ticket.labels
    assert ticket.known_repositories == ["customer-web"]


def test_github_pr_fixture_maps_to_pr_state() -> None:
    state = GitHubConnector().pr_state_from_event(
        "pull_request", load("github_pull_request_opened.json")
    )
    assert state.repo == "acme/customer-web"
    assert state.pr_number == 87
    assert state.branch == "cursor/acme-42-add-customer-favourites"
    assert state.title == "ACME-42: Add customer favourites"
    # Cursor opens draft PRs; the connector maps "draft": true accordingly.
    assert state.status is PRStatus.DRAFT


def test_github_check_suite_fixture_maps_ci_failure() -> None:
    state = GitHubConnector().pr_state_from_event(
        "check_suite", load("github_check_suite_failed.json")
    )
    assert state.ci_status is CIStatus.FAILING
    assert state.branch == "cursor/acme-42-add-customer-favourites"


def test_github_review_fixture_maps_changes_requested() -> None:
    state = GitHubConnector().pr_state_from_event(
        "pull_request_review", load("github_review_changes_requested.json")
    )
    assert state.review_status is ReviewStatus.CHANGES_REQUESTED


# -- the full loop through the signed endpoints -----------------------------------


def test_fixture_payloads_drive_the_whole_loop(client) -> None:
    """Label trigger -> approval comment -> PR -> CI failure -> remediation,
    using only recorded payload shapes."""
    started = post_linear(client, load("linear_issue_labeled.json"), delivery="fx-1")
    assert started.json()["status"] == "started"
    run = started.json()["run"]
    assert run["linear_issue_key"] == "ACME-42"
    assert run["status"] == "waiting_approval"

    approved = post_linear(
        client, load("linear_comment_approve.json"), delivery="fx-2"
    )
    assert approved.json()["status"] == "applied"
    assert approved.json()["dispatched"] is True

    # The delegated agent opened a PR on its own branch; the embedded issue key
    # correlates it back to the run.
    pr = post_github(
        client, load("github_pull_request_opened.json"), event="pull_request"
    )
    assert pr.json()["status"] == "recorded"
    assert pr.json()["run_status"] == "pr_open"

    # CI fails -> governed remediation re-dispatches the agent.
    ci = post_github(
        client, load("github_check_suite_failed.json"), event="check_suite"
    )
    assert ci.json()["status"] == "recorded"
    assert ci.json()["run_status"] == "agent_running"
