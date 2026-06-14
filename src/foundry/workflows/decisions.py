"""Pure workflow sequencing logic.

The Temporal workflow is a thin shell; the "what happens next" decisions live here
as pure functions so they are unit-testable without temporalio or a server. The
workflow imports these and only adds durability (signals, waits, retries) around
them.
"""

from __future__ import annotations

from enum import Enum

from foundry.schemas.common import PR_OBSERVABLE_STATUSES, RunStatus


class Phase(str, Enum):
    """What the workflow should do next."""

    AWAIT_APPROVAL = "await_approval"
    AWAIT_PR = "await_pr"
    DONE = "done"


class HumanDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    STOP = "stop"


class WaitPhase(str, Enum):
    """Which durable wait elapsed, for the expiry activity to terminate cleanly."""

    APPROVAL = "approval"
    PR = "pr"


def parse_decision(decision: str) -> HumanDecision | None:
    """Coerce a raw decision verb to a :class:`HumanDecision`, or ``None``.

    The InlineDriver raises ``ValueError`` on an unrecognised verb
    (``drivers.py``); the durable workflow instead validates the *signal* and
    drops anything it can't recognise, so a typo'd or malicious decision can
    never silently terminate the run (it previously fell through to ``stop``).
    The run simply keeps waiting for a valid decision.
    """
    try:
        return HumanDecision(decision)
    except ValueError:
        return None


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


def keep_observing_pr(status: RunStatus) -> bool:
    """Whether the workflow should keep waiting for further PR webhook events.

    Mirrors ``record_pr``'s own guard: while the run is in a PR-observable state
    (agent running, PR open, review requested) more pushes/CI/review events are
    meaningful and the diff-aware guardrails re-run on each. Once the run leaves
    that set (merged -> complete, closed/forbidden -> blocked) there is nothing
    left to observe and the loop ends.
    """
    return status in PR_OBSERVABLE_STATUSES
