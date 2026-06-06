"""Providers that do not depend on an external automation API.

``ManualProvider`` is the safe default for the MVP: it records an approved job
for a human to pick up (e.g. launch Cursor by hand) rather than dispatching
autonomously. ``InMemoryFakeProvider`` simulates the full lifecycle for tests
and integration checks against a fake repo.
"""

from __future__ import annotations

import uuid

from foundry.schemas.agent import (
    CodingAgentJob,
    CodingAgentJobInput,
    CodingAgentJobStatus,
)
from foundry.schemas.common import AgentJobStatus

from .provider import CodingAgentProvider


class ManualProvider(CodingAgentProvider):
    """Hands the approved plan to a human. Never writes to a repo itself."""

    name = "manual"

    def __init__(self) -> None:
        self._jobs: dict[str, CodingAgentJobStatus] = {}

    def _dispatch(self, job_input: CodingAgentJobInput) -> CodingAgentJob:
        job_id = f"manual-{uuid.uuid4()}"
        self._jobs[job_id] = CodingAgentJobStatus(
            job_id=job_id,
            provider=self.name,
            status=AgentJobStatus.CREATED,
            branch=job_input.branch_name,
        )
        return CodingAgentJob(job_id=job_id, provider=self.name)

    def get_job_status(self, job_id: str) -> CodingAgentJobStatus:
        return self._jobs[job_id]

    def cancel_job(self, job_id: str) -> None:
        if job_id in self._jobs:
            self._jobs[job_id].status = AgentJobStatus.CANCELLED


class InMemoryFakeProvider(CodingAgentProvider):
    """Test double that simulates a provider creating a branch and a PR.

    ``run`` advances a created job to ``SUCCEEDED`` with a synthetic PR URL so
    integration tests can exercise the downstream PR monitor without GitHub.
    """

    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self._jobs: dict[str, CodingAgentJobStatus] = {}
        self._inputs: dict[str, CodingAgentJobInput] = {}
        self._fail = fail

    def _dispatch(self, job_input: CodingAgentJobInput) -> CodingAgentJob:
        job_id = f"fake-{uuid.uuid4()}"
        self._inputs[job_id] = job_input
        self._jobs[job_id] = CodingAgentJobStatus(
            job_id=job_id,
            provider=self.name,
            status=AgentJobStatus.RUNNING,
            branch=job_input.branch_name,
        )
        return CodingAgentJob(
            job_id=job_id, provider=self.name, status=AgentJobStatus.RUNNING
        )

    def run(self, job_id: str) -> CodingAgentJobStatus:
        """Simulate the agent finishing its work."""
        status = self._jobs[job_id]
        job_input = self._inputs[job_id]
        if self._fail:
            status.status = AgentJobStatus.FAILED
            status.error = "simulated provider failure"
        else:
            status.status = AgentJobStatus.SUCCEEDED
            status.pr_url = (
                f"https://github.com/example/{job_input.repo}/pull/1"
            )
        return status

    def get_job_status(self, job_id: str) -> CodingAgentJobStatus:
        return self._jobs[job_id]

    def cancel_job(self, job_id: str) -> None:
        if job_id in self._jobs:
            self._jobs[job_id].status = AgentJobStatus.CANCELLED
