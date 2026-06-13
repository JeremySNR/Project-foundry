"""Direct unit tests for the webhook auth + approval-command helpers.

`api/security.py` is the fail-closed boundary: a wrong, missing, or unsigned
webhook must never start a run, and approval commands must parse predictably.
These pin the function-level contracts that the API tests only exercise
end-to-end.
"""

from __future__ import annotations

from foundry.api.security import (
    ApprovalCommand,
    compute_signature,
    is_authorised_approver,
    parse_command,
    verify_signature,
)

BODY = b'{"hello":"world"}'
SECRET = "test-secret"


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
