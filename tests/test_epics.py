"""Epic parent/child run model + rollup (issue #35).

Two layers, both offline: the pure ``compute_epic_rollup`` over child statuses,
and the orchestrator linkage that creates child runs under a parent.
"""

from __future__ import annotations

import pytest

from foundry.agents.manual import InMemoryFakeProvider
from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.epics import EpicStatus, compute_epic_rollup
from foundry.orchestrator import FoundryOrchestrator, OrchestratorError
from foundry.schemas.common import RunStatus
from foundry.schemas.ticket import RawTicket

READY_DESC = (
    "Customers want to favourite items.\n\n"
    "Acceptance Criteria:\n"
    "- A favourites button exists\n"
    "- Favourites persist across sessions\n"
)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


def _orch(session_factory) -> FoundryOrchestrator:
    return FoundryOrchestrator(session_factory, provider=InMemoryFakeProvider())


def _ready_ticket(issue_id: str, key: str) -> RawTicket:
    return RawTicket(
        issue_id=issue_id,
        issue_key=key,
        title="Add customer favourites",
        description=READY_DESC,
        known_repositories=["customer-web"],
    )


# -- pure rollup ---------------------------------------------------------------


def test_rollup_empty_has_no_children() -> None:
    rollup = compute_epic_rollup([])
    assert rollup["status"] == EpicStatus.EMPTY.value
    assert rollup["total"] == 0
    assert rollup["counts"] == {"active": 0, "complete": 0, "unsuccessful": 0}
    assert rollup["status_breakdown"] == {}


def test_rollup_in_progress_when_any_child_active() -> None:
    rollup = compute_epic_rollup([RunStatus.COMPLETE, RunStatus.AGENT_RUNNING])
    assert rollup["status"] == EpicStatus.IN_PROGRESS.value
    assert rollup["counts"] == {"active": 1, "complete": 1, "unsuccessful": 0}


def test_rollup_complete_when_all_children_merged() -> None:
    rollup = compute_epic_rollup([RunStatus.COMPLETE, RunStatus.COMPLETE])
    assert rollup["status"] == EpicStatus.COMPLETE.value
    assert rollup["total"] == 2


def test_rollup_partial_when_some_merged_some_not() -> None:
    rollup = compute_epic_rollup([RunStatus.COMPLETE, RunStatus.BLOCKED])
    assert rollup["status"] == EpicStatus.PARTIAL.value
    assert rollup["counts"] == {"active": 0, "complete": 1, "unsuccessful": 1}


def test_rollup_failed_when_no_child_merged() -> None:
    rollup = compute_epic_rollup(
        [RunStatus.BLOCKED, RunStatus.REJECTED, RunStatus.EXECUTION_FAILED]
    )
    assert rollup["status"] == EpicStatus.FAILED.value
    assert rollup["counts"] == {"active": 0, "complete": 0, "unsuccessful": 3}


def test_rollup_needs_clarification_counts_as_unsuccessful() -> None:
    # An unfinished-but-terminal child is "unsuccessful", not "active".
    rollup = compute_epic_rollup([RunStatus.NEEDS_CLARIFICATION])
    assert rollup["status"] == EpicStatus.FAILED.value
    assert rollup["counts"]["unsuccessful"] == 1


def test_rollup_is_order_independent() -> None:
    a = compute_epic_rollup([RunStatus.COMPLETE, RunStatus.BLOCKED, RunStatus.PR_OPEN])
    b = compute_epic_rollup([RunStatus.PR_OPEN, RunStatus.COMPLETE, RunStatus.BLOCKED])
    assert a == b


def test_rollup_status_breakdown_counts_each_status() -> None:
    rollup = compute_epic_rollup(
        [RunStatus.COMPLETE, RunStatus.COMPLETE, RunStatus.BLOCKED]
    )
    assert rollup["status_breakdown"] == {"complete": 2, "blocked": 1}


# -- orchestrator linkage ------------------------------------------------------


def _parent_id(session_factory) -> str:
    orch = _orch(session_factory)
    return orch.intake_and_plan(
        _ready_ticket("epic-1", "LIN-100"), trigger_type="label"
    )


def test_child_run_records_parent(session_factory) -> None:
    orch = _orch(session_factory)
    parent = orch.intake_and_plan(
        _ready_ticket("epic-1", "LIN-100"), trigger_type="label"
    )
    child = orch.intake_and_plan(
        _ready_ticket("child-1", "LIN-101"),
        trigger_type="label",
        parent_run_id=parent,
    )
    with session_factory() as s:
        assert s.get(FoundryRun, child).parent_run_id == parent
        assert s.get(FoundryRun, parent).parent_run_id is None


