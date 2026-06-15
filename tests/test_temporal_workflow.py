"""End-to-end Temporal workflow test.

Runs the real workflow against a Temporal server in one of two modes:

- **Time-skipping test server** (the default): ``WorkflowEnvironment``'s
  in-memory harness, fetched on demand. It can fast-forward past the workflow's
  multi-day durable waits, so it covers the approval/PR *timeout* paths. In
  sandboxes where the test-server binary cannot be fetched, the whole module
  skips - the pure decision logic and activity glue are covered elsewhere
  (test_workflow_decisions.py, test_temporal_activities.py).

- **Real server** (set ``FOUNDRY_TEMPORAL_TEST_ADDRESS``, e.g. ``localhost:7233``):
  connects to an actual ``temporalio/auto-setup`` server - the Postgres-backed
  production binary shipped in docker-compose's ``temporal`` profile - so the
  durability claim is proven against the real backend, not just the in-memory
  harness (issue #37). A real server does *not* skip time, so the two timeout
  cases (which would otherwise wait days) skip in this mode; every signal-driven
  case runs unchanged. CI boots the profile and points this var at it.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("temporalio")

import pytest_asyncio  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from temporalio.client import WorkflowFailureError  # noqa: E402

from foundry.agents.manual import InMemoryFakeProvider  # noqa: E402
from foundry.connectors import InMemoryIssueTracker  # noqa: E402
from foundry.db import (  # noqa: E402
    FoundryRun,
    create_all,
    make_engine,
    make_session_factory,
)
from foundry.orchestrator import FoundryOrchestrator  # noqa: E402
from foundry.schemas.common import RunStatus  # noqa: E402
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
    address = os.getenv("FOUNDRY_TEMPORAL_TEST_ADDRESS")
    if address:
        # Real-server mode: connect to the running Temporal server (the
        # docker-compose `temporal` profile in CI) rather than the in-memory
        # time-skipping harness. ``from_client`` reports ``supports_time_skipping
        # = False`` so the timeout cases skip themselves (a real server would
        # genuinely wait days). The caller owns the server lifecycle, so
        # ``shutdown()`` here only closes the client wrapper.
        from temporalio.client import Client

        client = await Client.connect(address)
        environment = WorkflowEnvironment.from_client(client)
        try:
            yield environment
        finally:
            await environment.shutdown()
        return
    try:
        environment = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Temporal test server unavailable: {exc}")
    yield environment
    await environment.shutdown()


def _require_time_skipping(env) -> None:
    """Skip a timeout case that needs the time-skipping harness.

    The approval/PR windows are measured in days (``workflow._APPROVAL_TIMEOUT``
    / ``_PR_TIMEOUT``); only the time-skipping server can fast-forward past them
    in a test. Against a real server (``FOUNDRY_TEMPORAL_TEST_ADDRESS``) these
    would block for the full window, so they are skipped there - the real-server
    job proves the signal-driven paths, the time-skipping job proves the waits.
    """
    if not env.supports_time_skipping:
        pytest.skip(
            "approval/PR-window timeout test needs the time-skipping server; "
            "against a real server it would wait days"
        )


def _activities() -> FoundryActivities:
    return _activities_with_db()[0]


def _activities_with_db() -> tuple[FoundryActivities, object]:
    """Activities plus the session factory, so a test can inspect the run row
    after the workflow finishes (or fails)."""
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(
        sf, provider=InMemoryFakeProvider(), issue_tracker=InMemoryIssueTracker()
    )
    return FoundryActivities(orch), sf


async def test_workflow_happy_path_with_signals(env) -> None:
    # Ends at pr_open: once the PR is observed the workflow keeps watching for
    # further pushes (pr_open is PR-observable) and only settles at pr_open when
    # the PR window goes quiet - which needs the time-skipping server to
    # fast-forward the multi-day _PR_TIMEOUT. Against a real server that wait is
    # real, so this case skips there; the approve -> dispatch -> PR -> terminal
    # path is proven on the real server by the multi-PR-to-merge case below.
    _require_time_skipping(env)
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
    _require_time_skipping(env)
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
    _require_time_skipping(env)
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


async def test_workflow_compensates_irrecoverable_activity_error(env) -> None:
    # An activity exhausts its retries mid-flight (here a malformed PR payload
    # makes record_pr raise a non-retryable ValidationError while the run is
    # agent_running). The workflow must run the fail_run compensation so the run
    # row ends execution_failed - not stranded active forever - and then surface
    # the failure to the operator rather than swallowing it (issue #37).
    activities, sf = _activities_with_db()
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
            # A malformed PR payload: record_pr's validation raises and the
            # activity is non-retryable on ValidationError.
            await handle.signal(
                TicketToPrWorkflow.pr_observed, {"not": "a valid pr state"}
            )
            with pytest.raises(WorkflowFailureError):
                await handle.result()

    # The compensation made the run's lifecycle honest despite the crash.
    with sf() as s:
        run = s.query(FoundryRun).one()
        assert run.status is RunStatus.EXECUTION_FAILED


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
