"""Pure workflow sequencing logic.

The Temporal workflow is a thin shell; the "what happens next" decisions live here
as pure functions so they are unit-testable without temporalio or a server. The
workflow imports these and only adds durability (signals, waits, retries) around
them.
"""

from __future__ import annotations

from enum import Enum

from foundry.schemas.common import RunStatus


class Phase(str, Enum):
    """What the workflow should do next."""

    AWAIT_APPROVAL = "await_approval"
    AWAIT_PR = "await_pr"
    DONE = "done"


class HumanDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    STOP = "stop"


# Run states from which the workflow has nothing left to drive.
_TERMINAL = frozenset(
    {
        RunStatus.NEEDS_CLARIFICATION,
        RunStatus.BLOCKED,
        RunStatus.REJECTED,
        RunStatus.EXECUTION_FAILED,
        RunStatus.PR_OPEN,
        RunStatus.REVIEW_REQUIRED,
        RunStatus.COMPLETE,
    }
)


def is_terminal(status: RunStatus) -> bool:
    return status in _TERMINAL


def phase_after_intake(status: RunStatus) -> Phase:
    """After analyse+plan: wait for approval only if the plan is approvable."""
    if status is RunStatus.WAITING_APPROVAL:
        return Phase.AWAIT_APPROVAL
    return Phase.DONE


def phase_after_dispatch(status: RunStatus) -> Phase:
    """After approve+dispatch: wait for the PR only if an agent actually started."""
    if status is RunStatus.AGENT_RUNNING:
        return Phase.AWAIT_PR
    return Phase.DONE
