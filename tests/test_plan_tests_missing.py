"""Plan-tests satisfaction escalation (issue #169, slice 2): the deterministic
test-plan member of the same orchestrator-only, escalate-only plan-aware family
as the scope-drift and out-of-scope checks. When the approved ``DeliveryPlan``
promised tests (any ``test_plan.unit_tests`` / ``integration_tests`` /
``e2e_tests``) but the diff touches *no* test file, the run escalates to a human.

Two layers:
- the pure ``diff_touches_tests`` matcher (shares the ``**/``-aware glob the
  sensitive-path checks use), and
- the orchestrator PR re-check that escalates a tests-promised-but-none-shipped
  diff to REVIEW_REQUIRED, passes when a test file is touched, is inert when the
  plan promised no tests, and is disabled by the ``enforce_plan_tests`` kill
  switch (which defaults *off*, so the default behaviour is unchanged even though
  the template planner promises a unit test per acceptance criterion).
"""

from __future__ import annotations

import json

import pytest

from foundry.agents.manual import InMemoryFakeProvider
from foundry.config import DEFAULT_TEST_PATH_GLOBS
from foundry.db import (
    FoundryAuditEvent,
    create_all,
    make_engine,
    make_session_factory,
)
from foundry.db.models import AuditEventType
from foundry.engines.planner import TemplatePlanner
from foundry.engines.risk import diff_touches_tests
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.common import PRStatus, RunStatus
from foundry.schemas.plan import TestPlan as _TestPlan
from foundry.schemas.pr import PullRequestState
from foundry.schemas.ticket import RawTicket

READY_DESC = """\
Customers want to favourite items.

Acceptance Criteria:
- A favourites button exists
- Favourites persist across sessions
"""


# -- pure matcher ----------------------------------------------------------------


def test_no_globs_is_inert() -> None:
    """No configured test convention recognises nothing as a test."""
    assert diff_touches_tests(["tests/test_x.py"], []) is False


def test_whitespace_only_globs_is_inert() -> None:
    assert diff_touches_tests(["tests/test_x.py"], ["  ", ""]) is False


def test_python_test_file_matches() -> None:
    assert diff_touches_tests(["src/foo/test_bar.py"], DEFAULT_TEST_PATH_GLOBS)
    assert diff_touches_tests(["test_top.py"], DEFAULT_TEST_PATH_GLOBS)
    assert diff_touches_tests(["src/foo/bar_test.py"], DEFAULT_TEST_PATH_GLOBS)


def test_tests_directory_matches() -> None:
    assert diff_touches_tests(["tests/unit/whatever.py"], DEFAULT_TEST_PATH_GLOBS)
    assert diff_touches_tests(
        ["src/pkg/__tests__/widget.test.ts"], DEFAULT_TEST_PATH_GLOBS
    )


def test_js_test_specs_match() -> None:
    assert diff_touches_tests(["src/widget.test.ts"], DEFAULT_TEST_PATH_GLOBS)
    assert diff_touches_tests(["src/widget.spec.js"], DEFAULT_TEST_PATH_GLOBS)


def test_non_test_files_do_not_match() -> None:
    assert (
        diff_touches_tests(
            ["src/features/favourites/index.ts", "README.md"],
            DEFAULT_TEST_PATH_GLOBS,
        )
        is False
    )


def test_any_test_among_many_matches() -> None:
    assert diff_touches_tests(
        ["src/app.py", "tests/test_app.py"], DEFAULT_TEST_PATH_GLOBS
    )


# -- orchestrator integration ----------------------------------------------------


class _TestPlanPlanner(TemplatePlanner):
    """Template planner that also declares a ``test_plan`` - a stand-in for a
    code-aware (LLM) planner that promises tests."""

    def __init__(self, test_plan: _TestPlan | None = None) -> None:
        self._test_plan = test_plan or _TestPlan()

    def plan(self, ticket, analysis, context, risk):
        plan = super().plan(ticket, analysis, context, risk)
        return plan.model_copy(update={"test_plan": self._test_plan})


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


