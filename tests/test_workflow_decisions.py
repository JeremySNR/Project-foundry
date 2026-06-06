"""Pure workflow-decision tests (no temporalio required)."""

from __future__ import annotations

from foundry.workflows.decisions import (
    Phase,
    is_terminal,
    phase_after_dispatch,
    phase_after_intake,
)
from foundry.schemas.common import RunStatus


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
