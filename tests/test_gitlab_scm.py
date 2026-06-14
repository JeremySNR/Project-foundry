"""GitLab as the SCM: MR/pipeline mapping and the webhook loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.api.security import compute_signature
from foundry.connectors.gitlab import GitLabConnector
from foundry.db import create_all, make_engine, make_session_factory
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.common import CIStatus, PRStatus, ReviewStatus

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "linear-secret"
GITLAB_TOKEN = "gitlab-shared-token"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# -- connector mapping -------------------------------------------------------------


def test_merge_request_event_maps_to_pr_state() -> None:
    state = GitLabConnector().pr_state_from_event(
        "Merge Request Hook", load("gitlab_merge_request_opened.json")
    )
    assert state.repo == "acme/customer-web"
    assert state.pr_number == 87
    assert state.branch == "agent/acme-42-add-customer-favourites"
    assert state.title == "ACME-42: Add customer favourites"
    assert state.status is PRStatus.OPEN


def test_draft_and_merged_states_map() -> None:
    payload = load("gitlab_merge_request_opened.json")
    payload["object_attributes"]["draft"] = True
    assert (
        GitLabConnector().pr_state_from_event("Merge Request Hook", payload).status
        is PRStatus.DRAFT
    )
    payload["object_attributes"]["draft"] = False
    payload["object_attributes"]["state"] = "merged"
    assert (
        GitLabConnector().pr_state_from_event("Merge Request Hook", payload).status
        is PRStatus.MERGED
    )


def test_approved_action_sets_review_status() -> None:
    payload = load("gitlab_merge_request_opened.json")
    payload["object_attributes"]["action"] = "approved"
    state = GitLabConnector().pr_state_from_event("Merge Request Hook", payload)
    assert state.review_status is ReviewStatus.APPROVED


# -- changed-file enrichment (the file-based safety gates depend on it) -------


def test_mr_files_empty_without_transport() -> None:
    """No transport => diff-blind, same posture as GitHubConnector unconfigured."""
    state = GitLabConnector().pr_state_from_event(
        "Merge Request Hook", load("gitlab_merge_request_opened.json")
    )
    assert state.files_changed == []


def test_mr_files_enriched_via_transport() -> None:
    def transport(method: str, path: str):
        assert method == "GET"
        assert path == (
            "/projects/acme%2Fcustomer-web/merge_requests/87/diffs?per_page=100&page=1"
        )
        return load("gitlab_mr_diffs.json")

    state = GitLabConnector(transport=transport).pr_state_from_event(
        "Merge Request Hook", load("gitlab_merge_request_opened.json")
    )
    assert state.files_changed == [
        "src/features/favourites/index.ts",
        "src/features/favourites/store.ts",
    ]


def test_mr_files_includes_renamed_old_path_deduped() -> None:
    """A rename reports both new and old path; identical paths collapse to one."""

    def transport(method: str, path: str):
        return [
            {"new_path": "src/new.ts", "old_path": "migrations/0001.sql"},
            {"new_path": "src/keep.ts", "old_path": "src/keep.ts"},
        ]

    files = GitLabConnector(transport=transport).list_mr_files("acme/web", 87)
    assert files == ["src/new.ts", "migrations/0001.sql", "src/keep.ts"]


def test_list_mr_files_paginates_until_short_page() -> None:
    """A forbidden file beyond the first page must still reach the gate."""
    pages = {
        1: [{"new_path": f"src/file_{i}.py"} for i in range(100)],
        2: [{"new_path": f"src/more_{i}.py"} for i in range(49)]
        + [{"new_path": "migrations/0042.py"}],
    }
    requested: list[str] = []

    def transport(method: str, path: str):
        requested.append(path)
        return pages[int(path.rsplit("page=", 1)[1])]

    files = GitLabConnector(transport=transport).list_mr_files("acme/web", 87)
    assert len(files) == 150
    assert "migrations/0042.py" in files
    assert requested == [
        "/projects/acme%2Fweb/merge_requests/87/diffs?per_page=100&page=1",
        "/projects/acme%2Fweb/merge_requests/87/diffs?per_page=100&page=2",
    ]


def test_pipeline_event_maps_ci_failure() -> None:
    state = GitLabConnector().pr_state_from_event(
        "Pipeline Hook", load("gitlab_pipeline_failed.json")
    )
    assert state.ci_status is CIStatus.FAILING
    assert state.pr_number == 87


def test_pipeline_without_merge_request_is_ignored() -> None:
    payload = load("gitlab_pipeline_failed.json")
    payload["merge_request"] = None
    assert GitLabConnector().pr_state_from_event("Pipeline Hook", payload) is None


def test_unknown_event_ignored() -> None:
    assert GitLabConnector().pr_state_from_event("Push Hook", {}) is None


# -- the webhook loop ---------------------------------------------------------------


def make_client(gitlab_connector: GitLabConnector | None = None) -> TestClient:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    return TestClient(
        create_app(
            webhook_secret=SECRET,
            session_factory=sf,
            orchestrator=orch,
            approvers={"lead@example.com": ["engineering"]},
            gitlab_webhook_secret=GITLAB_TOKEN,
            gitlab_connector=gitlab_connector,
        )
    )


@pytest.fixture
def client() -> TestClient:
    return make_client()


def post_gitlab(client, payload, *, event, token=GITLAB_TOKEN):
    return client.post(
        "/webhooks/gitlab",
        json=payload,
        headers={"X-Gitlab-Event": event, "X-Gitlab-Token": token},
    )


def _dispatched_run(client) -> str:
    """Start a Linear-triggered run and approve it (agent running)."""
    intake = {
        "action": "update",
        "type": "Issue",
        "data": {
            "id": "i-jira-mix",
            "identifier": "ACME-42",
            "title": "Add customer favourites",
            "description": (
                "Body.\n\nAcceptance Criteria:\n- A favourites button exists\n"
            ),
            "labels": [{"name": "foundry:candidate"}, {"name": "repo:customer-web"}],
        },
    }
    body = json.dumps(intake).encode()
    resp = client.post(
        "/webhooks/linear",
        content=body,
        headers={
            "Linear-Delivery": "gl-1",
            "Content-Type": "application/json",
            "Linear-Signature": "sha256=" + compute_signature(SECRET, body),
        },
    )
    run_id = resp.json()["run"]["id"]
    approve = {
        "action": "create",
        "type": "Comment",
        "data": {
            "issueId": "i-jira-mix",
            "body": "/foundry approve",
            "actor": {"name": "Lee", "email": "lead@example.com"},
        },
    }
    body = json.dumps(approve).encode()
    client.post(
        "/webhooks/linear",
        content=body,
        headers={
            "Linear-Delivery": "gl-2",
            "Content-Type": "application/json",
            "Linear-Signature": "sha256=" + compute_signature(SECRET, body),
        },
    )
    return run_id


def test_endpoint_fails_closed_without_secret() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    bare = TestClient(
        create_app(webhook_secret="x", session_factory=sf), raise_server_exceptions=False
    )
    resp = bare.post(
        "/webhooks/gitlab",
        json={},
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "anything"},
    )
    assert resp.status_code == 403


def test_bad_token_rejected(client) -> None:
    resp = post_gitlab(
        client,
        load("gitlab_merge_request_opened.json"),
        event="Merge Request Hook",
        token="wrong",
    )
    assert resp.status_code == 401


def test_mr_correlates_to_run_by_issue_key(client) -> None:
    run_id = _dispatched_run(client)
    resp = post_gitlab(
        client, load("gitlab_merge_request_opened.json"), event="Merge Request Hook"
    )
    assert resp.json()["status"] == "recorded"
    assert resp.json()["run_id"] == run_id
    assert resp.json()["run_status"] == "pr_open"


def test_pipeline_failure_triggers_remediation(client) -> None:
    _dispatched_run(client)
    post_gitlab(
        client, load("gitlab_merge_request_opened.json"), event="Merge Request Hook"
    )
    resp = post_gitlab(
        client, load("gitlab_pipeline_failed.json"), event="Pipeline Hook"
    )
    assert resp.json()["status"] == "recorded"
    assert resp.json()["run_status"] == "agent_running"


def test_forbidden_path_in_mr_hard_blocks_run() -> None:
    """A migrations/** file in a GitLab MR hard-blocks, exactly like a GitHub PR.

    This is the bug in #8: without diff enrichment the file-based gate never
    sees the forbidden path and the MR sails through.
    """

    def transport(method: str, path: str):
        return [{"new_path": "migrations/0002_drop_users.sql"}]

    client = make_client(gitlab_connector=GitLabConnector(transport=transport))
    _dispatched_run(client)
    resp = post_gitlab(
        client, load("gitlab_merge_request_opened.json"), event="Merge Request Hook"
    )
    assert resp.json()["status"] == "recorded"
    assert resp.json()["run_status"] == "blocked"


def test_unrelated_mr_ignored(client) -> None:
    payload = load("gitlab_merge_request_opened.json")
    payload["object_attributes"]["title"] = "chore: bump deps"
    payload["object_attributes"]["source_branch"] = "renovate/all-minor"
    resp = post_gitlab(client, payload, event="Merge Request Hook")
    assert resp.json()["status"] == "ignored"
