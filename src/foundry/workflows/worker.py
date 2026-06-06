"""Temporal worker entrypoint.

Connects to a Temporal server, registers the Ticket-to-PR workflow and the
Foundry activities (bound to a live orchestrator), and serves the task queue.
Activities run in a thread-pool executor because they do blocking work.

Usage (live; needs a running Temporal at ``TEMPORAL_ADDRESS``)::

    import asyncio
    from foundry.workflows.worker import run_worker
    asyncio.run(run_worker(orchestrator))
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from foundry.orchestrator import FoundryOrchestrator
from foundry.workflows.activities import FoundryActivities
from foundry.workflows.workflow import TicketToPrWorkflow

TASK_QUEUE = "foundry-ticket-to-pr"


async def run_worker(
    orchestrator: FoundryOrchestrator,
    *,
    address: str | None = None,
    task_queue: str = TASK_QUEUE,
    max_workers: int = 16,
) -> None:  # pragma: no cover - requires a live Temporal server
    client = await Client.connect(
        address or os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    )
    activities = FoundryActivities(orchestrator)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[TicketToPrWorkflow],
            activities=activities.all(),
            activity_executor=executor,
        )
        await worker.run()