def _ready_ticket() -> RawTicket:
    return RawTicket(
        issue_id="i-1",
        issue_key="LIN-123",
        title="Add customer favourites",
        description=READY_DESC,
        known_repositories=["customer-web"],
    )


def _pr(**overrides) -> PullRequestState:
    base = dict(
        repo="customer-web",
        pr_number=7,
        url="https://github.com/example/customer-web/pull/7",
        branch="foundry/lin-123-add-customer-favourites",
        status=PRStatus.OPEN,
        files_changed=["src/features/favourites/index.ts"],
    )
    base.update(overrides)
    return PullRequestState(**base)


def _dispatched_run(session_factory, **orch_kwargs) -> tuple:
    provider = InMemoryFakeProvider()
    orch = FoundryOrchestrator(session_factory, provider=provider, **orch_kwargs)
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    provider.run(job.job_id)
    return orch, run_id


def _audit_meta(session_factory, run_id, event_type):
    with session_factory() as s:
        events = [
            e
            for e in s.query(FoundryAuditEvent).filter_by(run_id=run_id)
            if e.event_type is event_type
        ]
        return [json.loads(e.metadata_json) if e.metadata_json else {} for e in events]


def test_promised_tests_but_none_shipped_escalates(session_factory) -> None:
    """The plan promised unit tests; the diff ships only product code."""
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_TestPlanPlanner(_TestPlan(unit_tests=["favourites persist"])),
        enforce_plan_tests=True,
    )
    pr = _pr(files_changed=["src/features/favourites/index.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.REVIEW_REQUIRED

    metas = _audit_meta(session_factory, run_id, AuditEventType.RISK_ESCALATED)
    assert any(m.get("category") == "plan_tests_missing" for m in metas)


def test_tests_present_passes(session_factory) -> None:
    """A diff that includes a test file satisfies the promise."""
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_TestPlanPlanner(_TestPlan(integration_tests=["end to end"])),
        enforce_plan_tests=True,
    )
    pr = _pr(
        files_changed=[
            "src/features/favourites/index.ts",
            "tests/test_favourites.py",
        ]
    )
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_no_promised_tests_is_inert(session_factory) -> None:
    """A plan promising no tests never escalates, even with the switch on."""
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_TestPlanPlanner(_TestPlan(manual_checks=["click around"])),
        enforce_plan_tests=True,
    )
    pr = _pr(files_changed=["src/features/favourites/index.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_kill_switch_off_disables_check(session_factory) -> None:
    """Default off: a tests-promised-but-none-shipped diff rides through."""
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_TestPlanPlanner(_TestPlan(unit_tests=["favourites persist"])),
        # enforce_plan_tests defaults to False - assert the default is off.
    )
    pr = _pr(files_changed=["src/features/favourites/index.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_template_planner_engages_when_enabled(session_factory) -> None:
    """The default (template) planner promises a unit test per AC, so with the
    switch *on* a no-tests diff escalates - inertness here rides on the kill
    switch defaulting off (see ``test_kill_switch_off_disables_check``), not on an
    empty plan field as it does for the scope checks."""
    orch, run_id = _dispatched_run(session_factory, enforce_plan_tests=True)
    pr = _pr(files_changed=["src/features/favourites/index.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.REVIEW_REQUIRED


def test_custom_test_path_globs_escalates_off_convention(session_factory) -> None:
    """A standard ``tests/`` file is *not* recognised under a bespoke convention,
    so a diff lacking a ``spec/`` file still escalates."""
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_TestPlanPlanner(_TestPlan(unit_tests=["x"])),
        enforce_plan_tests=True,
        test_path_globs=["spec/**"],
    )
    pr = _pr(files_changed=["src/app.rb", "tests/app_test.rb"])
    assert orch.record_pr(run_id, pr) is RunStatus.REVIEW_REQUIRED


def test_custom_test_path_globs_passes_on_convention(session_factory) -> None:
    """A file matching the bespoke convention satisfies the promise."""
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_TestPlanPlanner(_TestPlan(unit_tests=["x"])),
        enforce_plan_tests=True,
        test_path_globs=["spec/**"],
    )
    pr = _pr(files_changed=["src/app.rb", "spec/app_spec.rb"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN
