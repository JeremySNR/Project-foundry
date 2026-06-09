"""Generic webhook coding-agent provider.

The escape hatch that keeps Foundry honest about vendor neutrality: instead of
Foundry shipping an adapter for every agent (Codex CLI, Aider, an internal
tool), you point this provider at *your* endpoint. Foundry POSTs the governed
job input as JSON, HMAC-signed so your receiver can verify it really came from
Foundry, and then watches for the PR through the normal GitHub webhook.

Receiver contract:

- ``POST <url>`` with the ``CodingAgentJobInput`` JSON body.
- ``X-Foundry-Signature: sha256=<hex hmac of the raw body>`` when a secret is
  configured. Verify it.
- Respond 2xx. An optional JSON body ``{"job_id": "..."}`` names the job;
  otherwise Foundry synthesises one from the run id.
- Do the work on ``branch_name`` and open a PR; Foundry takes it from there.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Callable

from foundry.schemas.agent import (
    CodingAgentJob,
    CodingAgentJobInput,
    CodingAgentJobStatus,
)
from foundry.schemas.common import AgentJobStatus

from .provider import CodingAgentProvider

# http_post(url, raw_body_bytes, headers) -> parsed JSON (or None).
RawHttpPost = Callable[[str, bytes, dict[str, str]], Any]


def sign_payload(secret: str, body: bytes) -> str:
    """The signature value sent in ``X-Foundry-Signature``."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class WebhookProvider(CodingAgentProvider):
    """POST the governed job input to a configured endpoint."""

    name = "webhook"

    def __init__(
        self,
        *,
        url: str,
        http_post: RawHttpPost,
        signing_secret: str | None = None,
    ) -> None:
        self._url = url
        self._http_post = http_post
        self._secret = signing_secret

    def _dispatch(self, job_input: CodingAgentJobInput) -> CodingAgentJob:
        body = json.dumps(
            job_input.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._secret:
            headers["X-Foundry-Signature"] = sign_payload(self._secret, body)
        response = self._http_post(self._url, body, headers) or {}
        job_id = str(response.get("job_id") or f"webhook:{job_input.run_id}")
        return CodingAgentJob(
            job_id=job_id, provider=self.name, status=AgentJobStatus.RUNNING
        )

    def get_job_status(self, job_id: str) -> CodingAgentJobStatus:
        # Observed out-of-band via the GitHub webhook, like the other
        # delegation-style providers.
        return CodingAgentJobStatus(
            job_id=job_id, provider=self.name, status=AgentJobStatus.RUNNING
        )

    def cancel_job(self, job_id: str) -> None:  # pragma: no cover - receiver's job
        return None