def test_child_runs_lists_children(session_factory) -> None:
    orch = _orch(session_factory)
    parent = orch.intake_and_plan(
        _ready_ticket("epic-1", "LIN-100"), trigger_type="label"
    )
    c1 = orch.intake_and_plan(
        _ready_ticket("c-1", "LIN-101"), trigger_type="label", parent_run_id=parent
    )
    c2 = orch.intake_and_plan(
        _ready_ticket("c-2", "LIN-102"), trigger_type="label", parent_run_id=parent
    )
    children = {r.id for r in orch.child_runs(parent)}
    assert children == {c1, c2}
    # The parent itself has no parent and is not its own child.
    assert orch.child_runs(c1) == []


def test_epic_root_id_resolves_from_child_or_parent(session_factory) -> None:
    orch = _orch(session_factory)
    parent = orch.intake_and_plan(
        _ready_ticket("epic-1", "LIN-100"), trigger_type="label"
    )
    child = orch.intake_and_plan(
        _ready_ticket("c-1", "LIN-101"), trigger_type="label", parent_run_id=parent
    )
    assert orch.epic_root_id(child) == parent
    assert orch.epic_root_id(parent) == parent
    assert orch.epic_root_id("does-not-exist") is None


def test_epic_rollup_over_live_children(session_factory) -> None:
    orch = _orch(session_factory)
    parent = orch.intake_and_plan(
        _ready_ticket("epic-1", "LIN-100"), trigger_type="label"
    )
    orch.intake_and_plan(
        _ready_ticket("c-1", "LIN-101"), trigger_type="label", parent_run_id=parent
    )
    orch.intake_and_plan(
        _ready_ticket("c-2", "LIN-102"), trigger_type="label", parent_run_id=parent
    )
    rollup = orch.epic_rollup(parent)
    # Both children park at WAITING_APPROVAL, an active status.
    assert rollup["status"] == EpicStatus.IN_PROGRESS.value
    assert rollup["total"] == 2
    assert rollup["counts"]["active"] == 2


def test_link_to_missing_parent_is_rejected(session_factory) -> None:
    orch = _orch(session_factory)
    with pytest.raises(OrchestratorError, match="does not exist"):
        orch.intake_and_plan(
            _ready_ticket("c-1", "LIN-101"),
            trigger_type="label",
            parent_run_id="no-such-run",
        )


def test_epics_cannot_nest(session_factory) -> None:
    orch = _orch(session_factory)
    parent = orch.intake_and_plan(
        _ready_ticket("epic-1", "LIN-100"), trigger_type="label"
    )
    child = orch.intake_and_plan(
        _ready_ticket("c-1", "LIN-101"), trigger_type="label", parent_run_id=parent
    )
    # A child cannot itself be a parent - epics are a single level in v1.
    with pytest.raises(OrchestratorError, match="single level"):
        orch.intake_and_plan(
            _ready_ticket("gc-1", "LIN-102"),
            trigger_type="label",
            parent_run_id=child,
        )


def test_list_epics_returns_only_roots_with_children(session_factory) -> None:
    orch = _orch(session_factory)
    # A plain run with no children is not an epic.
    orch.intake_and_plan(_ready_ticket("solo", "LIN-1"), trigger_type="label")
    # Two real epics, each with children.
    epic_a = orch.intake_and_plan(
        _ready_ticket("epic-a", "LIN-100"), trigger_type="label"
    )
    orch.intake_and_plan(
        _ready_ticket("a-1", "LIN-101"), trigger_type="label", parent_run_id=epic_a
    )
    orch.intake_and_plan(
        _ready_ticket("a-2", "LIN-102"), trigger_type="label", parent_run_id=epic_a
    )
    epic_b = orch.intake_and_plan(
        _ready_ticket("epic-b", "LIN-200"), trigger_type="label"
    )
    orch.intake_and_plan(
        _ready_ticket("b-1", "LIN-201"), trigger_type="label", parent_run_id=epic_b
    )

    roots = orch.list_epics()
    # Only the two parents are epics; the solo run and the children are omitted.
    assert [r.id for r in roots] == [epic_a, epic_b]  # oldest first


def test_list_epics_empty_when_no_children(session_factory) -> None:
    orch = _orch(session_factory)
    orch.intake_and_plan(_ready_ticket("solo", "LIN-1"), trigger_type="label")
    assert orch.list_epics() == []


# -- decomposition producer (intake_epic) -------------------------------------

