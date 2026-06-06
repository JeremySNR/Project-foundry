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
        self._decision: str | None = None
        self._decision_user: str | None = None
        self._roles: list[str] = []
        self._pr_state: dict[str, Any] | None = None

    @workflow.run
    async def run(self, params: dict[str, Any]) -> dict[str, Any]:
        intake = await workflow.execute_activity_method(
            FoundryActivities.intake_and_plan, params, **_ACTIVITY_OPTS
        )
        self._run_id = intake["run_id"]
        self._status = intake["status"]

        phase = phase_after_intake(RunStatus(self._status))

        if phase is Phase.AWAIT_APPROVAL:
            await workflow.wait_condition(
                lambda: self._decision is not None, timeout=_APPROVAL_TIMEOUT
            )
            phase = await self._handle_decision()

        if phase is Phase.AWAIT_PR:
            await workflow.wait_condition(
                lambda: self._pr_state is not None, timeout=_PR_TIMEOUT
            )
            result = await workflow.execute_activity_method(
                FoundryActivities.record_pr,
                {"run_id": self._run_id, "pr_state": self._pr_state},
                **_ACTIVITY_OPTS,
            )
            self._status = result["status"]

        return {"run_id": self._run_id, "status": self._status}

    async def _handle_decision(self) -> Phase:
        decision = self._decision
        base = {"run_id": self._run_id, "user": self._decision_user}

        if decision == HumanDecision.APPROVE.value:
            await workflow.execute_activity_method(
                FoundryActivities.approve, {**base, "roles": self._roles}, **_ACTIVITY_OPTS
            )
            dispatched = await workflow.execute_activity_method(
                FoundryActivities.dispatch_agent, self._run_id, **_ACTIVITY_OPTS
            )
            self._status = dispatched["status"]
            return phase_after_dispatch(RunStatus(self._status))

        activity_method = (
            FoundryActivities.reject
            if decision == HumanDecision.REJECT.value
            else FoundryActivities.stop
        )
        result = await workflow.execute_activity_method(
            activity_method, base, **_ACTIVITY_OPTS
        )
        self._status = result["status"]
        return Phase.DONE

    @workflow.signal
    def submit_decision(
        self, decision: str, user: str, roles: list[str] | None = None
    ) -> None:
        self._decision = decision
        self._decision_user = user
        self._roles = roles or []

    @workflow.signal
    def pr_observed(self, pr_state: dict[str, Any]) -> None:
        self._pr_state = pr_state

    @workflow.query
    def current_status(self) -> str:
        return self._status
