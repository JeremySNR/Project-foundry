"""Epic rollup: derive an epic's status from its child runs' statuses.

An *epic* is a parent run that decomposes into one or more child runs (one per
repo / scope). This module is the **read** side of the parent/child run model
introduced in issue #35: a pure function over child :class:`RunStatus` values,
so it unit-tests offline with no DB. The producer that splits an epic ticket
into independently-gated child plans is a separate slice; here we only
summarise children that already exist.

The rollup deliberately mirrors the lifecycle sets in ``schemas.common`` rather
than hardcoding statuses, so it can never drift from the definition of "in
flight" vs "finished": a child is *active* if its status is in
``ACTIVE_RUN_STATUSES``, *complete* on the single success terminal
(``RunStatus.COMPLETE``), and *unsuccessful* on every other terminal status
(blocked / failed / rejected / needs-clarification).
"""

from __future__ import annotations

from collections import Counter
from enum import Enum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from foundry.engines.decomposition import EpicDecomposition
from foundry.schemas.common import ACTIVE_RUN_STATUSES, RunStatus


class EpicIntakeResult(BaseModel):
    """Outcome of :meth:`FoundryOrchestrator.intake_epic` (issue #35).

    Whether or not the ticket decomposed, ``parent_run_id`` is the run for the
    epic ticket itself. For a real epic, ``child_run_ids`` holds the
    independently-gated child runs (oldest first); for a ticket that did not
    decompose it is empty and the run is an ordinary single-repo run.
    """

    model_config = ConfigDict(extra="forbid")

    parent_run_id: str
    child_run_ids: list[str] = Field(default_factory=list)
    decomposition: EpicDecomposition

    @property
    def is_epic(self) -> bool:
        return self.decomposition.is_epic


class EpicStatus(str, Enum):
    """Rolled-up status of an epic, derived from its children.

    String values are stable for JSON / golden tests, mirroring the other
    enums in ``schemas.common``.
    """

    # No children linked yet (a freshly-created epic, or a run that is not one).
    EMPTY = "empty"
    # At least one child is still in flight - the epic is not yet decided.
    IN_PROGRESS = "in_progress"
    # Every child finished and every one of them merged.
    COMPLETE = "complete"
    # No child still in flight; at least one merged and at least one did not.
    PARTIAL = "partial"
    # No child still in flight and none merged - the epic delivered nothing.
    FAILED = "failed"


def _bucket(status: RunStatus) -> str:
    if status in ACTIVE_RUN_STATUSES:
        return "active"
    if status is RunStatus.COMPLETE:
        return "complete"
    return "unsuccessful"


def compute_epic_rollup(child_statuses: Iterable[RunStatus]) -> dict:
    """Summarise an epic from its children's run statuses.

    Returns a JSON-serialisable dict:

    - ``status``: the :class:`EpicStatus` value (string).
    - ``total``: number of child runs.
    - ``counts``: ``{"active": int, "complete": int, "unsuccessful": int}`` -
      the three rollup buckets.
    - ``status_breakdown``: ``{RunStatus.value: count}`` for the curious /
      dashboard, only including statuses actually present.

    Pure and order-independent; safe to call with an empty iterable.
    """
    statuses = list(child_statuses)
    counts = {"active": 0, "complete": 0, "unsuccessful": 0}
    breakdown: Counter[str] = Counter()
    for status in statuses:
        counts[_bucket(status)] += 1
        breakdown[status.value] += 1

    return {
        "status": _rollup_status(len(statuses), counts).value,
        "total": len(statuses),
        "counts": counts,
        "status_breakdown": dict(breakdown),
    }


def _rollup_status(total: int, counts: dict[str, int]) -> EpicStatus:
    if total == 0:
        return EpicStatus.EMPTY
    if counts["active"] > 0:
        return EpicStatus.IN_PROGRESS
    # All children are terminal from here.
    if counts["complete"] == total:
        return EpicStatus.COMPLETE
    if counts["complete"] == 0:
        return EpicStatus.FAILED
    return EpicStatus.PARTIAL
