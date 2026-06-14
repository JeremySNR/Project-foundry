"""End-to-end Temporal workflow test.

Runs the real workflow under Temporal's time-skipping test environment when it
is available. In sandboxes where the test-server binary cannot be fetched, the
whole module skips - the pure decision logic and activity glue are covered
elsewhere (test_workflow_decisions.py, test_temporal_activities.py).
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

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
    with ThreadPoolExecutor(max_workers=4) as executor:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TicketToPrWorkflow],
            activities=activities.all(),
            activity_executor=executor,
        ):
            handle = await env.client.start_workflow(
                TicketToPrWorkflow.run,
                {"ticket": READY_TICKET, "trigger_type": "label"},
                id=f"wf-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            # Approve, then deliver the observed PR.
            await handle.signal(
                TicketToPrWorkflow.submit_decision,
                args=["approve", "lead@example.com", []],
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
    with ThreadPoolExecutor(max_workers=4) as executor:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TicketToPrWorkflow],
            activities=activities.all(),
            activity_executor=executor,
        ):
            handle = await env.client.start_workflow(
                TicketToPrWorkflow.run,
                {"ticket": READY_TICKET, "trigger_type": "label"},
                id=f"wf-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            await handle.signal(
                TicketToPrWorkflow.submit_decision,
                args=["reject", "lead@example.com", []],
            )
            result = await handle.result()
            assert result["status"] == "rejected"


def _pr_event(status: str = "open") -> dict:
    return {
        "repo": "customer-web",
        "pr_number": 1,
        "url": "https://github.com/o/customer-web/pull/1",
        "branch": "foundry/lin-123-add-customer-favourites",
        "status": status,
        "files_changed": ["src/x.ts"],
    }


async def test_workflow_approval_timeout_blocks_cleanly(env) -> None:
    # No decision is ever sent: the time-skipping env fast-forwards past the
    # approval window. The workflow must end the run cleanly (blocked), not fail
    # and strand it at waiting_approval (issue #15, problem 1).
    activities = _activities()
    with ThreadPoolExecutor(max_workers=4) as executor:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TicketToPrWorkflow],
            activities=activities.all(),
            activity_executor=executor,
        ):
            handle = await env.client.start_workflow(
                TicketToPrWorkflow.run,
                {"ticket": READY_TICKET, "trigger_type": "label"},
                id=f"wf-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            result = await handle.result()
            assert result["status"] == "blocked"


async def test_workflow_pr_timeout_fails_cleanly(env) -> None:
    # Approved and dispatched, but the agent never opens a PR: the PR window
    # elapses and the run ends as execution_failed, not stranded at agent_running.
    activities = _activities()
    with ThreadPoolExecutor(max_workers=4) as executor:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TicketToPrWorkflow],
            activities=activities.all(),
            activity_executor=executor,
        ):
            handle = await env.client.start_workflow(
                TicketToPrWorkflow.run,
                {"ticket": READY_TICKET, "trigger_type": "label"},
                id=f"wf-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            await handle.signal(
                TicketToPrWorkflow.submit_decision,
                args=["approve", "lead@example.com", []],
            )
            result = await handle.result()
            assert result["status"] == "execution_failed"


async def test_workflow_ignores_unknown_decision_then_accepts_valid(env) -> None:
    # An unrecognised verb must not silently stop the run (issue #15, problem 2):
    # it is dropped, and a subsequent valid reject is honoured.
    activities = _activities()
    with ThreadPoolExecutor(max_workers=4) as executor:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TicketToPrWorkflow],
            activities=activities.all(),
            activity_executor=executor,
        ):
            handle = await env.client.start_workflow(
                TicketToPrWorkflow.run,
                {"ticket": READY_TICKET, "trigger_type": "label"},
                id=f"wf-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            await handle.signal(
                TicketToPrWorkflow.submit_decision,
                args=["frobnicate", "attacker@example.com", []],
            )
            await handle.signal(
                TicketToPrWorkflow.submit_decision,
                args=["reject", "lead@example.com", []],
            )
            result = await handle.result()
            assert result["status"] == "rejected"


async def test_workflow_processes_multiple_pr_events_until_terminal(env) -> None:
    # The workflow loops on every PR event, not just the first (issue #15,
    # problem 4): an opened PR followed by a merge drives the run to complete.
    activities = _activities()
    with ThreadPoolExecutor(max_workers=4) as executor:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TicketToPrWorkflow],
            activities=activities.all(),
            activity_executor=executor,
        ):
            handle = await env.client.start_workflow(
                TicketToPrWorkflow.run,
                {"ticket": READY_TICKET, "trigger_type": "label"},
                id=f"wf-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            await handle.signal(
                TicketToPrWorkflow.submit_decision,
                args=["approve", "lead@example.com", []],
            )
            await handle.signal(TicketToPrWorkflow.pr_observed, _pr_event("open"))
            await handle.signal(TicketToPrWorkflow.pr_observed, _pr_event("merged"))
            result = await handle.result()
            assert result["status"] == "complete"
