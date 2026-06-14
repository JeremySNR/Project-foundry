"""GitHub Issues as the tracker: connector, mapping, and the webhook loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.api.mapping import github_issue_payload_to_ticket
from foundry.api.security import compute_signature
from foundry.connectors.github_issues import (
    GitHubIssuesConnector,
    github_issue_key,
    split_issue_id,
)
from foundry.db import create_all, make_engine, make_session_factory
from foundry.orchestrator import FoundryOrchestrator

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "gh-issues-secret"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# -- key + id helpers ------------------------------------------------------------


def test_issue_key_matches_correlation_pattern() -> None:
    import re

    pattern = re.compile(r"\b([A-Za-z][A-Za-z0-9]{1,9}-\d+)\b")
    for repo, number in [
        ("acme/customer-web", 42),
        ("acme/x", 1),
        ("org/very-long-repository-name", 12345),
    ]:
        key = github_issue_key(repo, number)
        assert pattern.fullmatch(key), key


def test_issue_keys_for_similarly_named_repos_do_not_collide() -> None:
    """Repos that normalise to the same alnum prefix must get distinct keys."""
    # Same number, different repos that previously both became MYAPP-7 / WEB-7.
    assert github_issue_key("acme/my-app", 7) != github_issue_key("acme/myapp", 7)
    assert github_issue_key("acme/web", 7) != github_issue_key("beta/web", 7)
    # Deterministic: same input always yields the same key (correlation relies
    # on regenerating the identical key when a PR is observed).
    assert github_issue_key("acme/web", 7) == github_issue_key("acme/web", 7)


def test_split_issue_id_roundtrip() -> None:
    assert split_issue_id("acme/customer-web#42") == ("acme/customer-web", "42")
    with pytest.raises(ValueError):
        split_issue_id("no-hash-here")


# -- connector over a fake transport ----------------------------------------------


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.issue = {
            "number": 42,
            "title": "Add customer favourites",
            "body": "Body text.\n\nAcceptance Criteria:\n- works",
            "labels": [{"name": "foundry:candidate"}, {"name": "repo:customer-web"}],
        }

    def __call__(self, method: str, path: str, body: object | None = None):
        self.calls.append((method, path, body))
        if method == "GET":
            return self.issue
        return None


def test_connector_get_issue_maps_ticket() -> None:
    gh = FakeGitHub()
    ticket = GitHubIssuesConnector(transport=gh).get_issue("acme/customer-web#42")
    assert ticket.issue_key == github_issue_key("acme/customer-web", 42)
    assert ticket.title == "Add customer favourites"
    assert "Acceptance Criteria:" in ticket.description
    # The explicit repo: label wins; the host repo is only the fallback
    # (two confident candidates would read as ambiguity and park the run).
    assert ticket.known_repositories == ["customer-web"]


def test_connector_post_comment_and_set_state() -> None:
    gh = FakeGitHub()
    connector = GitHubIssuesConnector(transport=gh)
    connector.post_comment("acme/customer-web#42", "hello")
    assert ("POST", "/repos/acme/customer-web/issues/42/comments", {"body": "hello"}) in gh.calls

    gh.issue["labels"].append({"name": "foundry:status:in-progress"})
    connector.set_state("acme/customer-web#42", "Foundry: Waiting Approval")

    # Additive POST of the new status label, never a whole-set PUT (which would
    # clobber a concurrent edit to an unrelated label).
    assert (
        "POST",
        "/repos/acme/customer-web/issues/42/labels",
        {"labels": ["foundry:status:waiting-approval"]},
    ) in gh.calls
    assert not any(method == "PUT" for method, _, _ in gh.calls)
    # The stale status label is removed by a targeted DELETE; non-status labels
    # (foundry:candidate, repo:customer-web) are never touched.
    deletes = [path for method, path, _ in gh.calls if method == "DELETE"]
    assert deletes == [
        "/repos/acme/customer-web/issues/42/labels/foundry%3Astatus%3Ain-progress"
    ]


def test_set_state_first_status_label_makes_no_delete() -> None:
    """With no prior foundry:status: label there is nothing to remove."""
    gh = FakeGitHub()  # issue starts with candidate + repo labels only
    GitHubIssuesConnector(transport=gh).set_state(
        "acme/customer-web#42", "Foundry: Waiting Approval"
    )
    assert not any(method == "DELETE" for method, _, _ in gh.calls)
    assert (
        "POST",
        "/repos/acme/customer-web/issues/42/labels",
        {"labels": ["foundry:status:waiting-approval"]},
    ) in gh.calls


# -- payload mapping ---------------------------------------------------------------


def test_issue_payload_maps_to_ticket() -> None:
    ticket = github_issue_payload_to_ticket(load("github_issue_labeled.json"))
    assert ticket.issue_id == "acme/customer-web#42"
    assert ticket.issue_key == github_issue_key("acme/customer-web", 42)
    assert ticket.known_repositories == ["customer-web"]  # repo: label wins
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
            webhook_secret=SECRET,
            session_factory=sf,
            orchestrator=orch,
            # GitHub Issues approvers are keyed by login, not email.
            approvers={"lee-cardall": ["engineering", "security"]},
        )
    )


def post_github(client, payload, *, event, delivery="d-1"):
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=" + compute_signature(SECRET, body),
        },
    )


def test_labeled_issue_starts_run(client) -> None:
    resp = post_github(client, load("github_issue_labeled.json"), event="issues")
    assert resp.json()["status"] == "started"
    run = resp.json()["run"]
    assert run["linear_issue_id"] == "acme/customer-web#42"
    assert run["status"] == "waiting_approval"
    assert run["created_by"] == "priya-patel"


def test_duplicate_delivery_ignored(client) -> None:
    payload = load("github_issue_labeled.json")
    post_github(client, payload, event="issues", delivery="dup")
    second = post_github(client, payload, event="issues", delivery="dup")
    assert second.json()["status"] == "duplicate"
    assert len(client.get("/runs").json()["runs"]) == 1


def test_comment_approval_dispatches_agent(client) -> None:
    post_github(client, load("github_issue_labeled.json"), event="issues", delivery="d1")
    resp = post_github(
        client,
        load("github_issue_comment_approve.json"),
        event="issue_comment",
        delivery="d2",
    )
    assert resp.json()["status"] == "applied"
    assert resp.json()["dispatched"] is True


def test_unauthorised_login_cannot_approve(client) -> None:
    post_github(client, load("github_issue_labeled.json"), event="issues", delivery="d1")
    payload = load("github_issue_comment_approve.json")
    payload["comment"]["user"]["login"] = "drive-by-rando"
    payload["sender"]["login"] = "drive-by-rando"
    resp = post_github(client, payload, event="issue_comment", delivery="d2")
    assert resp.json()["status"] == "ignored"
    assert "not an authorised approver" in resp.json()["reason"]


def test_comment_on_labelled_issue_does_not_restart_run(client) -> None:
    """The label triggers only on issue events; chatter must not start runs."""
    payload = load("github_issue_comment_approve.json")
    payload["comment"]["body"] = "looking forward to this one!"
    resp = post_github(client, payload, event="issue_comment", delivery="d9")
    assert resp.json()["status"] == "ignored"
    assert client.get("/runs").json()["runs"] == []


def test_pr_correlates_back_via_synthesised_key(client) -> None:
    """A delegated agent embeds the synthesised issue key in its title; loop closes."""
    post_github(client, load("github_issue_labeled.json"), event="issues", delivery="d1")
    post_github(
        client,
        load("github_issue_comment_approve.json"),
        event="issue_comment",
        delivery="d2",
    )
    key = github_issue_key("acme/customer-web", 42)
    pr_payload = {
        "action": "opened",
        "pull_request": {
            "number": 87,
            "html_url": "https://github.com/acme/customer-web/pull/87",
            "state": "open",
            "title": f"{key}: add favourites",
            "merged": False,
            "head": {"ref": "agent/some-opaque-branch"},
        },
        "repository": {"full_name": "acme/customer-web"},
    }
    resp = post_github(client, pr_payload, event="pull_request", delivery="d3")
    assert resp.json()["status"] == "recorded"
    assert resp.json()["run_status"] == "pr_open"
