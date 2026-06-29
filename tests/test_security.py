"""Direct unit tests for the webhook auth + approval-command helpers.

`api/security.py` is the fail-closed boundary: a wrong, missing, or unsigned
webhook must never start a run, and approval commands must parse predictably.
These pin the function-level contracts that the API tests only exercise
end-to-end.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from foundry.api.security import (
    SLACK_MAX_AGE_SECONDS,
    TEAMS_MAX_AGE_SECONDS,
    ApprovalCommand,
    compute_signature,
    compute_slack_signature,
    compute_teams_signature,
    is_authorised_approver,
    parse_command,
    verify_signature,
    verify_slack_signature,
    verify_teams_signature,
    verify_teams_timestamp,
)

BODY = b'{"hello":"world"}'
SECRET = "test-secret"

# Teams issues the shared token as base64; the signature is keyed by its decoded
# bytes (see security.py).
TEAMS_SECRET = base64.b64encode(b"teams-shared-secret").decode("ascii")
TEAMS_BODY = b'{"type":"message","text":"approve issue-r"}'

SLACK_BODY = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
SLACK_TS = "1718370123"


# -- verify_signature ----------------------------------------------------------


def test_verify_signature_accepts_a_correct_signature() -> None:
    sig = compute_signature(SECRET, BODY)
    assert verify_signature(SECRET, BODY, sig) is True


def test_verify_signature_accepts_the_sha256_prefix() -> None:
    sig = compute_signature(SECRET, BODY)
    assert verify_signature(SECRET, BODY, f"sha256={sig}") is True


def test_verify_signature_rejects_a_wrong_signature() -> None:
    assert verify_signature(SECRET, BODY, "deadbeef") is False
    assert verify_signature(SECRET, BODY, compute_signature("other", BODY)) is False


def test_verify_signature_rejects_a_missing_signature() -> None:
    # A completely absent header (None) must fail closed, not raise.
    assert verify_signature(SECRET, BODY, None) is False
    assert verify_signature(SECRET, BODY, "") is False


def test_verify_signature_fails_closed_without_a_configured_secret() -> None:
    # An empty configured secret cannot authenticate anything: an attacker could
    # otherwise compute a valid signature for the empty key.
    sig = compute_signature("", BODY)
    assert verify_signature("", BODY, sig) is False
    assert verify_signature("", BODY, "sha256=" + sig) is False


def test_verify_signature_is_body_sensitive() -> None:
    sig = compute_signature(SECRET, BODY)
    assert verify_signature(SECRET, BODY + b"tampered", sig) is False


# -- verify_slack_signature ----------------------------------------------------


def _slack_sig(secret=SECRET, ts=SLACK_TS, body=SLACK_BODY):
    return compute_slack_signature(secret, ts, body)


def test_verify_slack_signature_accepts_a_correct_signature() -> None:
    sig = _slack_sig()
    # ``now`` pinned to the signed timestamp so the request is fresh.
    assert verify_slack_signature(
        SECRET, SLACK_BODY, SLACK_TS, sig, now=float(SLACK_TS)
    )


def test_verify_slack_signature_rejects_a_wrong_signature() -> None:
    assert not verify_slack_signature(
        SECRET, SLACK_BODY, SLACK_TS, "v0=deadbeef", now=float(SLACK_TS)
    )
    other = compute_slack_signature("other", SLACK_TS, SLACK_BODY)
    assert not verify_slack_signature(
        SECRET, SLACK_BODY, SLACK_TS, other, now=float(SLACK_TS)
    )


def test_verify_slack_signature_is_body_sensitive() -> None:
    sig = _slack_sig()
    assert not verify_slack_signature(
        SECRET, SLACK_BODY + b"x", SLACK_TS, sig, now=float(SLACK_TS)
    )


def test_verify_slack_signature_fails_closed_without_secret_or_headers() -> None:
    sig = _slack_sig()
    assert not verify_slack_signature("", SLACK_BODY, SLACK_TS, sig, now=float(SLACK_TS))
    assert not verify_slack_signature(SECRET, SLACK_BODY, None, sig, now=float(SLACK_TS))
    assert not verify_slack_signature(
        SECRET, SLACK_BODY, SLACK_TS, None, now=float(SLACK_TS)
    )


def test_verify_slack_signature_rejects_non_numeric_timestamp() -> None:
    sig = compute_slack_signature(SECRET, "not-a-number", SLACK_BODY)
    assert not verify_slack_signature(SECRET, SLACK_BODY, "not-a-number", sig)


def test_verify_slack_signature_rejects_a_stale_request() -> None:
    # A correctly-signed but old request must be refused (replay protection),
    # both for the past and a clock-skewed future.
    sig = _slack_sig()
    stale = float(SLACK_TS) + SLACK_MAX_AGE_SECONDS + 1
    assert not verify_slack_signature(SECRET, SLACK_BODY, SLACK_TS, sig, now=stale)
    future = float(SLACK_TS) - SLACK_MAX_AGE_SECONDS - 1
    assert not verify_slack_signature(SECRET, SLACK_BODY, SLACK_TS, sig, now=future)


# -- verify_teams_signature ----------------------------------------------------


def _teams_auth(secret=TEAMS_SECRET, body=TEAMS_BODY):
    return "HMAC " + compute_teams_signature(secret, body)


def test_verify_teams_signature_accepts_a_correct_signature() -> None:
    assert verify_teams_signature(TEAMS_SECRET, TEAMS_BODY, _teams_auth())


def test_verify_teams_signature_rejects_a_wrong_signature() -> None:
    assert not verify_teams_signature(TEAMS_SECRET, TEAMS_BODY, "HMAC deadbeef")
    other = base64.b64encode(b"other-secret").decode("ascii")
    assert not verify_teams_signature(
        TEAMS_SECRET, TEAMS_BODY, _teams_auth(secret=other)
    )


def test_verify_teams_signature_is_body_sensitive() -> None:
    assert not verify_teams_signature(
        TEAMS_SECRET, TEAMS_BODY + b" ", _teams_auth()
    )


def test_verify_teams_signature_requires_the_hmac_scheme() -> None:
    # A bare base64 signature without the ``HMAC `` scheme prefix is refused.
    bare = compute_teams_signature(TEAMS_SECRET, TEAMS_BODY)
    assert not verify_teams_signature(TEAMS_SECRET, TEAMS_BODY, bare)
    assert not verify_teams_signature(TEAMS_SECRET, TEAMS_BODY, "Bearer " + bare)


def test_verify_teams_signature_fails_closed_without_secret_or_header() -> None:
    assert not verify_teams_signature("", TEAMS_BODY, _teams_auth())
    assert not verify_teams_signature(TEAMS_SECRET, TEAMS_BODY, None)
    assert not verify_teams_signature(TEAMS_SECRET, TEAMS_BODY, "HMAC ")


def test_verify_teams_signature_rejects_a_non_base64_secret() -> None:
    # A misconfigured (non-base64) token must fail closed, not raise.
    assert not verify_teams_signature("not-base64!!!", TEAMS_BODY, _teams_auth())


def test_verify_teams_timestamp_accepts_a_fresh_activity() -> None:
    now = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    assert verify_teams_timestamp("2026-06-29T12:00:00.000Z", now=now)


def test_verify_teams_timestamp_rejects_a_stale_activity() -> None:
    now = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    stale = "2026-06-29T11:54:59.000Z"
    future = "2026-06-29T12:05:01.000Z"
    assert not verify_teams_timestamp(stale, now=now)
    assert not verify_teams_timestamp(future, now=now)
    assert not verify_teams_timestamp(
        "2026-06-29T11:54:59.000Z",
        now=now,
        max_age_seconds=TEAMS_MAX_AGE_SECONDS,
    )


def test_verify_teams_timestamp_fails_closed_without_an_iso_timestamp() -> None:
    now = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    assert not verify_teams_timestamp(None, now=now)
    assert not verify_teams_timestamp("not-a-date", now=now)
    assert not verify_teams_timestamp("2026-06-29T12:00:00", now=now)


# -- parse_command -------------------------------------------------------------


def test_parse_command_extracts_a_bare_command() -> None:
    assert parse_command("/foundry approve") == ApprovalCommand(command="approve")


def test_parse_command_extracts_a_command_with_argument() -> None:
    cmd = parse_command("/foundry ask which repo owns billing?")
    assert cmd == ApprovalCommand(command="ask", argument="which repo owns billing?")


def test_parse_command_is_case_insensitive_on_the_verb() -> None:
    assert parse_command("/foundry APPROVE") == ApprovalCommand(command="approve")


def test_parse_command_tolerates_surrounding_whitespace() -> None:
    assert parse_command("   /foundry reject  ") == ApprovalCommand(command="reject")


def test_parse_command_ignores_non_foundry_text() -> None:
    assert parse_command("approve this please") is None
    assert parse_command("looks good, /foundry approve") is None  # must lead


def test_parse_command_rejects_unknown_verbs() -> None:
    assert parse_command("/foundry frobnicate") is None


def test_parse_command_requires_a_verb() -> None:
    assert parse_command("/foundry") is None
    assert parse_command("/foundry   ") is None


# -- is_authorised_approver ----------------------------------------------------


def test_is_authorised_approver_checks_membership() -> None:
    approvers = {"lead@example.com", "pm@example.com"}
    assert is_authorised_approver("lead@example.com", approvers) is True
    assert is_authorised_approver("stranger@example.com", approvers) is False
    assert is_authorised_approver("lead@example.com", set()) is False
