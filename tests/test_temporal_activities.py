"""Temporal activity-glue tests against a real orchestrator (no server needed)."""

from __future__ import annotations

import pytest

pytest.importorskip("temporalio")

from foundry.agents.manual import InMemoryFakeProvider  # noqa: E402
from foundry.connectors import InMemoryIssueTracker  # noqa: E402
from foundry.db import create_all, make_engine, make_session_factory  # noqa: E402
from foundry.orchestrator import FoundryOrchestrator  # noqa: E402
from foundry.workflows.activities import FoundryActivities  # noqa: E402

READY_DESC = (
    "Customers want to favourite items.\n\n"
    "Acceptance Criteria:\n- A button exists\n- Favourites persist\n"
)


@pytest.fixture
def activities() -> FoundryActivities:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(
        sf, provider=InMemoryFakeProvider(), issue_tracker=InMemoryIssueTracker()
    )
    return FoundryActivities(orch)


def _ticket_params() -> dict:
    return {
        "ticket": {
            "issue_id": "i-1",
            "issue_key": "LIN-123",
            "title": "Add customer favourites",
            "description": READY_DESC,
            "known_repositories": ["customer-web"],
        },
        "trigger_type": "label",
    }


def test_intake_activity_returns_run_and_status(activities: FoundryActivities) -> None:
    result = activities.intake_and_plan(_ticket_params())
    assert result["run_id"]
    assert result["status"] == "waiting_approval"


def test_approve_then_dispatch_activities(activities: FoundryActivities) -> None:
    run_id = activities.intake_and_plan(_ticket_params())["run_id"]
    activities.approve({"run_id": run_id, "user": "lead@example.com", "roles": []})
    dispatched = activities.dispatch_agent(run_id)
    assert dispatched["dispatched"] is True
    assert dispatched["status"] == "agent_running"


def test_dispatch_blocked_reports_cleanly(activities: FoundryActivities) -> None:
    # Auth work is human-only: dispatch is blocked but reported, not raised.
    params = _ticket_params()
    params["ticket"]["title"] = "Rotate auth login session tokens"
    params["ticket"]["description"] = (
        "Acceptance Criteria:\n- auth tokens rotate\n- login still works"
    )
    run_id = activities.intake_and_plan(params)["run_id"]
    activities.approve(
        {"run_id": run_id, "user": "lead@example.com", "roles": ["engineering"]}
    )
    dispatched = activities.dispatch_agent(run_id)
    assert dispatched["dispatched"] is False
    assert dispatched["status"] == "blocked"
    assert "detail" in dispatched


def test_record_pr_activity(activities: FoundryActivities) -> None:
    run_id = activities.intake_and_plan(_ticket_params())["run_id"]
    activities.approve({"run_id": run_id, "user": "lead@example.com", "roles": []})
    activities.dispatch_agent(run_id)
    result = activities.record_pr(
        {
            "run_id": run_id,
            "pr_state": {
                "repo": "customer-web",
                "pr_number": 1,
                "url": "https://github.com/o/customer-web/pull/1",
                "branch": "foundry/lin-123",
                "status": "open",
                "files_changed": ["src/x.ts"],
            },
        }
    )
    assert result["status"] == "pr_open"


def test_reject_activity(activities: FoundryActivities) -> None:
    run_id = activities.intake_and_plan(_ticket_params())["run_id"]
    result = activities.reject({"run_id": run_id, "user": "lead@example.com"})
    assert result["status"] == "rejected"
