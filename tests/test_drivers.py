"""Tests for the InlineDriver run-execution seam."""

from __future__ import annotations

import pytest

from foundry.agents.manual import InMemoryFakeProvider
from foundry.db import create_all, make_engine, make_session_factory
from foundry.drivers import InlineDriver
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.common import ApprovalRole, PRStatus, RunStatus
from foundry.schemas.pr import PullRequestState
from foundry.schemas.ticket import RawTicket

READY_DESC = "Acceptance Criteria:\n- A button exists\n- Favourites persist"


@pytest.fixture
def driver_and_orch():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    return InlineDriver(orch), orch


def _ready_ticket(**ov) -> RawTicket:
    base = dict(
        issue_id="i-1",
        issue_key="LIN-123",
        title="Add customer favourites",
        description=READY_DESC,
        known_repositories=["customer-web"],
    )
    base.update(ov)
    return RawTicket(**base)


def test_start_then_approve_dispatches(driver_and_orch) -> None:
    driver, orch = driver_and_orch
    run_id = driver.start(_ready_ticket(), trigger_type="label")
    assert orch.get_run(run_id).status is RunStatus.WAITING_APPROVAL

    driver.submit_decision(run_id, decision="approve", user="lead@example.com")
    assert orch.get_run(run_id).status is RunStatus.AGENT_RUNNING


def test_approve_human_only_work_ends_blocked_not_raised(driver_and_orch) -> None:
    driver, orch = driver_and_orch
    ticket = _ready_ticket(
        title="Rotate auth login session tokens",
        description="Acceptance Criteria:\n- auth tokens rotate\n- login works",
    )
    run_id = driver.start(ticket, trigger_type="label")
    # Auth work is human-only: the driver swallows the policy block and the run
    # ends blocked rather than raising.
    driver.submit_decision(
        run_id, decision="approve", user="lead@example.com",
        roles={ApprovalRole.ENGINEERING},
    )
    assert orch.get_run(run_id).status is RunStatus.BLOCKED


def test_reject(driver_and_orch) -> None:
    driver, orch = driver_and_orch
    run_id = driver.start(_ready_ticket(), trigger_type="label")
    driver.submit_decision(run_id, decision="reject", user="lead@example.com")
    assert orch.get_run(run_id).status is RunStatus.REJECTED


def test_observe_pr_records(driver_and_orch) -> None:
    driver, orch = driver_and_orch
    run_id = driver.start(_ready_ticket(), trigger_type="label")
    driver.submit_decision(run_id, decision="approve", user="lead@example.com")
    driver.observe_pr(
        run_id,
        PullRequestState(
            repo="customer-web",
            pr_number=1,
            url="https://github.com/o/customer-web/pull/1",
            branch="foundry/lin-123",
            status=PRStatus.OPEN,
            files_changed=["src/x.ts"],
        ),
    )
    assert orch.get_run(run_id).status is RunStatus.PR_OPEN


def test_unsupported_decision_raises(driver_and_orch) -> None:
    driver, _ = driver_and_orch
    run_id = driver.start(_ready_ticket(), trigger_type="label")
    with pytest.raises(ValueError):
        driver.submit_decision(run_id, decision="frobnicate", user="lead@example.com")
