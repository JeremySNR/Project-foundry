"""Run drivers - the single seam for *how* a run is executed.

The API (and any other entrypoint) drives runs through a :class:`RunDriver`
rather than calling the orchestrator directly. This gives one place that owns the
"approve => approve + dispatch, reject => reject" semantics, and a clean swap
point between two execution strategies:

- :class:`InlineDriver` - run the steps synchronously in-process (the default;
  fully tested here).
- A future ``TemporalDriver`` - start/signal the durable
  :class:`~foundry.workflows.workflow.TicketToPrWorkflow`. It implements this same
  interface; because driving a live workflow needs a running Temporal server, it
  is not shipped here rather than shipped untested. The seam below is where it
  attaches: ``start`` becomes ``client.start_workflow`` and ``submit_decision`` /
  ``observe_pr`` become ``handle.signal`` calls.
"""

from __future__ import annotations

from typing import Protocol

from foundry.orchestrator import FoundryOrchestrator, OrchestratorError
from foundry.schemas.common import ApprovalRole
from foundry.schemas.pr import PullRequestState
from foundry.schemas.ticket import RawTicket

# Human decision verbs (match the /foundry <command> approval commands).
APPROVE = "approve"
REJECT = "reject"
STOP = "stop"


class RunDriver(Protocol):
    def start(
        self, ticket: RawTicket, *, trigger_type: str, created_by: str | None = None
    ) -> str: ...

    def submit_decision(
        self,
        run_id: str,
        *,
        decision: str,
        user: str,
        roles: set[ApprovalRole] | None = None,
    ) -> None: ...

    def observe_pr(self, run_id: str, pr_state: PullRequestState) -> None: ...


class InlineDriver:
    """Execute run steps synchronously via the orchestrator, in-process."""

    def __init__(self, orchestrator: FoundryOrchestrator) -> None:
        self._orch = orchestrator

    def start(
        self, ticket: RawTicket, *, trigger_type: str, created_by: str | None = None
    ) -> str:
        return self._orch.intake_and_plan(
            ticket, trigger_type=trigger_type, created_by=created_by
        )

    def submit_decision(
        self,
        run_id: str,
        *,
        decision: str,
        user: str,
        roles: set[ApprovalRole] | None = None,
    ) -> None:
        if decision == APPROVE:
            self._orch.approve(run_id, user=user, granted_roles=roles or set())
            try:
                self._orch.dispatch_agent(run_id)
            except OrchestratorError:
                # A policy block (e.g. human-only work) already set the run to
                # blocked; that is the outcome, not an error to surface here.
                pass
        elif decision == REJECT:
            self._orch.reject(run_id, user=user)
        elif decision == STOP:
            self._orch.stop(run_id, user=user)
        else:
            raise ValueError(f"unsupported decision '{decision}'")

    def observe_pr(self, run_id: str, pr_state: PullRequestState) -> None:
        self._orch.record_pr(run_id, pr_state)
