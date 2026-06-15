#!/usr/bin/env python3
"""Block until a Temporal server is ready to run workflows.

Used by the ``temporal`` CI job (and anyone running the real-server E2E locally)
to wait out the gap between ``docker compose --profile temporal up`` returning
and the ``temporalio/auto-setup`` server actually serving: the frontend accepts
connections a little before the ``default`` namespace is registered, and a
workflow start against a missing namespace fails. We therefore poll until a
``DescribeNamespace`` for the target namespace succeeds, which proves the server
is up *and* the namespace exists.

Pure ``temporalio`` client calls, no project imports - it only needs the
``[workflow]`` extra. Reads the same ``FOUNDRY_TEMPORAL_TEST_ADDRESS`` the test
fixture uses (default ``localhost:7233``); namespace defaults to ``default``.

Exit 0 once ready, 1 if it never becomes ready within the timeout.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client


async def _wait(address: str, namespace: str, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        attempt += 1
        try:
            client = await Client.connect(address, namespace=namespace)
            await client.workflow_service.describe_namespace(
                DescribeNamespaceRequest(namespace=namespace)
            )
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready yet"
            last_error = exc
            print(f"[{attempt}] temporal not ready at {address}: {exc}")
            await asyncio.sleep(2.0)
            continue
        print(f"temporal ready at {address} (namespace {namespace!r}) after {attempt} checks")
        return True
    print(f"temporal did not become ready within {timeout_seconds:.0f}s: {last_error}", file=sys.stderr)
    return False


def main() -> int:
    address = os.getenv("FOUNDRY_TEMPORAL_TEST_ADDRESS", "localhost:7233")
    namespace = os.getenv("FOUNDRY_TEMPORAL_TEST_NAMESPACE", "default")
    timeout_seconds = float(os.getenv("FOUNDRY_TEMPORAL_WAIT_TIMEOUT", "180"))
    ready = asyncio.run(_wait(address, namespace, timeout_seconds))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
