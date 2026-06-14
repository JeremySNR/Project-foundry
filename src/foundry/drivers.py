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
    """Execute run steps synchronously via the orchestrator, in-process.

    ``auto_decompose_epics`` (issue #35) routes intake through
    :meth:`FoundryOrchestrator.intake_epic` so a ticket spanning several repos is
    automatically split into one independently-gated child run per repo. It is
    opt-in (default off) and behaviour-only: the deterministic producer is
    conservative, but auto-fanning one ticket into several governed runs is a
    change an operator turns on. With it off, ``start`` behaves exactly as
    before (a single ``intake_and_plan`` run).
    """

    def __init__(
        self,
        orchestrator: FoundryOrchestrator,
        *,
        auto_decompose_epics: bool = False,
    ) -> None:
        self._orch = orchestrator
        self._auto_decompose_epics = auto_decompose_epics

    def start(
        self, ticket: RawTicket, *, trigger_type: str, created_by: str | None = None
    ) -> str:
        if self._auto_decompose_epics:
            # An epic fans out into independently-gated child runs; a ticket that
            # does not decompose degrades to a single ordinary run. Either way
            # the returned id is the run for the ticket itself (the epic root),
            # so the caller's one-active-run-per-issue bookkeeping is unchanged.
            return self._orch.intake_epic(
                ticket, trigger_type=trigger_type, created_by=created_by
            ).parent_run_id
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
