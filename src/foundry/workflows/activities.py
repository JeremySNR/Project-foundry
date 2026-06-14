"""Temporal activities - the side-effecting steps of a Foundry run.

Each activity is a thin wrapper over :class:`FoundryOrchestrator`. They are
*synchronous* (the orchestrator does blocking DB / HTTP work) and the worker runs
them in a thread-pool executor, which is Temporal's recommended pattern for
blocking activities. Keeping them thin means they can also be called directly in
tests with a real orchestrator - no Temporal server required.

Arguments and results are plain JSON-serialisable dicts so Temporal can persist
them in workflow history.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity

from foundry.orchestrator import FoundryOrchestrator, OrchestratorError
from foundry.schemas.common import ApprovalRole
from foundry.schemas.pr import PullRequestState
from foundry.schemas.ticket import RawTicket
from foundry.workflows.decisions import WaitPhase


class FoundryActivities:
    def __init__(self, orchestrator: FoundryOrchestrator) -> None:
        self._orch = orchestrator

    def _status(self, run_id: str) -> str:
        run = self._orch.get_run(run_id)
        return run.status.value if run else "unknown"

    @activity.defn
    def intake_and_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        ticket = RawTicket.model_validate(params["ticket"])
        try:
            run_id = self._orch.intake_and_plan(
                ticket,
                trigger_type=params.get("trigger_type", "unknown"),
                created_by=params.get("created_by"),
            )
        except OrchestratorError:
            # Idempotent under Temporal's at-least-once retries: a prior attempt
            # of *this* activity already created the run, then timed out before
            # returning. Re-running intake would raise "already has an active
            # run" and fail the workflow despite a healthy run existing - so
            # attach to that run instead of creating a duplicate.
            existing = self._orch.find_active_run_id_for_issue(ticket.issue_id)
            if existing is None:
                raise
            run_id = existing
        return {"run_id": run_id, "status": self._status(run_id)}

    @activity.defn
    def approve(self, params: dict[str, Any]) -> dict[str, Any]:
        roles = {ApprovalRole(r) for r in params.get("roles", [])}
        self._orch.approve(params["run_id"], user=params["user"], granted_roles=roles)
        return {"run_id": params["run_id"], "status": self._status(params["run_id"])}

    @activity.defn
    def dispatch_agent(self, run_id: str) -> dict[str, Any]:
        try:
            self._orch.dispatch_agent(run_id)
            return {"dispatched": True, "status": self._status(run_id)}
        except OrchestratorError as exc:
            # A policy block (e.g. human-only work) is an expected outcome, not a
            # workflow failure: report it so the run ends cleanly as blocked.
            return {"dispatched": False, "status": self._status(run_id), "detail": str(exc)}

    @activity.defn
    def reject(self, params: dict[str, Any]) -> dict[str, Any]:
        self._orch.reject(params["run_id"], user=params["user"])
        return {"run_id": params["run_id"], "status": self._status(params["run_id"])}

    @activity.defn
    def stop(self, params: dict[str, Any]) -> dict[str, Any]:
        self._orch.stop(params["run_id"], user=params["user"])
        return {"run_id": params["run_id"], "status": self._status(params["run_id"])}

    @activity.defn
    def record_pr(self, params: dict[str, Any]) -> dict[str, Any]:
        pr_state = PullRequestState.model_validate(params["pr_state"])
        status = self._orch.record_pr(params["run_id"], pr_state)
        return {"run_id": params["run_id"], "status": status.value}

    @activity.defn
    def expire(self, params: dict[str, Any]) -> dict[str, Any]:
        """Cleanly terminate a run whose durable wait window elapsed.

        ``phase`` is ``"approval"`` (no decision arrived) or ``"pr"`` (the agent
        produced no PR). Idempotent: a run that has since moved on is untouched.
        """
        phase = params["phase"]
        if phase == WaitPhase.APPROVAL.value:
            status = self._orch.expire_pending_approval(params["run_id"])
        elif phase == WaitPhase.PR.value:
            status = self._orch.expire_pending_pr(params["run_id"])
        else:
            raise ValueError(f"unknown wait phase '{phase}'")
        return {"run_id": params["run_id"], "status": status.value}

    def all(self) -> list:
        """Convenience: the bound activity callables to register with the worker."""
        return [
            self.intake_and_plan,
            self.approve,
            self.dispatch_agent,
            self.reject,
            self.stop,
            self.record_pr,
            self.expire,
        ]
