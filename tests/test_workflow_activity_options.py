"""Explicit per-activity retry/timeout policy (issue #37).

The policy itself lives in the pure-stdlib ``activity_options`` module, so most of
these tests run offline with no ``temporalio`` installed. The two that assert the
wiring (every registered activity is mapped; ``workflow.py`` builds a matching
``RetryPolicy``) ``importorskip`` the ``[workflow]`` extra.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from foundry.workflows.activity_options import (
    ACTIVITY_OPTIONS,
    DEFAULT_OPTIONS,
    DETERMINISTIC_ERRORS,
    ActivityOptions,
    options_for,
)


def test_every_mapped_activity_has_sane_timeout_and_attempts() -> None:
    assert ACTIVITY_OPTIONS  # not empty
    for name, opts in ACTIVITY_OPTIONS.items():
        assert opts.start_to_close_timeout > timedelta(0), name
        assert opts.maximum_attempts >= 1, name
        assert opts.initial_retry_interval > timedelta(0), name
        assert opts.backoff_coefficient >= 1.0, name


def test_deterministic_errors_are_non_retryable_on_writing_activities() -> None:
    # Activities that can raise a deterministic error (a void approval, an
    # unknown wait phase, a malformed PR payload) must fail fast, not burn the
    # whole retry budget re-running identical inputs.
    for name in ("approve", "reject", "stop", "record_pr", "expire"):
        assert ACTIVITY_OPTIONS[name].non_retryable_error_types == DETERMINISTIC_ERRORS
    # Class names, so Temporal's name-based matching works (e.g. OrchestratorError).
    assert "OrchestratorError" in DETERMINISTIC_ERRORS
    assert "ValueError" in DETERMINISTIC_ERRORS
    assert "ValidationError" in DETERMINISTIC_ERRORS


def test_idempotent_intake_stays_broadly_retryable() -> None:
    # intake_and_plan re-attaches to an existing run under retry (issue #15); it
    # must NOT short-circuit on a deterministic class, or that recovery path
    # would never get its second attempt.
    intake = ACTIVITY_OPTIONS["intake_and_plan"]
    assert intake.non_retryable_error_types == ()
    # ...and it gets the most generous budget (heaviest step + idempotent).
    assert intake.maximum_attempts >= max(
        o.maximum_attempts for o in ACTIVITY_OPTIONS.values()
    )
    assert intake.start_to_close_timeout >= max(
        o.start_to_close_timeout for o in ACTIVITY_OPTIONS.values()
    )


def test_options_for_unknown_name_returns_conservative_default() -> None:
    opts = options_for("does_not_exist")
    assert opts is DEFAULT_OPTIONS
    assert opts.non_retryable_error_types == DETERMINISTIC_ERRORS
    assert opts.maximum_attempts >= 1


def test_options_for_known_name_returns_its_spec() -> None:
    assert options_for("expire") is ACTIVITY_OPTIONS["expire"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_to_close_timeout": timedelta(0), "maximum_attempts": 3},
        {"start_to_close_timeout": timedelta(minutes=1), "maximum_attempts": 0},
        {
            "start_to_close_timeout": timedelta(minutes=1),
            "maximum_attempts": 1,
            "initial_retry_interval": timedelta(0),
        },
        {
            "start_to_close_timeout": timedelta(minutes=1),
            "maximum_attempts": 1,
            "backoff_coefficient": 0.5,
        },
    ],
)
def test_invalid_options_rejected_at_construction(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        ActivityOptions(**kwargs)


def test_every_registered_activity_is_mapped() -> None:
    # Guards against adding an activity that silently inherits the default: the
    # registered set and the explicit policy map must match exactly.
    pytest.importorskip("temporalio")
    from foundry.workflows.activities import FoundryActivities

    # ``.all()`` only reads ``self`` to list its bound methods - no orchestrator
    # call - so any placeholder object is enough to introspect the names.
    registered = {m.__name__ for m in FoundryActivities(object()).all()}
    assert registered == set(ACTIVITY_OPTIONS)


def test_workflow_builds_retry_policy_from_spec() -> None:
    pytest.importorskip("temporalio")
    from foundry.workflows.activities import FoundryActivities
    from foundry.workflows.workflow import _opts

    kwargs = _opts(FoundryActivities.approve)
    spec = ACTIVITY_OPTIONS["approve"]
    assert kwargs["start_to_close_timeout"] == spec.start_to_close_timeout
    policy = kwargs["retry_policy"]
    assert policy.maximum_attempts == spec.maximum_attempts
    assert list(policy.non_retryable_error_types) == list(spec.non_retryable_error_types)

    # The idempotent intake path carries no non-retryable types through to Temporal.
    intake_policy = _opts(FoundryActivities.intake_and_plan)["retry_policy"]
    assert list(intake_policy.non_retryable_error_types) == []
