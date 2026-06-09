#!/usr/bin/env python3
"""Live end-to-end smoke test: Linear issue -> approval -> agent -> PR.

Drives a *real* run against real services, exercising the same code paths the
webhooks use. Never runs in CI: it is gated on ``FOUNDRY_E2E=1`` plus live
credentials, and it mutates a real Linear issue (comments, state changes) and
may launch a real coding agent.

Required environment:

    FOUNDRY_E2E=1                       explicit opt-in
    FOUNDRY_LINEAR_API_TOKEN            Linear API key
    FOUNDRY_E2E_ISSUE_ID                Linear issue UUID to run against
    FOUNDRY_E2E_APPROVER                email present in the approvers config
    FOUNDRY_CONFIG                      foundry.yaml with approvers + repos

Optional:

    FOUNDRY_AGENT_PROVIDER              defaults to cursor_via_linear
    FOUNDRY_GITHUB_API_TOKEN            enables PR polling/observation
    FOUNDRY_E2E_REPO                    owner/name to poll for the PR
    FOUNDRY_E2E_PR_TIMEOUT              seconds to wait for the PR (default 600)

Usage:

    FOUNDRY_E2E=1 python scripts/smoke_e2e.py
"""

from __future__ import annotations

import os
import sys
import time

from foundry.api.app import build_orchestrator
from foundry.config import Settings
from foundry.connectors.linear import LinearConnector
from foundry.connectors.transport import github_transport, linear_transport
from foundry.db import create_all, make_engine, make_session_factory
from foundry.schemas.common import ApprovalRole, RunStatus
from foundry.schemas.pr import PullRequestState


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def _step(msg: str) -> None:
    print(f"\n==> {msg}")


def main() -> int:
    if os.environ.get("FOUNDRY_E2E") != "1":
        print("FOUNDRY_E2E != 1; refusing to touch live services.")
        return 0

    for var in ("FOUNDRY_LINEAR_API_TOKEN", "FOUNDRY_E2E_ISSUE_ID", "FOUNDRY_E2E_APPROVER"):
        if not os.environ.get(var):
            _fail(f"{var} is required")

    settings = Settings.load(os.environ.get("FOUNDRY_CONFIG"), env=os.environ)
    issue_id = os.environ["FOUNDRY_E2E_ISSUE_ID"]
    approver = os.environ["FOUNDRY_E2E_APPROVER"]
    roles = {ApprovalRole(r) for r in settings.roles_for(approver)}
    if not roles:
        _fail(f"approver {approver!r} has no roles in the configured approvers map")

    engine = make_engine("sqlite+pysqlite:///foundry-e2e.db")
    create_all(engine)
    orch = build_orchestrator(settings, make_session_factory(engine))

    _step(f"Fetching Linear issue {issue_id}")
    linear = LinearConnector(transport=linear_transport(settings.linear_api_token))
    ticket = linear.get_issue(issue_id)
    print(f"    {ticket.issue_key}: {ticket.title}")

    _step("Intake + analysis + plan (posts the analysis comment on the issue)")
    run_id = orch.intake_and_plan(ticket, trigger_type="e2e_smoke")
    run = orch.get_run(run_id)
    print(f"    run {run_id} -> {run.status.value}")
    if run.status is RunStatus.NEEDS_CLARIFICATION:
        _fail(
            "ticket needs clarification - add acceptance criteria and a repo "
            "label (see the comment Foundry just posted), then re-run"
        )
    if run.status is not RunStatus.WAITING_APPROVAL:
        _fail(f"expected waiting_approval, got {run.status.value}")

    _step(f"Approving as {approver} (roles: {sorted(r.value for r in roles)})")
    orch.approve(run_id, user=approver, granted_roles=roles)

    _step(f"Dispatching coding agent via provider '{settings.agent_provider}'")
    job = orch.dispatch_agent(run_id)
    print(f"    job {job.job_id} ({job.provider})")

    repo = os.environ.get("FOUNDRY_E2E_REPO")
    if not (settings.github_api_token and repo):
        print(
            "\nNo FOUNDRY_GITHUB_API_TOKEN/FOUNDRY_E2E_REPO - stopping here. "
            "The PR will be picked up by the GitHub webhook in a deployed setup."
        )
        print(f"\nPASS (dispatched). Run id: {run_id}")
        return 0

    timeout = int(os.environ.get("FOUNDRY_E2E_PR_TIMEOUT", "600"))
    _step(f"Polling github.com/{repo} for the agent's PR (up to {timeout}s)")
    transport = github_transport(settings.github_api_token)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for pr in transport("GET", f"/repos/{repo}/pulls?state=open&per_page=50"):
            state = PullRequestState.model_validate(
                {
                    "pr_number": pr["number"],
                    "url": pr["html_url"],
                    "branch": pr["head"]["ref"],
                    "title": pr.get("title") or "",
                    "status": "open",
                    "files_changed": [],
                }
            )
            if orch.correlate_pr(state) == run_id:
                _step(f"PR observed: {state.url}")
                status = orch.record_pr(run_id, state)
                print(f"    run -> {status.value}")
                print(f"\nPASS. Run id: {run_id}; inspect /runs/{run_id}/timeline.")
                return 0
        time.sleep(15)
        print("    ... still waiting")

    _fail(f"no PR correlated to run {run_id} within {timeout}s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
