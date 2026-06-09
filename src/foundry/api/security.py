"""Webhook authentication and approval-command helpers.

Foundry rejects unauthenticated webhooks (no workflow starts) and only lets
authorised users approve a run. Signature verification is constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass


class WebhookAuthError(Exception):
    """Raised when a webhook fails signature verification."""


def compute_signature(secret: str, body: bytes) -> str:
    """HMAC-SHA256 hex digest of ``body`` under ``secret`` (for tests/clients)."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time comparison of the provided signature against the expected one.

    Accepts an optional ``sha256=`` prefix (GitHub-style) on the header.
    Returns False when no secret is configured - an empty secret cannot
    authenticate anything because an attacker can trivially forge it.
    """
    if not secret:
        return False
    if not signature:
        return False
    if signature.startswith("sha256="):
        signature = signature[len("sha256=") :]
    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected, signature)


# --- approval commands -------------------------------------------------------

# The approval surface for the MVP is Linear comments.
APPROVAL_COMMANDS = frozenset(
    {"approve", "reject", "revise", "start", "stop", "ask"}
)


@dataclass(frozen=True)
class ApprovalCommand:
    command: str
    argument: str | None = None


def parse_command(text: str) -> ApprovalCommand | None:
    """Parse a ``/foundry <command> [argument]`` comment.

    Returns ``None`` when the text is not a recognised Foundry command.
    """
    stripped = text.strip()
    if not stripped.startswith("/foundry"):
        return None
    parts = stripped.split(maxsplit=2)
    if len(parts) < 2:
        return None
    command = parts[1].lower()
    if command not in APPROVAL_COMMANDS:
        return None
    argument = parts[2] if len(parts) == 3 else None
    return ApprovalCommand(command=command, argument=argument)


def is_authorised_approver(user: str, authorised: set[str]) -> bool:
    return user in authorised
