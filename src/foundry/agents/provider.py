"""The ``CodingAgentProvider`` abstraction.

Foundry must not be built around one coding tool. Every backend (Cursor Cloud
Agent, Claude Code, OpenAI agent, or a human picking up the plan) implements the
same interface and accepts the same :class:`CodingAgentJobInput`.

A small guard (:func:`assert_no_secrets`) enforces the security rule that
providers must never receive secrets in their prompt/instructions.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from foundry.schemas.agent import (
    CodingAgentJob,
    CodingAgentJobInput,
    CodingAgentJobStatus,
)


class SecretLeakError(ValueError):
    """Raised when a job input appears to contain a secret."""


# Heuristic patterns for obviously-secret material. This is a safety net, not a
# substitute for never putting secrets in plans in the first place. Patterns are
# kept specific (provider prefixes, anchors, min lengths) so they fire on real
# credentials without tripping on ordinary branch slugs, repo names, or URLs.
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # GitHub tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack tokens
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),  # Google API key
    re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b"),  # Stripe secret/restricted keys
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}\b"),  # OpenAI API keys
    # JWT: three base64url segments. The header always begins with ``eyJ``.
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
    # Basic-auth credentials embedded in a URL (https://user:pass@host).
    re.compile(r"https?://[^/\s:@\"]+:[^/\s:@\"]+@"),
    # Labelled secrets (covers AWS secret access keys, bearer tokens, etc. when
    # they travel next to a giveaway keyword such as ``authorization``).
    re.compile(
        r"(?i)\b(?:authorization|api[_-]?key|secret|password|token)\b\s*[:=]\s*\S{8,}"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),  # bearer token
)


def assert_no_secrets(job_input: CodingAgentJobInput) -> None:
    """Raise :class:`SecretLeakError` if the job input looks like it leaks a secret.

    The whole serialized job input is scanned — not just ``agent_instructions``
    and ``delivery_plan`` — because providers forward other fields verbatim:
    ``WebhookProvider`` posts the entire payload and ``ClaudeCodeProvider``
    forwards ``ticket_url`` and the ``constraints`` lists. A secret hiding in a
    ticket URL query string or a constraints entry must not slip through.
    """
    haystack = job_input.model_dump_json()
    for pattern in _SECRET_PATTERNS:
        if pattern.search(haystack):
            raise SecretLeakError(
                "coding-agent job input appears to contain a secret; refusing to dispatch"
            )


class CodingAgentProvider(ABC):
    """Provider-agnostic contract for launching and tracking a coding job."""

    #: Stable identifier persisted on ``foundry_agent_jobs.provider``.
    name: str = "base"

    def create_job(self, job_input: CodingAgentJobInput) -> CodingAgentJob:
        """Validate, guard, then dispatch the job.

        Subclasses implement :meth:`_dispatch`; the secret guard runs first for
        every provider so the rule cannot be bypassed per-backend.
        """
        assert_no_secrets(job_input)
        return self._dispatch(job_input)

    @abstractmethod
    def _dispatch(self, job_input: CodingAgentJobInput) -> CodingAgentJob: ...

    @abstractmethod
    def get_job_status(self, job_id: str) -> CodingAgentJobStatus: ...

    @abstractmethod
    def cancel_job(self, job_id: str) -> None: ...
