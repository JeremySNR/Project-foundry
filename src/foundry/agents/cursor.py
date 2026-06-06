"""Cursor coding-agent providers.

Two ways to hand approved work to Cursor, both implementing
:class:`CodingAgentProvider`:

- :class:`CursorViaLinearProvider` - the *preferred* path. Foundry has already
  gathered context, classified risk and obtained approval; it then delegates to
  Cursor through Linear by posting an ``@Cursor`` comment with the governed
  instructions. Cursor's Linear integration runs the cloud agent, reports status
  back in Linear and auto-opens the PR. Foundry stays the control plane above.
  (Requires the Cursor⨉Linear integration: a Cursor admin connection + GitHub.)

- :class:`CursorCloudAgentProvider` - the direct Cloud Agents API
  (``POST https://api.cursor.com/v0/agents``). Useful when there is no Linear
  hand-off (e.g. a non-Linear trigger). The HTTP calls are injected so this is
  testable without network and without a real Cursor key.

Both go through ``create_job`` on the base class, so the secret-leak guard runs
before anything is dispatched.
"""

from __future__ import annotations

from typing import Any, Callable

from foundry.connectors.base import IssueTracker
from foundry.connectors.comments import format_cursor_delegation
from foundry.schemas.agent import (
    CodingAgentJob,
    CodingAgentJobInput,
    CodingAgentJobStatus,
)
from foundry.schemas.common import AgentJobStatus

from .provider import CodingAgentProvider

# Cursor cloud-agent status -> Foundry job status.
_CURSOR_STATUS = {
    "CREATING": AgentJobStatus.CREATED,
    "PENDING": AgentJobStatus.CREATED,
    "RUNNING": AgentJobStatus.RUNNING,
    "FINISHED": AgentJobStatus.SUCCEEDED,
    "COMPLETED": AgentJobStatus.SUCCEEDED,
    "ERROR": AgentJobStatus.FAILED,
    "FAILED": AgentJobStatus.FAILED,
    "CANCELLED": AgentJobStatus.CANCELLED,
    "EXPIRED": AgentJobStatus.CANCELLED,
}


class CursorViaLinearProvider(CodingAgentProvider):
    """Delegate to Cursor by commenting ``@Cursor`` on the Linear issue."""

    name = "cursor_via_linear"

    def __init__(self, tracker: IssueTracker) -> None:
        self._tracker = tracker

    def _dispatch(self, job_input: CodingAgentJobInput) -> CodingAgentJob:
        if not job_input.tracker_issue_id:
            raise ValueError(
                "CursorViaLinearProvider requires job_input.tracker_issue_id "
                "(the Linear issue to delegate on)"
            )
        body = format_cursor_delegation(job_input.agent_instructions)
        self._tracker.post_comment(job_input.tracker_issue_id, body)
        # The agent now runs inside Cursor's Linear integration; progress and the
        # PR arrive via Linear/GitHub webhooks, which drive orchestrator.record_pr.
        return CodingAgentJob(
            job_id=f"cursor-linear:{job_input.tracker_issue_id}",
            provider=self.name,
            status=AgentJobStatus.RUNNING,
        )

    def get_job_status(self, job_id: str) -> CodingAgentJobStatus:
        # Status is observed out-of-band (Linear/GitHub), not polled here.
        return CodingAgentJobStatus(
            job_id=job_id, provider=self.name, status=AgentJobStatus.RUNNING
        )

    def cancel_job(self, job_id: str) -> None:  # pragma: no cover - no-op
        # Cancellation is a human action in Linear/Cursor for this path.
        return None


class CursorCloudAgentProvider(CodingAgentProvider):
    """Launch a Cursor cloud agent directly via the Cloud Agents API."""

    name = "cursor_cloud"
    _AGENTS_URL = "https://api.cursor.com/v0/agents"

    def __init__(
        self,
        *,
        http_post: Callable[[str, dict, dict], dict],
        http_get: Callable[[str, dict], dict] | None = None,
        auto_create_pr: bool = True,
    ) -> None:
        # http_post(url, json_body, headers) -> response json
        # http_get(url, headers) -> response json
        self._http_post = http_post
        self._http_get = http_get
        self._auto_create_pr = auto_create_pr

    def _dispatch(self, job_input: CodingAgentJobInput) -> CodingAgentJob:
        body = {
            "prompt": {"text": job_input.agent_instructions},
            "source": {
                "repository": self._repo_url(job_input.repo),
                "ref": job_input.base_branch,
            },
            "target": {
                "autoCreatePr": self._auto_create_pr,
                "branchName": job_input.branch_name,
            },
        }
        # The API key is supplied by the injected transport's headers, never in
        # the job input - so the secret guard never sees it.
        response = self._http_post(self._AGENTS_URL, body, {})
        return CodingAgentJob(
            job_id=str(response["id"]),
            provider=self.name,
            status=_CURSOR_STATUS.get(response.get("status", ""), AgentJobStatus.CREATED),
        )

    def get_job_status(self, job_id: str) -> CodingAgentJobStatus:
        if self._http_get is None:  # pragma: no cover - requires injected client
            raise RuntimeError("CursorCloudAgentProvider needs http_get to poll status")
        data = self._http_get(f"{self._AGENTS_URL}/{job_id}", {})
        target = data.get("target", {}) or {}
        return CodingAgentJobStatus(
            job_id=job_id,
            provider=self.name,
            status=_CURSOR_STATUS.get(data.get("status", ""), AgentJobStatus.RUNNING),
            branch=target.get("branchName"),
            pr_url=target.get("prUrl") or target.get("url"),
        )

    def cancel_job(self, job_id: str) -> None:
        self._http_post(f"{self._AGENTS_URL}/{job_id}/cancel", {}, {})

    @staticmethod
    def _repo_url(repo: str) -> str:
        if repo.startswith("http://") or repo.startswith("https://"):
            return repo
        return f"https://github.com/{repo}"
