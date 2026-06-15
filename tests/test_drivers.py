"""Tests for the InlineDriver run-execution seam."""

from __future__ import annotations

import pytest

from foundry.agents.manual import InMemoryFakeProvider
from foundry.db import create_all, make_engine, make_session_factory
from foundry.drivers import InlineDriver
from foundry.orchestrator import FoundryOrchestrator, OrchestratorError
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


def test_two_person_rule_needs_a_second_approver(driver_and_orch) -> None:
    """Under an N-of-M approval matrix (issue #31) the first approval through the
    driver does not dispatch: dispatch_agent raises (run still WAITING_APPROVAL)
    and is swallowed, so the run waits for the second distinct approver, who
    drives it through to dispatch."""
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider(), min_approvals=2)
    driver = InlineDriver(orch)
    run_id = driver.start(_ready_ticket(), trigger_type="label")

    driver.submit_decision(run_id, decision="approve", user="alice@example.com")
    assert orch.get_run(run_id).status is RunStatus.WAITING_APPROVAL

    driver.submit_decision(run_id, decision="approve", user="bob@example.com")
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


def test_approve_without_required_role_raises_not_swallowed(driver_and_orch) -> None:
    """Unlike a human-only policy block (swallowed so the run simply ends blocked),
    an approval the approver is not authorised for is refused *loudly* so the
    calling surface can report it - and the run stays awaiting approval (issue #18)."""
    driver, orch = driver_and_orch
    ticket = _ready_ticket(
        title="Update the terraform deployment config",
        description="Acceptance Criteria:\n- terraform plan runs clean\n- config applies",
    )
    run_id = driver.start(ticket, trigger_type="label")
    with pytest.raises(OrchestratorError, match="approval refused"):
        driver.submit_decision(run_id, decision="approve", user="lead@example.com")
    assert orch.get_run(run_id).status is RunStatus.WAITING_APPROVAL


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


# -- epic auto-decomposition at intake (issue #35) ----------------------------

EPIC_DESC = (
    "Add favourites across our surfaces.\n\n"
    "Repositories:\n"
    "- customer-web: add the favourites button\n"
    "- mobile-app: add the favourites button\n\n"
    "Acceptance Criteria:\n"
    "- A favourites button exists\n"
    "- Favourites persist across sessions\n"
)


def _epic_ticket(**ov) -> RawTicket:
    base = dict(
        issue_id="epic-1",
        issue_key="LIN-900",
        title="Add favourites everywhere",
        description=EPIC_DESC,
    )
    base.update(ov)
    return RawTicket(**base)


def test_default_driver_does_not_decompose_epics(driver_and_orch) -> None:
    """The default driver is unchanged: a multi-repo epic ticket runs as a single
    ordinary run, no children - decomposition is opt-in."""
    driver, orch = driver_and_orch
    run_id = driver.start(_epic_ticket(), trigger_type="label")
    assert orch.child_runs(run_id) == []
    assert orch.list_epics() == []


def test_auto_decompose_driver_fans_epic_into_child_runs(driver_and_orch) -> None:
    _, orch = driver_and_orch
    driver = InlineDriver(orch, auto_decompose_epics=True)

    parent_run_id = driver.start(_epic_ticket(), trigger_type="label")

    # start() returns the parent (epic-root) run id, and the epic fanned out
    # into one independently-gated child run per repo.
    children = orch.child_runs(parent_run_id)
    assert len(children) == 2
    assert all(c.parent_run_id == parent_run_id for c in children)
    assert [r.id for r in orch.list_epics()] == [parent_run_id]
    # Each child is gated on its own and parks for its own approval.
    for child in children:
        assert child.status is RunStatus.WAITING_APPROVAL


def test_auto_decompose_driver_non_epic_runs_as_single_run(driver_and_orch) -> None:
    """With decomposition on, a ticket scoped to one repo still degrades to a
    single ordinary run with no children - so the path is always safe to take."""
    _, orch = driver_and_orch
    driver = InlineDriver(orch, auto_decompose_epics=True)

    run_id = driver.start(_ready_ticket(), trigger_type="label")

    assert orch.child_runs(run_id) == []
    assert orch.list_epics() == []
    assert orch.get_run(run_id).status is RunStatus.WAITING_APPROVAL
