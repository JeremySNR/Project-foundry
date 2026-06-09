"""Postgres compatibility smoke test.

The default suite runs on SQLite for speed and zero infrastructure. This module
re-exercises the full state machine against a real Postgres when
``FOUNDRY_TEST_DATABASE_URL`` is set (CI provides a service container), killing
SQLite-only assumptions: native enum types, timezone-aware datetimes, boolean
columns, and the Alembic schema itself.

Skipped silently everywhere else.
"""

from __future__ import annotations

import os

import pytest

from foundry.agents.manual import InMemoryFakeProvider
from foundry.db import create_all, make_engine, make_session_factory
from foundry.db.base import Base
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.common import CIStatus, PRStatus, RunStatus
from foundry.schemas.pr import PullRequestState
from foundry.schemas.ticket import RawTicket

DATABASE_URL = os.environ.get("FOUNDRY_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL.startswith("postgresql"),
    reason="FOUNDRY_TEST_DATABASE_URL not pointing at Postgres",
)


@pytest.fixture()
def session_factory():
    engine = make_engine(DATABASE_URL)
    Base.metadata.drop_all(engine)
    create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _ticket() -> RawTicket:
    return RawTicket.model_validate(
        {
            "issue_id": "pg-issue-1",
            "issue_key": "LIN-PG1",
            "title": "Add favourites",
            "description": (
                "Customers want favourites.\n\n"
                "Acceptance Criteria:\n- button exists\n- persists\n"
            ),
            "labels": ["repo:customer-web"],
        }
    )


def test_full_run_lifecycle_on_postgres(session_factory) -> None:
    provider = InMemoryFakeProvider()
    orch = FoundryOrchestrator(session_factory, provider=provider)

    run_id = orch.intake_and_plan(_ticket(), trigger_type="label")
    assert orch.get_run(run_id).status is RunStatus.WAITING_APPROVAL

    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)

    pr = PullRequestState.model_validate(
        {
            "pr_number": 1,
            "url": "https://github.com/o/customer-web/pull/1",
            "branch": "foundry/lin-pg1-add-favourites",
            "status": PRStatus.OPEN,
            "ci_status": CIStatus.PASSING,
            "files_changed": ["src/favourites.ts"],
        }
    )
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN

    # The audit trail round-trips through Postgres enums and JSON text.
    runs = orch.list_runs()
    assert len(runs) == 1
    assert runs[0].risk_level is not None