EPIC_DESC = (
    "Add favourites across our surfaces.\n\n"
    "Repositories:\n"
    "- customer-web: add the favourites button\n"
    "- mobile-app: add the favourites button\n\n"
    "Acceptance Criteria:\n"
    "- A favourites button exists\n"
    "- Favourites persist across sessions\n"
)


def _epic_ticket() -> RawTicket:
    return RawTicket(
        issue_id="epic-99",
        issue_key="LIN-900",
        title="Add favourites everywhere",
        description=EPIC_DESC,
    )


def test_intake_epic_opens_parent_and_one_child_per_repo(session_factory) -> None:
    orch = _orch(session_factory)
    result = orch.intake_epic(_epic_ticket(), trigger_type="label")

    assert result.is_epic is True
    assert len(result.child_run_ids) == 2
    # Children are linked under the parent and discoverable as an epic.
    children = orch.child_runs(result.parent_run_id)
    assert {c.id for c in children} == set(result.child_run_ids)
    assert all(c.parent_run_id == result.parent_run_id for c in children)
    assert [r.id for r in orch.list_epics()] == [result.parent_run_id]


def test_intake_epic_children_are_independently_gated(session_factory) -> None:
    orch = _orch(session_factory)
    result = orch.intake_epic(_epic_ticket(), trigger_type="label")

    # Each scoped child is ready + routable + low risk, so it parks for its
    # own approval - the gate ran once per child, not once for the epic.
    for child_id in result.child_run_ids:
        assert orch.get_run(child_id).status is RunStatus.WAITING_APPROVAL
    rollup = orch.epic_rollup(result.parent_run_id)
    assert rollup["status"] == EpicStatus.IN_PROGRESS.value
    assert rollup["counts"]["active"] == 2


def test_intake_epic_children_approve_independently(session_factory) -> None:
    orch = _orch(session_factory)
    result = orch.intake_epic(_epic_ticket(), trigger_type="label")
    first, second = result.child_run_ids

    # Approving one child does not touch the other - independent gating.
    orch.approve(first, user="lead@example.com")
    assert orch.get_run(first).status is RunStatus.APPROVED
    assert orch.get_run(second).status is RunStatus.WAITING_APPROVAL
    assert orch.epic_rollup(result.parent_run_id)["counts"]["active"] == 2


def test_intake_epic_non_epic_degrades_to_single_run(session_factory) -> None:
    orch = _orch(session_factory)
    result = orch.intake_epic(_ready_ticket("solo", "LIN-1"), trigger_type="label")

    assert result.is_epic is False
    assert result.child_run_ids == []
    assert orch.child_runs(result.parent_run_id) == []
    assert orch.list_epics() == []
    # The single run still went through intake normally.
    assert orch.get_run(result.parent_run_id).status is RunStatus.WAITING_APPROVAL


def test_intake_epic_rejects_duplicate_active_epic(session_factory) -> None:
    orch = _orch(session_factory)
    orch.intake_epic(_epic_ticket(), trigger_type="label")
    # The parent's issue already has an active run; re-triggering fails loudly
    # on the one-active-run-per-issue guard before any new child is created.
    with pytest.raises(OrchestratorError, match="already has an active run"):
        orch.intake_epic(_epic_ticket(), trigger_type="label")


def test_intake_epic_uses_injected_llm_decomposer(session_factory) -> None:
    # A prose epic with no Repositories section and no associated repos: the
    # deterministic floor declines, so the injected LLM decomposer drives the
    # split and intake_epic fans out into one independently-gated child per repo.
    from foundry.engines.llm import FakeStructuredLLM
    from foundry.engines.llm_decomposition import LlmDecomposer

    llm = FakeStructuredLLM(
        [
            {
                "is_epic": True,
                "repositories": [
                    {"repo": "customer-web", "scope": "add the favourites button"},
                    {"repo": "mobile-app", "scope": "add the favourites button"},
                ],
                "reason": "spans two surfaces",
            }
        ]
    )
    orch = FoundryOrchestrator(
        session_factory,
        provider=InMemoryFakeProvider(),
        decomposer=LlmDecomposer(llm),
    )
    ticket = RawTicket(
        issue_id="epic-prose",
        issue_key="LIN-700",
        title="Add favourites everywhere",
        description=(
            "Add favourites in customer-web and mobile-app together.\n\n"
            "Acceptance Criteria:\n"
            "- A favourites button exists\n"
            "- Favourites persist across sessions\n"
        ),
    )
    result = orch.intake_epic(ticket, trigger_type="label")

    assert result.is_epic is True
    assert len(result.child_run_ids) == 2
    for child_id in result.child_run_ids:
        assert orch.get_run(child_id).status is RunStatus.WAITING_APPROVAL
