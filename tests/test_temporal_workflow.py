"""End-to-end Temporal workflow test.

Runs the real workflow under Temporal's time-skipping test environment when it
is available. In sandboxes where the test-server binary cannot be fetched, the
whole module skips - the pure decision logic and activity glue are covered
elsewhere (test_workflow_decisions.py, test_temporal_activities.py).
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("temporalio")

import pytest_asyncio  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from foundry.agents.manual import InMemoryFakeProvider  # noqa: E402
from foundry.connectors import InMemoryIssueTracker  # noqa: E402
from foundry.db import create_all, make_engine, make_session_factory  # noqa: E402
from foundry.orchestrator import FoundryOrchestrator  # noqa: E402
from foundry.workflows.activities import FoundryActivities  # noqa: E402
from foundry.workflows.workflow import TicketToPrWorkflow  # noqa: E402

TASK_QUEUE = "foundry-test"

READY_TICKET = {
    "issue_id": "i-1",
    "issue_key": "LIN-123",
    "title": "Add customer favourites",
    "description": "Acceptance Criteria:\n- A button exists\n- Favourites persist",
    "known_repositories": ["customer-web"],
}


@pytest_asyncio.fixture
async def env():
    try:
        environment = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Temporal test server unavailable: {exc}")
    yield environment
    await environment.shutdown()


def _activities() -> FoundryActivities:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(
        sf, provider=InMemoryFakeProvider(), issue_tracker=InMemoryIssueTracker()
    )
    return FoundryActivities(orch)


async def test_workflow_happy_path_with_signals(env) -> None:
    activities = _activities()
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[TicketToPrWorkflow],
        activities=activities.all(),
    ):
        handle = await env.client.start_workflow(
            TicketToPrWorkflow.run,
            {"ticket": READY_TICKET, "trigger_type": "label"},
            id=f"wf-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        # Approve, then deliver the observed PR.
        await handle.signal(
            TicketToPrWorkflow.submit_decision, "approve", "lead@example.com", []
        )
        await handle.signal(
            TicketToPrWorkflow.pr_observed,
            {
                "repo": "customer-web",
                "pr_number": 1,
                "url": "https://github.com/o/customer-web/pull/1",
                "branch": "foundry/lin-123-add-customer-favourites",
                "status": "open",
                "files_changed": ["src/x.ts"],
            },
        )
        result = await handle.result()
        assert result["status"] == "pr_open"


async def test_workflow_reject_path(env) -> None:
    activities = _activities()
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[TicketToPrWorkflow],
        activities=activities.all(),
    ):
        handle = await env.client.start_workflow(
            TicketToPrWorkflow.run,
            {"ticket": READY_TICKET, "trigger_type": "label"},
            id=f"wf-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        await handle.signal(
            TicketToPrWorkflow.submit_decision, "reject", "lead@example.com", []
        )
        result = await handle.result()
        assert result["status"] == "rejected"
