"""In-memory run store for the API skeleton.

The foundation does not yet wire Temporal; this store provides just enough state
for the webhook/approval/run-status endpoints to be meaningful and testable. It
is intentionally swappable for a DB/Temporal-backed implementation later.

Two invariants it guarantees and the API relies on:

- **Idempotent intake**: a webhook delivery id is processed at most once, so a
  redelivered event never creates a second run.
- **Authorised approvals only**: approvals carry the acting user; the route
  layer checks authorisation before calling :meth:`apply_approval`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from foundry.schemas.common import RunStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunRecord(BaseModel):
    id: str
    linear_issue_id: str
    linear_issue_key: str
    status: RunStatus = RunStatus.ANALYSING
    trigger_type: str = "unknown"
    created_by: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class RunStore:
    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._runs_by_issue: dict[str, str] = {}
        self._processed_events: set[str] = set()

    # --- idempotency ---------------------------------------------------------
    def has_processed_event(self, event_id: str) -> bool:
        return event_id in self._processed_events

    def mark_event_processed(self, event_id: str) -> None:
        self._processed_events.add(event_id)

    # --- runs ----------------------------------------------------------------
    def create_run(self, record: RunRecord) -> RunRecord:
        self._runs[record.id] = record
        self._runs_by_issue[record.linear_issue_id] = record.id
        return record

    def get_run(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    def get_run_by_issue(self, linear_issue_id: str) -> RunRecord | None:
        run_id = self._runs_by_issue.get(linear_issue_id)
        return self._runs.get(run_id) if run_id else None

    def list_runs(self) -> list[RunRecord]:
        return list(self._runs.values())

    def apply_approval(
        self, run_id: str, *, command: str, user: str
    ) -> RunRecord | None:
        """Transition a run in response to an authorised approval command."""
        run = self._runs.get(run_id)
        if run is None:
            return None
        if command == "approve":
            run.status = RunStatus.APPROVED
            run.approved_by = user
            run.approved_at = _utcnow()
        elif command == "reject":
            run.status = RunStatus.REJECTED
        elif command == "revise":
            # Loop back to planning for a fresh delivery plan.
            run.status = RunStatus.PLAN_READY
        elif command == "stop":
            run.status = RunStatus.BLOCKED
        run.updated_at = _utcnow()
        return run
