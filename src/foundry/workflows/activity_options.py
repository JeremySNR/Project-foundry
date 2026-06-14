"""Explicit per-activity retry/timeout policy for the durable workflow.

Each Temporal activity has a different cost and failure profile, so a single
blanket retry policy is the wrong default:

* A *deterministic* failure - an unknown wait phase (``ValueError``), a malformed
  PR payload (``ValidationError``), a void / failed-role approval
  (``OrchestratorError``) - re-runs the same inputs and fails the same way, so it
  should surface immediately instead of burning the whole retry budget.
* A transient DB / provider hiccup on an *idempotent* step (intake re-attaches to
  an existing run under retry, issue #15; ``record_pr`` re-checks on every push)
  is worth retrying patiently.
* The heaviest step (analyse + enrich + plan, possibly LLM-backed) needs a far
  longer ``start_to_close_timeout`` than a one-row state write.

These specs make that intent explicit and reviewable in one place. The module is
**pure stdlib (no ``temporalio`` import)** so the policy is unit-tested offline
even where the ``[workflow]`` extra is absent; ``workflow.py`` turns each spec
into Temporal's ``execute_activity`` kwargs (``start_to_close_timeout`` +
``RetryPolicy``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

# Deterministic exception classes: retrying re-runs identical inputs and fails
# identically, so Temporal should not retry them. Matched by class name, which is
# how ``RetryPolicy.non_retryable_error_types`` compares (the activity exception
# is surfaced with its type name in workflow history).
DETERMINISTIC_ERRORS: tuple[str, ...] = (
    "ValueError",
    "ValidationError",
    "OrchestratorError",
)


@dataclass(frozen=True)
class ActivityOptions:
    """A reviewable retry/timeout policy for a single activity."""

    start_to_close_timeout: timedelta
    maximum_attempts: int
    initial_retry_interval: timedelta = timedelta(seconds=1)
    backoff_coefficient: float = 2.0
    non_retryable_error_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.start_to_close_timeout <= timedelta(0):
            raise ValueError("start_to_close_timeout must be positive")
        if self.maximum_attempts < 1:
            raise ValueError("maximum_attempts must be >= 1")
        if self.initial_retry_interval <= timedelta(0):
            raise ValueError("initial_retry_interval must be positive")
        if self.backoff_coefficient < 1.0:
            raise ValueError("backoff_coefficient must be >= 1.0")


# Keyed by activity name (Temporal registers each activity under its function
# name; see ``FoundryActivities`` in ``activities.py``). Every registered activity
# MUST have an entry here - ``tests/test_workflow_activity_options.py`` asserts the
# two sets are equal so a new activity can't silently inherit a stale default.
ACTIVITY_OPTIONS: dict[str, ActivityOptions] = {
    # Idempotent under retry (re-attaches to an existing run, issue #15) and the
    # heaviest step (analyse + enrich + plan, possibly LLM-backed): a long
    # timeout and patient retries through transient outages. Deliberately keeps
    # the default (empty) non-retryable set so the idempotent recovery path is
    # never short-circuited.
    "intake_and_plan": ActivityOptions(
        start_to_close_timeout=timedelta(minutes=10),
        maximum_attempts=5,
    ),
    # Records a human approval; the workflow dispatches right after. A void /
    # failed-role approval raises ``OrchestratorError`` and is deterministic.
    "approve": ActivityOptions(
        start_to_close_timeout=timedelta(minutes=1),
        maximum_attempts=3,
        non_retryable_error_types=DETERMINISTIC_ERRORS,
    ),
    # Creates the agent job (provider HTTP). It already swallows a policy block
    # (``OrchestratorError``) and reports it cleanly, so only transient errors
    # reach the retry path; the orchestrator's locked terminal-state guard
    # (AGENTS.md invariant #7) makes a post-success retry a safe no-op rather
    # than a double-dispatch.
    "dispatch_agent": ActivityOptions(
        start_to_close_timeout=timedelta(minutes=5),
        maximum_attempts=3,
    ),
    "reject": ActivityOptions(
        start_to_close_timeout=timedelta(minutes=1),
        maximum_attempts=3,
        non_retryable_error_types=DETERMINISTIC_ERRORS,
    ),
    "stop": ActivityOptions(
        start_to_close_timeout=timedelta(minutes=1),
        maximum_attempts=3,
        non_retryable_error_types=DETERMINISTIC_ERRORS,
    ),
    # Idempotent re-check on every PR webhook; transient failures are worth
    # retrying, a malformed payload (``ValidationError``) is not.
    "record_pr": ActivityOptions(
        start_to_close_timeout=timedelta(minutes=2),
        maximum_attempts=5,
        non_retryable_error_types=DETERMINISTIC_ERRORS,
    ),
    # The compensating terminal transition for an elapsed durable wait. An
    # unknown phase raises ``ValueError`` (a programming error) - never retry it.
    "expire": ActivityOptions(
        start_to_close_timeout=timedelta(minutes=1),
        maximum_attempts=3,
        non_retryable_error_types=DETERMINISTIC_ERRORS,
    ),
}

# Conservative fallback for an unmapped activity name: a short timeout and
# fail-fast on deterministic errors. Reaching it means the equality test above
# failed in review, but the running workflow still degrades safely.
DEFAULT_OPTIONS = ActivityOptions(
    start_to_close_timeout=timedelta(minutes=5),
    maximum_attempts=3,
    non_retryable_error_types=DETERMINISTIC_ERRORS,
)


def options_for(activity_name: str) -> ActivityOptions:
    """The explicit options for ``activity_name`` (or the conservative default)."""
    return ACTIVITY_OPTIONS.get(activity_name, DEFAULT_OPTIONS)
