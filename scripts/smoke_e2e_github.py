#!/usr/bin/env python3
"""Live end-to-end smoke test: GitHub issue -> approval -> agent -> PR.

The GitHub Issues twin of ``smoke_e2e.py`` (which is Linear-only), and the
dogfooding entry point: point it at a real issue in a real repository and it
drives the same production code paths the webhooks use - intake, analysis,
routing, risk, plan, the policy gate, an approval, and a governed dispatch -
printing the run's full audit timeline at the end.

Never runs in CI: it is gated on ``FOUNDRY_E2E=1`` plus live credentials, and
it mutates a real GitHub issue (comments, ``foundry:status:`` labels) and may
launch a real coding agent, depending on the configured provider.

Required environment:

    FOUNDRY_E2E=1                       explicit opt-in
    FOUNDRY_GITHUB_API_TOKEN            GitHub token (repo scope on the target)
    FOUNDRY_E2E_ISSUE_ID                issue as owner/repo#number
    FOUNDRY_E2E_APPROVER                GitHub login present in the approvers config
    FOUNDRY_CONFIG                      foundry.yaml with tracker.provider=github_issues

Optional:

    FOUNDRY_E2E_PR_TIMEOUT              seconds to wait for the agent's PR; 0 (the
                                        default) skips PR polling - in a deployed
                                        setup the GitHub webhook picks the PR up

Usage:

    FOUNDRY_E2E=1 FOUNDRY_CONFIG=foundry.dogfood.yaml \
    FOUNDRY_E2E_ISSUE_ID='owner/repo#123' FOUNDRY_E2E_APPROVER=your-login \
    python scripts/smoke_e2e_github.py
"""

from __future__ import annotations

import json
import os
import sys
import time

from foundry.api.app import build_orchestrator
from foundry.config import Settings
from foundry.connectors.github_issues import GitHubIssuesConnector, split_issue_id
from foundry.connectors.transport import github_transport
from foundry.db import create_all, make_engine, make_session_factory
from foundry.db.models import FoundryAuditEvent, FoundryPolicyDecision
from foundry.schemas.common import ApprovalRole, RunStatus
from foundry.schemas.pr import PullRequestState


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def _step(msg: str) -> None:
    print(f"\n==> {msg}")


def _print_timeline(session_factory, run_id: str) -> None:
    """The receipts: every audit event and policy decision, in order."""
    _step("Audit timeline")
    with session_factory() as session:
        events = (
            session.query(FoundryAuditEvent)
            .filter_by(run_id=run_id)
            .order_by(FoundryAuditEvent.sequence)
            .all()
        )
        for e in events:
            meta = json.loads(e.metadata_json) if e.metadata_json else {}
            detail = meta.get("reason") or meta.get("category") or ""
            print(
                f"    [{e.sequence:>2}] {e.event_type.value:<24} "
                f"{e.actor_type}:{e.actor_id}" + (f"  ({detail})" if detail else "")
            )
        decisions = (
            session.query(FoundryPolicyDecision)
            .filter_by(run_id=run_id)
            .order_by(FoundryPolicyDecision.created_at)
            .all()
        )
        for d in decisions:
            verdict = "ALLOW" if d.allowed else "DENY"
            print(f"    policy {d.policy_name}: {verdict} - {d.reason or ''}")


def main() -> int:
    if os.environ.get("FOUNDRY_E2E") != "1":
        print("FOUNDRY_E2E != 1; refusing to touch live services.")
        return 0

    for var in ("FOUNDRY_GITHUB_API_TOKEN", "FOUNDRY_E2E_ISSUE_ID", "FOUNDRY_E2E_APPROVER"):
        if not os.environ.get(var):
            _fail(f"{var} is required")

    settings = Settings.load(os.environ.get("FOUNDRY_CONFIG"), env=os.environ)
    if settings.tracker_provider != "github_issues":
        _fail("FOUNDRY_CONFIG must set tracker.provider=github_issues")
    issue_id = os.environ["FOUNDRY_E2E_ISSUE_ID"]
    approver = os.environ["FOUNDRY_E2E_APPROVER"]
    roles = {ApprovalRole(r) for r in settings.roles_for(approver)}
    if not roles:
        _fail(f"approver {approver!r} has no roles in the configured approvers map")
    repo, _ = split_issue_id(issue_id)  # validates the id shape up front

    engine = make_engine("sqlite+pysqlite:///foundry-e2e-github.db")
    create_all(engine)
    session_factory = make_session_factory(engine)
    orch = build_orchestrator(settings, session_factory)

    _step(f"Fetching GitHub issue {issue_id}")
    tracker = GitHubIssuesConnector(
        transport=github_transport(settings.github_api_token)
    )
    ticket = tracker.get_issue(issue_id)
    print(f"    {ticket.issue_key}: {ticket.title}")

    _step("Intake + analysis + plan (posts the analysis comment on the issue)")
    run_id = orch.intake_and_plan(ticket, trigger_type="e2e_smoke")
    run = orch.get_run(run_id)
    print(f"    run {run_id} -> {run.status.value}")
    if run.status is RunStatus.NEEDS_CLARIFICATION:
        _print_timeline(session_factory, run_id)
        _fail(
            "ticket needs clarification - add acceptance criteria (see the "
            "comment Foundry just posted), then re-run"
        )
    if run.status is not RunStatus.WAITING_APPROVAL:
        _print_timeline(session_factory, run_id)
        _fail(f"expected waiting_approval, got {run.status.value}")

    _step(f"Approving as {approver} (roles: {sorted(r.value for r in roles)})")
    orch.approve(run_id, user=approver, granted_roles=roles)

    _step(f"Dispatching coding agent via provider '{settings.agent_provider}'")
    job = orch.dispatch_agent(run_id)
    print(f"    job {job.job_id} ({job.provider})")

    timeout = int(os.environ.get("FOUNDRY_E2E_PR_TIMEOUT", "0"))
    if not timeout:
        _print_timeline(session_factory, run_id)
        print(
            "\nPASS (dispatched). In a deployed setup the GitHub webhook picks "
            f"the PR up from here. Run id: {run_id}"
        )
        return 0

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
                _print_timeline(session_factory, run_id)
                print(f"\nPASS. Run id: {run_id}; inspect /runs/{run_id}/timeline.")
                return 0
        time.sleep(15)
        print("    ... still waiting")

    _fail(f"no PR correlated to run {run_id} within {timeout}s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
