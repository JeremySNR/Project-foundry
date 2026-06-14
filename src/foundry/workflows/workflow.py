"""The durable Ticket-to-PR workflow.

Survives worker restarts, retries failed activities, and - crucially - can wait
days for a human approval or for the agent's PR without holding any resources.
The business decisions are delegated to ``decisions.py``; this file only adds the
Temporal machinery (signals, waits, retries).

Signals:
- ``submit_decision(decision, user, roles)`` - human approve / reject / stop.
- ``pr_observed(pr_state)`` - a PR was detected for this run (from the GitHub
  webhook).

Query:
- ``current_status()`` - the latest known run status.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from foundry.schemas.common import RunStatus
    from foundry.workflows.activities import FoundryActivities
    from foundry.workflows.decisions import (
        HumanDecision,
        Phase,
        WaitPhase,
        keep_observing_pr,
        parse_decision,
        phase_after_dispatch,
        phase_after_intake,
    )

# How long the workflow will patiently wait for each human/agent step.
_APPROVAL_TIMEOUT = timedelta(days=7)
_PR_TIMEOUT = timedelta(days=3)

_ACTIVITY_OPTS: dict[str, Any] = {
    "start_to_close_timeout": timedelta(minutes=5),
    "retry_policy": RetryPolicy(maximum_attempts=3),
}


@workflow.defn
class TicketToPrWorkflow:
    def __init__(self) -> None:
        self._run_id: str | None = None
        self._status: str = RunStatus.ANALYSING.value
        self._decision: HumanDecision | None = None
        self._decision_user: str | None = None
        self._roles: list[str] = []
        # PR events accumulate so every push/CI/review is processed, not just
        # the first; ``_pr_processed`` is how many the workflow has consumed.
        self._pr_events: list[dict[str, Any]] = []
        self._pr_processed: int = 0

    @workflow.run
    async def run(self, params: dict[str, Any]) -> dict[str, Any]:
        intake = await workflow.execute_activity_method(
            FoundryActivities.intake_and_plan, params, **_ACTIVITY_OPTS
        )
        self._run_id = intake["run_id"]
        self._status = intake["status"]

        phase = phase_after_intake(RunStatus(self._status))

        if phase is Phase.AWAIT_APPROVAL:
            try:
                await workflow.wait_condition(
                    lambda: self._decision is not None, timeout=_APPROVAL_TIMEOUT
                )
            except asyncio.TimeoutError:
                # The approval window closed with no decision: terminate cleanly
                # (audited) instead of failing the workflow and stranding the run.
                await self._expire(WaitPhase.APPROVAL)
                return self._result()
            phase = await self._handle_decision()

        if phase is Phase.AWAIT_PR:
            await self._observe_pr()

        return self._result()

    async def _handle_decision(self) -> Phase:
        decision = self._decision
        base = {"run_id": self._run_id, "user": self._decision_user}

        if decision is HumanDecision.APPROVE:
            await workflow.execute_activity_method(
                FoundryActivities.approve, {**base, "roles": self._roles}, **_ACTIVITY_OPTS
            )
            dispatched = await workflow.execute_activity_method(
                FoundryActivities.dispatch_agent, self._run_id, **_ACTIVITY_OPTS
            )
            self._status = dispatched["status"]
            return phase_after_dispatch(RunStatus(self._status))

        # Only APPROVE/REJECT/STOP ever reach here - the signal handler drops
        # anything else - so an unknown verb can no longer silently ``stop`` the
        # run (the previous ``reject if REJECT else stop`` fall-through).
        activity_method = (
            FoundryActivities.reject
            if decision is HumanDecision.REJECT
            else FoundryActivities.stop
        )
        result = await workflow.execute_activity_method(
            activity_method, base, **_ACTIVITY_OPTS
        )
        self._status = result["status"]
        return Phase.DONE

    async def _observe_pr(self) -> None:
        """Process every PR webhook for the run until it leaves an observable
        state - matching the inline driver's re-check-on-every-push loop, not a
        single shot. The agent's first PR is bounded by ``_PR_TIMEOUT``; once a
        PR exists, a quiet period simply ends observation at that (delivered)
        state, while merge/close/forbidden ends it on the resulting status.
        """
        while True:
            try:
                await workflow.wait_condition(
                    lambda: self._pr_processed < len(self._pr_events),
                    timeout=_PR_TIMEOUT,
                )
            except asyncio.TimeoutError:
                if self._pr_processed == 0:
                    # No PR ever arrived: the agent failed to deliver. Mark the
                    # run failed (audited) rather than failing the workflow.
                    await self._expire(WaitPhase.PR)
                # A PR exists but has gone quiet: it is the deliverable; stop
                # observing and keep the current status.
                return
            while self._pr_processed < len(self._pr_events):
                if not keep_observing_pr(RunStatus(self._status)):
                    # The run reached a non-observable state mid-batch; drop the
                    # rest so we never call record_pr on a terminal run.
                    self._pr_processed = len(self._pr_events)
                    break
                pr_state = self._pr_events[self._pr_processed]
                self._pr_processed += 1
                result = await workflow.execute_activity_method(
                    FoundryActivities.record_pr,
                    {"run_id": self._run_id, "pr_state": pr_state},
                    **_ACTIVITY_OPTS,
                )
                self._status = result["status"]
            if not keep_observing_pr(RunStatus(self._status)):
                return

    async def _expire(self, phase: WaitPhase) -> None:
        result = await workflow.execute_activity_method(
            FoundryActivities.expire,
            {"run_id": self._run_id, "phase": phase.value},
            **_ACTIVITY_OPTS,
        )
        self._status = result["status"]

    def _result(self) -> dict[str, Any]:
        return {"run_id": self._run_id, "status": self._status}

    @workflow.signal
    def submit_decision(
        self, decision: str, user: str, roles: list[str] | None = None
    ) -> None:
        parsed = parse_decision(decision)
        if parsed is None:
            # Drop an unrecognised verb rather than letting it terminate the run;
            # the run keeps waiting for a valid decision (or the approval window).
            workflow.logger.warning("ignoring unknown decision verb %r", decision)
            return
        self._decision = parsed
        self._decision_user = user
        self._roles = roles or []

    @workflow.signal
    def pr_observed(self, pr_state: dict[str, Any]) -> None:
        self._pr_events.append(pr_state)

    @workflow.query
    def current_status(self) -> str:
        return self._status
