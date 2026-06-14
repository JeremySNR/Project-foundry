#!/usr/bin/env python3
"""The Foundry demo: the whole governed loop, offline, in about a minute.

No credentials, no network, no Docker - an in-memory database, the fake
coding-agent provider and an in-memory issue tracker. Every stage you see is
the real production code path (the same orchestrator, policy engine and audit
trail the webhooks drive); only the external services are stand-ins.

    python scripts/demo.py          # run it
    python scripts/demo.py --slow   # dramatic pacing for screen recording
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Run straight from a clone: put the repo's src/ on the path so `import foundry`
# resolves whether or not `pip install -e .` has been run. The third-party deps
# (pydantic, sqlalchemy, pyyaml) still need installing - if anything is missing
# we print a cross-platform install hint instead of a bare ModuleNotFoundError.
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

try:
    from foundry.agents.manual import InMemoryFakeProvider
    from foundry.connectors import InMemoryIssueTracker
    from foundry.db import (
        FoundryAuditEvent,
        FoundryPolicyDecision,
        create_all,
        make_engine,
        make_session_factory,
    )
    from foundry.orchestrator import FoundryOrchestrator
    from foundry.schemas.common import CIStatus, PRStatus, ReviewStatus, RunStatus
    from foundry.schemas.pr import PullRequestState
    from foundry.schemas.ticket import RawTicket
except ModuleNotFoundError as exc:
    sys.exit(
        f"\nThe demo needs Foundry's dependencies installed (missing: {exc.name}).\n\n"
        "From the repo root, create a virtualenv and install the package:\n\n"
        "  python -m venv .venv\n"
        "  .venv\\Scripts\\activate          # Windows\n"
        "  source .venv/bin/activate        # macOS / Linux\n"
        "  pip install -e .\n\n"
        "then re-run:  python scripts/demo.py\n"
    )

# -- terminal dressing ---------------------------------------------------------

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, AMBER, BLUE, PURPLE = (
    "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[35m"
)
_DELAY = 0.0

STATUS_COLOUR = {
    RunStatus.NEEDS_CLARIFICATION: AMBER,
    RunStatus.WAITING_APPROVAL: AMBER,
    RunStatus.AGENT_RUNNING: PURPLE,
    RunStatus.PR_OPEN: BLUE,
    RunStatus.REVIEW_REQUIRED: AMBER,
    RunStatus.BLOCKED: RED,
    RunStatus.COMPLETE: GREEN,
}


def say(text: str = "", indent: int = 0) -> None:
    print(" " * indent + text)
    if _DELAY:
        time.sleep(_DELAY)


def act(title: str) -> None:
    say()
    say(f"{BOLD}{'=' * 74}{RESET}")
    say(f"{BOLD}  {title}{RESET}")
    say(f"{BOLD}{'=' * 74}{RESET}")


def show_status(label: str, status: RunStatus) -> None:
    colour = STATUS_COLOUR.get(status, "")
    say(f"{label}: {colour}{BOLD}{status.value}{RESET}")


def show_comment(tracker: InMemoryIssueTracker, issue_id: str) -> None:
    body = tracker.comments[issue_id][-1]
    say(f"{DIM}--- comment posted to the ticket " + "-" * 40 + RESET)
    for line in body.splitlines():
        say(f"{DIM}| {line}{RESET}")
    say(f"{DIM}{'-' * 73}{RESET}")


def show_last_decision(sf, run_id: str) -> None:
    with sf() as session:
        decision = (
            session.query(FoundryPolicyDecision)
            .filter_by(run_id=run_id)
            .order_by(FoundryPolicyDecision.created_at.desc())
            .first()
        )
    if decision is None:
        return
    verdict = f"{GREEN}ALLOWED{RESET}" if decision.allowed else f"{RED}DENIED{RESET}"
    action = json.loads(decision.input_json).get("action", decision.policy_name)
    say(f"policy gate [{action}] -> {verdict}")
    colour = DIM if decision.allowed else RED
    for reason in json.loads(decision.decision_json).get("reasons", []):
        say(f"  {colour}- {reason}{RESET}")


# -- the script ----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slow", action="store_true", help="pause between lines")
    args = parser.parse_args()
    global _DELAY
    _DELAY = 0.35 if args.slow else 0.0

    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    provider = InMemoryFakeProvider()
    tracker = InMemoryIssueTracker()
    orch = FoundryOrchestrator(
        sf, provider=provider, issue_tracker=tracker, max_agent_retries=2
    )

    say(f"{BOLD}Project Foundry{RESET} - raw tickets in, reviewed PRs out.")
    say(f"{DIM}(everything below is the real code path; only the external "
        f"services are fakes){RESET}")

    # -- Act 1: a thin ticket gets bounced, helpfully --------------------------
    act("Act 1 - A vague ticket does not get built")
    say('PM files: "Add customer favourites" with no acceptance criteria.')
    thin = RawTicket(
        issue_id="demo-1",
        issue_key="LIN-101",
        title="Add customer favourites",
        description="Customers want to favourite items.",
    )
    run_1 = orch.intake_and_plan(thin, trigger_type="label")
    show_status("run", orch.get_run(run_1).status)
    say("Foundry does not reject it - it drafts the acceptance criteria for you:")
    show_comment(tracker, "demo-1")

    # -- Act 2: the improved ticket reaches the human gate ---------------------
    act("Act 2 - The improved ticket gets analysed, planned and gated")
    say("The PM pastes the criteria in and adds the repo label. Re-trigger:")
    ready = RawTicket(
        issue_id="demo-1",
        issue_key="LIN-101",
        title="Add customer favourites",
        description=(
            "Customers want to favourite items.\n\n"
            "Acceptance Criteria:\n"
            "- A favourites button exists on every item card\n"
            "- Favourites persist across sessions\n"
        ),
        known_repositories=["customer-web"],
    )
    run_id = orch.intake_and_plan(ready, trigger_type="label")
    run = orch.get_run(run_id)
    show_status("run", run.status)
    say(f"risk: {BOLD}{run.risk_level.value}{RESET}  "
        f"agent mode: {BOLD}{run.agent_mode.value}{RESET}")
    say("Nothing dispatches without a human. The plan is on the ticket:")
    show_comment(tracker, "demo-1")

    # -- Act 3: approval + governed dispatch -----------------------------------
    act("Act 3 - A human approves; the policy gate decides; the agent runs")
    say('lead@example.com comments "/foundry approve" ...')
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    show_last_decision(sf, run_id)
    show_status("run", orch.get_run(run_id).status)
    say(f"agent job {DIM}{job.job_id}{RESET} dispatched "
        "(instructions passed the secret-leak guard).")
    provider.run(job.job_id)

    def pr(**overrides) -> PullRequestState:
        base = dict(
            repo="customer-web",
            pr_number=42,
            url="https://github.com/example/customer-web/pull/42",
            branch="foundry/lin-101-add-customer-favourites",
            status=PRStatus.OPEN,
            ci_status=CIStatus.PENDING,
            files_changed=["src/components/Favourites.tsx", "src/api/favourites.ts"],
        )
        base.update(overrides)
        return PullRequestState.model_validate(base)

    say("The agent opens PR #42. Foundry verifies the diff against the guardrails:")
    show_status("run", orch.record_pr(run_id, pr()))

    # -- Act 4: CI fails; the agent fixes its own PR, under governance ---------
    act("Act 4 - CI fails; Foundry re-dispatches the agent with the failure")
    say("The check suite comes back red:")
    failing = pr(
        files_changed=[],
        ci_status=CIStatus.FAILING,
        summary="- unit tests: FavouritesButton renders twice on item cards",
    )
    status = orch.record_pr(run_id, failing)
    show_last_decision(sf, run_id)
    show_status("run", status)
    say("Same branch, original plan + failure excerpt, attempt counted against "
        "the retry cap.")

    # -- Act 5: green, reviewed, merged -----------------------------------------
    act("Act 5 - Green CI, human review, merge, done")
    orch.record_pr(run_id, pr())  # agent pushed the fix; PR re-opens clean
    orch.record_pr(
        run_id,
        pr(files_changed=[], ci_status=CIStatus.PASSING,
           review_status=ReviewStatus.APPROVED),
    )
    final = orch.record_pr(run_id, pr(status=PRStatus.MERGED))
    show_status("run", final)
    say("Linear was kept in sync the whole way. No auto-merge happened: a "
        "human clicked the button.")

    # -- Act 6: what getting stopped looks like ---------------------------------
    act("Act 6 - And this is what the brakes feel like")
    say("A second ticket; this time the agent's PR sneaks in a file under "
        "migrations/ :")
    risky = RawTicket(
        issue_id="demo-2",
        issue_key="LIN-102",
        title="Tidy up the favourites schema",
        description=(
            "Small cleanup.\n\nAcceptance Criteria:\n- Schema fields renamed\n"
        ),
        known_repositories=["customer-web"],
    )
    run_2 = orch.intake_and_plan(risky, trigger_type="label")
    orch.approve(run_2, user="lead@example.com")
    job_2 = orch.dispatch_agent(run_2)
    provider.run(job_2.job_id)
    blocked = orch.record_pr(
        run_2,
        PullRequestState.model_validate(dict(
            repo="customer-web",
            pr_number=43,
            url="https://github.com/example/customer-web/pull/43",
            branch="foundry/lin-102-tidy-up-the-favourites-schema",
            status=PRStatus.OPEN,
            files_changed=["src/models.py", "migrations/0007_rename.sql"],
        )),
    )
    show_status("run", blocked)
    say(f"{RED}Forbidden path touched -> blocked, audited, human required. "
        f"No retry resurrects it.{RESET}")

    # -- epilogue: the receipts --------------------------------------------------
    act("The receipts - every decision is written down")
    with sf() as session:
        events = (
            session.query(FoundryAuditEvent)
            .filter_by(run_id=run_id)
            .order_by(FoundryAuditEvent.sequence)
            .all()
        )
        say(f"audit trail for {ready.issue_key} ({len(events)} events):")
        for event in events:
            say(f"  #{event.sequence:<3} {event.event_type.value:<32} "
                f"{DIM}{event.actor_type}{RESET}")
        decisions = session.query(FoundryPolicyDecision).filter_by(run_id=run_id).count()
    say(f"plus {decisions} policy decisions with full inputs and reasons, "
        "content-hashed artifacts, and a per-run timeline at "
        "/runs/{id}/timeline.")
    say()
    say(f"{BOLD}{GREEN}Raw ore in, beskar out.{RESET} "
        f"{DIM}docs/quickstart.md takes this live in ~30 minutes.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
