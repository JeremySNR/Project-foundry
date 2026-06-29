"""Webhook authentication and approval-command helpers.

Foundry rejects unauthenticated webhooks (no workflow starts) and only lets
authorised users approve a run. Signature verification is constant-time.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
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


# --- Slack request signing ---------------------------------------------------

# Slack's recommended replay window: reject deliveries whose timestamp is more
# than five minutes from now (https://api.slack.com/authentication/verifying-requests-from-slack).
SLACK_MAX_AGE_SECONDS = 60 * 5


def compute_slack_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Slack v0 request signature for ``body`` (for tests/clients).

    Slack signs the basestring ``v0:{timestamp}:{raw_body}`` and sends the
    result as ``v0=<hex>`` in ``X-Slack-Signature``.
    """
    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return "v0=" + digest


def verify_slack_signature(
    secret: str,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    *,
    now: float | None = None,
    max_age_seconds: int = SLACK_MAX_AGE_SECONDS,
) -> bool:
    """Verify a Slack interactivity request: signature plus replay-age window.

    Fail-closed: returns False when no secret is configured (an empty secret
    cannot authenticate anything), when the timestamp/signature header is
    missing or non-numeric, or when the request is older than ``max_age_seconds``
    (Slack replay protection - a captured request cannot be re-sent forever).
    Comparison is constant-time.
    """
    if not secret or not timestamp or not signature:
        return False
    try:
        sent_at = float(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - sent_at) > max_age_seconds:
        return False
    expected = compute_slack_signature(secret, timestamp, body)
    return hmac.compare_digest(expected, signature)


# --- Microsoft Teams request signing -----------------------------------------

# Teams Outgoing Webhooks sign each delivery with an HMAC-SHA256 of the raw body
# under a shared secret, delivered as ``Authorization: HMAC <base64-signature>``.
# The secret Teams shows the operator at registration is itself base64, so it is
# decoded to raw bytes before keying the HMAC.
# https://learn.microsoft.com/microsoftteams/platform/webhooks-and-connectors/how-to/add-outgoing-webhook
TEAMS_AUTH_SCHEME = "HMAC"


def compute_teams_signature(secret: str, body: bytes) -> str:
    """Teams Outgoing Webhook signature for ``body`` (for tests/clients).

    The shared ``secret`` is base64 (as Teams issues it); it is decoded to raw
    key bytes, and the result is the base64-encoded HMAC-SHA256 of the raw body
    — the value Teams puts after ``HMAC `` in the ``Authorization`` header.
    """
    key = base64.b64decode(secret)
    digest = hmac.new(key, body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_teams_signature(
    secret: str, body: bytes, authorization: str | None
) -> bool:
    """Verify a Teams Outgoing Webhook ``Authorization: HMAC <sig>`` header.

    Fail-closed: returns False when no secret is configured (an empty secret
    cannot authenticate anything), when the header is missing or not the ``HMAC``
    scheme, or when the secret is not valid base64. Comparison is constant-time.
    """
    if not secret or not authorization:
        return False
    scheme, _, provided = authorization.partition(" ")
    if scheme != TEAMS_AUTH_SCHEME or not provided:
        return False
    try:
        expected = compute_teams_signature(secret, body)
    except (binascii.Error, ValueError):
        return False
    return hmac.compare_digest(expected, provided.strip())


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
