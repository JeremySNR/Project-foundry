"""Pure workflow-decision tests (no temporalio required)."""

from __future__ import annotations

from foundry.workflows.decisions import (
    HumanDecision,
    Phase,
    WaitPhase,
    is_terminal,
    keep_observing_pr,
    parse_decision,
    phase_after_dispatch,
    phase_after_intake,
)
from foundry.schemas.common import PR_OBSERVABLE_STATUSES, RunStatus


def test_phase_after_intake_waiting_approval() -> None:
    assert phase_after_intake(RunStatus.WAITING_APPROVAL) is Phase.AWAIT_APPROVAL


def test_phase_after_intake_needs_clarification_is_done() -> None:
    assert phase_after_intake(RunStatus.NEEDS_CLARIFICATION) is Phase.DONE
    assert phase_after_intake(RunStatus.BLOCKED) is Phase.DONE


def test_phase_after_dispatch_agent_running_awaits_pr() -> None:
    assert phase_after_dispatch(RunStatus.AGENT_RUNNING) is Phase.AWAIT_PR


def test_phase_after_dispatch_blocked_is_done() -> None:
    assert phase_after_dispatch(RunStatus.BLOCKED) is Phase.DONE


def test_terminal_states() -> None:
    assert is_terminal(RunStatus.PR_OPEN)
    assert is_terminal(RunStatus.REJECTED)
    assert is_terminal(RunStatus.REVIEW_REQUIRED)
    assert not is_terminal(RunStatus.WAITING_APPROVAL)
    assert not is_terminal(RunStatus.AGENT_RUNNING)


def test_parse_decision_recognises_known_verbs() -> None:
    assert parse_decision("approve") is HumanDecision.APPROVE
    assert parse_decision("reject") is HumanDecision.REJECT
    assert parse_decision("stop") is HumanDecision.STOP


def test_parse_decision_drops_unknown_verb() -> None:
    # An unrecognised verb returns None so the workflow can ignore it instead of
    # falling through to a silent stop (issue #15, problem 2).
    assert parse_decision("approveee") is None
    assert parse_decision("") is None
    assert parse_decision("STOP") is None  # case-sensitive, matches the enum


def test_keep_observing_pr_matches_observable_statuses() -> None:
    # Drift guard: the workflow's "keep waiting for PR events" set is exactly the
    # orchestrator's PR-observable set (the shared schemas/common.py constant).
    for status in RunStatus:
        assert keep_observing_pr(status) is (status in PR_OBSERVABLE_STATUSES)
    assert keep_observing_pr(RunStatus.AGENT_RUNNING)
    assert keep_observing_pr(RunStatus.PR_OPEN)
    assert keep_observing_pr(RunStatus.REVIEW_REQUIRED)
    assert not keep_observing_pr(RunStatus.COMPLETE)
    assert not keep_observing_pr(RunStatus.BLOCKED)


def test_wait_phase_values() -> None:
    assert WaitPhase.APPROVAL.value == "approval"
    assert WaitPhase.PR.value == "pr"
