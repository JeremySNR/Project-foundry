"""Claude Code coding-agent provider.

Dispatches approved work to Claude Code running headless in the target repo's
own CI via a GitHub Actions ``workflow_dispatch`` event. The repo installs a
small reference workflow (see ``examples/claude-code-runner.yml``) that runs
Claude Code with the governed instructions, pushes the branch and opens the PR.
Foundry then observes that PR through the normal GitHub webhook (correlated by
branch name, which Foundry chooses here).

This keeps the trust boundary clean: Foundry never holds an Anthropic key -
the *repo's* CI does - and the agent only ever receives the policy-gated
instructions that passed the secret-leak guard in ``create_job``.
"""

from __future__ import annotations

from typing import Any, Callable

from foundry.schemas.agent import (
    CodingAgentJob,
    CodingAgentJobInput,
    CodingAgentJobStatus,
)
from foundry.schemas.common import AgentJobStatus

from .provider import CodingAgentProvider

# http_post(url, json_body, headers) -> parsed JSON (or None for 204s).
HttpPost = Callable[[str, dict[str, Any], dict[str, str]], Any]

DEFAULT_WORKFLOW_FILE = "foundry-claude-code.yml"
GITHUB_API_BASE = "https://api.github.com"


class ClaudeCodeProvider(CodingAgentProvider):
    """Launch Claude Code via ``workflow_dispatch`` in the target repository."""

    name = "claude_code"

    def __init__(
        self,
        *,
        http_post: HttpPost,
        workflow_file: str = DEFAULT_WORKFLOW_FILE,
        base_url: str = GITHUB_API_BASE,
    ) -> None:
        self._http_post = http_post
        self._workflow_file = workflow_file
        self._base_url = base_url.rstrip("/")

    def _dispatch(self, job_input: CodingAgentJobInput) -> CodingAgentJob:
        url = (
            f"{self._base_url}/repos/{job_input.repo}/actions/workflows/"
            f"{self._workflow_file}/dispatches"
        )
        body = {
            "ref": job_input.base_branch,
            "inputs": {
                # GitHub limits workflow_dispatch inputs to strings.
                "run_id": job_input.run_id,
                "branch_name": job_input.branch_name,
                "ticket_url": job_input.ticket_url,
                "instructions": job_input.agent_instructions,
                "do_not_modify": "\n".join(job_input.constraints.do_not_modify),
                "required_tests": "\n".join(job_input.constraints.required_tests),
            },
        }
        # The GitHub token comes from the injected transport's headers; it is
        # never part of the job input, so the secret guard never sees it.
        self._http_post(url, body, {})
        return CodingAgentJob(
            job_id=f"claude-gha:{job_input.repo}:{job_input.branch_name}",
            provider=self.name,
            status=AgentJobStatus.RUNNING,
        )

    def get_job_status(self, job_id: str) -> CodingAgentJobStatus:
        # Progress is observed out-of-band: the workflow opens a PR on the
        # branch Foundry chose, and the GitHub webhook drives record_pr.
        return CodingAgentJobStatus(
            job_id=job_id, provider=self.name, status=AgentJobStatus.RUNNING
        )

    def cancel_job(self, job_id: str) -> None:  # pragma: no cover - human action
        # Cancelling a workflow run is a human action in the GitHub UI for now.
        return None
