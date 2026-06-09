"""Tests for the Cursor providers (via-Linear delegation and direct Cloud API)."""

from __future__ import annotations

import pytest

from foundry.agents import (
    CursorCloudAgentProvider,
    CursorViaLinearProvider,
    SecretLeakError,
)
from foundry.connectors import InMemoryIssueTracker
from foundry.schemas.agent import CodingAgentJobInput
from foundry.schemas.common import AgentJobStatus


def _job_input(**overrides) -> CodingAgentJobInput:
    base = {
        "run_id": "run-1",
        "repo": "customer-web",
        "branch_name": "foundry/lin-123-favourites",
        "ticket_url": "https://linear.app/issue/LIN-123",
        "delivery_plan": {"goal": "Add favourites"},
        "agent_instructions": "Implement favourites per the plan.",
        "tracker_issue_id": "issue-uuid",
    }
    base.update(overrides)
    return CodingAgentJobInput.model_validate(base)


# -- CursorViaLinearProvider --------------------------------------------------


def test_via_linear_posts_cursor_delegation_comment() -> None:
    tracker = InMemoryIssueTracker()
    provider = CursorViaLinearProvider(tracker)
    job = provider.create_job(_job_input())
    assert job.provider == "cursor_via_linear"
    assert job.status is AgentJobStatus.RUNNING
    comment = tracker.comments["issue-uuid"][0]
    assert comment.startswith("@Cursor")
    assert "Implement favourites per the plan." in comment


def test_via_linear_requires_tracker_issue_id() -> None:
    provider = CursorViaLinearProvider(InMemoryIssueTracker())
    with pytest.raises(ValueError):
        provider.create_job(_job_input(tracker_issue_id=None))


def test_via_linear_secret_guard_blocks_before_posting() -> None:
    tracker = InMemoryIssueTracker()
    provider = CursorViaLinearProvider(tracker)
    with pytest.raises(SecretLeakError):
        provider.create_job(
            _job_input(agent_instructions="token=abcdef1234567890 do the thing")
        )
    assert tracker.comments == {}  # nothing leaked to the tracker


# -- CursorCloudAgentProvider -------------------------------------------------


class FakeHttp:
    def __init__(self, post_response: dict, get_response: dict | None = None) -> None:
        self.post_calls: list[tuple[str, dict, dict]] = []
        self.get_calls: list[tuple[str, dict]] = []
        self._post_response = post_response
        self._get_response = get_response or {}

    def post(self, url: str, body: dict, headers: dict) -> dict:
        self.post_calls.append((url, body, headers))
        return self._post_response

    def get(self, url: str, headers: dict) -> dict:
        self.get_calls.append((url, headers))
        return self._get_response


def test_cloud_provider_launches_agent() -> None:
    http = FakeHttp({"id": "agent_1", "status": "CREATING"})
    provider = CursorCloudAgentProvider(http_post=http.post, http_get=http.get)
    job = provider.create_job(_job_input())

    assert job.job_id == "agent_1"
    assert job.status is AgentJobStatus.CREATED

    url, body, _headers = http.post_calls[0]
    assert url == "https://api.cursor.com/v0/agents"
    assert body["prompt"]["text"] == "Implement favourites per the plan."
    assert body["source"]["repository"] == "https://github.com/customer-web"
    assert body["target"] == {
        "autoCreatePr": True,
        "branchName": "foundry/lin-123-favourites",
    }


def test_cloud_provider_maps_status_and_pr() -> None:
    http = FakeHttp(
        {"id": "agent_1", "status": "CREATING"},
        {"status": "FINISHED", "target": {"branchName": "b", "prUrl": "http://pr/1"}},
    )
    provider = CursorCloudAgentProvider(http_post=http.post, http_get=http.get)
    provider.create_job(_job_input())
    status = provider.get_job_status("agent_1")
    assert status.status is AgentJobStatus.SUCCEEDED
    assert status.pr_url == "http://pr/1"


def test_cloud_provider_full_repo_url_passthrough() -> None:
    http = FakeHttp({"id": "a", "status": "RUNNING"})
    provider = CursorCloudAgentProvider(http_post=http.post)
    provider.create_job(_job_input(repo="https://github.com/org/repo"))
    assert http.post_calls[0][1]["source"]["repository"] == "https://github.com/org/repo"


def test_cloud_provider_captures_cost() -> None:
    http = FakeHttp(
        {"id": "agent_1", "status": "CREATING"},
        {"status": "FINISHED", "target": {}, "usage": {"totalCostUsd": 3.42}},
    )
    provider = CursorCloudAgentProvider(http_post=http.post, http_get=http.get)
    provider.create_job(_job_input())
    assert provider.get_job_status("agent_1").cost_usd == 3.42


def test_cloud_provider_cost_absent_is_none() -> None:
    http = FakeHttp({"id": "a", "status": "RUNNING"}, {"status": "RUNNING", "target": {}})
    provider = CursorCloudAgentProvider(http_post=http.post, http_get=http.get)
    provider.create_job(_job_input())
    assert provider.get_job_status("a").cost_usd is None
