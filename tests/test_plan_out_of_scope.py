"""Plan out-of-scope escalation (issue #169, slice 1): the out-of-scope twin of
the plan-scope drift check. It consumes the approved ``DeliveryPlan.out_of_scope``
(paths/areas the plan promised *not* to touch) - a stronger off-plan signal than
mere scope drift.

Two layers:
- the pure ``files_matching_scope`` matcher (the inverse of ``files_outside_scope``,
  sharing its exact / glob / prefix / bare-area convention), and
- the orchestrator PR re-check that escalates a diff reaching an out-of-scope path
  to REVIEW_REQUIRED, is inert for the default (template) planner that declares no
  out-of-scope entries, is disabled by the ``enforce_plan_out_of_scope`` kill
  switch, takes precedence over plan-scope drift, and yields to a forbidden-path
  block.
"""

from __future__ import annotations

import json

import pytest

from foundry.agents.manual import InMemoryFakeProvider
from foundry.db import (
    FoundryAuditEvent,
    create_all,
    make_engine,
    make_session_factory,
)
from foundry.db.models import AuditEventType
from foundry.engines.planner import TemplatePlanner
from foundry.engines.risk import files_matching_scope
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.common import PRStatus, RunStatus
from foundry.schemas.pr import PullRequestState
from foundry.schemas.ticket import RawTicket

READY_DESC = """\
Customers want to favourite items.

Acceptance Criteria:
- A favourites button exists
- Favourites persist across sessions
"""


# -- pure matcher ----------------------------------------------------------------


def test_empty_scope_is_inert() -> None:
    """No declared out-of-scope (the template planner's default) matches nothing."""
    assert files_matching_scope([], ["src/anything.ts", "deploy/helm.yaml"]) == []


def test_whitespace_only_scope_is_inert() -> None:
    assert files_matching_scope(["  ", "", "  /"], ["src/x.ts"]) == []


def test_exact_file_matches() -> None:
    assert files_matching_scope(["src/a.ts"], ["src/a.ts"]) == ["src/a.ts"]


def test_directory_prefix_matches_nested_files() -> None:
    scope = ["src/billing"]
    assert files_matching_scope(scope, ["src/billing/charge.ts"]) == [
        "src/billing/charge.ts"
    ]
    # A sibling directory sharing a name prefix is *not* matched.
    assert files_matching_scope(scope, ["src/billing-docs/x.ts"]) == []


def test_glob_entry_matches() -> None:
    assert files_matching_scope(["**/*.sql"], ["migrations/0001.sql"]) == [
        "migrations/0001.sql"
    ]


def test_bare_area_name_matches_path_segment() -> None:
    scope = ["billing"]
    assert files_matching_scope(scope, ["src/features/billing/charge.ts"]) == [
        "src/features/billing/charge.ts"
    ]
    # A mere substring is not a segment match, so it is not flagged.
    assert files_matching_scope(scope, ["src/mybilling.ts"]) == []


def test_only_matching_files_are_returned() -> None:
    scope = ["src/billing"]
    hits = files_matching_scope(
        scope,
        ["src/features/favourites/index.ts", "src/billing/charge.ts"],
    )
    assert hits == ["src/billing/charge.ts"]


def test_entry_normalisation() -> None:
    """Leading ``./`` and trailing ``/`` are trimmed before matching."""
    assert files_matching_scope(["./src/billing/"], ["src/billing/charge.ts"]) == [
        "src/billing/charge.ts"
    ]


# -- orchestrator integration ----------------------------------------------------


class _ScopedPlanner(TemplatePlanner):
    """Template planner that also declares ``expected_files_or_areas`` and/or
    ``out_of_scope`` - a stand-in for a code-aware (LLM) planner that scopes the
    diff."""

    def __init__(
        self,
        expected: list[str] | None = None,
        out_of_scope: list[str] | None = None,
    ) -> None:
        self._expected = expected or []
        self._out_of_scope = out_of_scope or []

    def plan(self, ticket, analysis, context, risk):
        plan = super().plan(ticket, analysis, context, risk)
        return plan.model_copy(
            update={
                "expected_files_or_areas": self._expected,
                "out_of_scope": self._out_of_scope,
            }
        )


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


def test_diff_avoiding_out_of_scope_does_not_escalate(session_factory) -> None:
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_ScopedPlanner(out_of_scope=["src/legacy"]),
    )
    pr = _pr(files_changed=["src/features/favourites/index.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_diff_touching_out_of_scope_escalates(session_factory) -> None:
    """The plan promised not to touch the legacy area; the agent changed it anyway."""
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_ScopedPlanner(out_of_scope=["src/legacy"]),
    )
    pr = _pr(
        files_changed=[
            "src/features/favourites/index.ts",
            "src/legacy/old.ts",
        ]
    )
    assert orch.record_pr(run_id, pr) is RunStatus.REVIEW_REQUIRED

    metas = _audit_meta(session_factory, run_id, AuditEventType.RISK_ESCALATED)
    meta = next(m for m in metas if m.get("category") == "plan_out_of_scope")
    assert meta["out_of_scope_files"] == ["src/legacy/old.ts"]


def test_kill_switch_disables_check(session_factory) -> None:
    """With ``enforce_plan_out_of_scope=False`` an off-limits diff rides through."""
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_ScopedPlanner(out_of_scope=["src/legacy"]),
        enforce_plan_out_of_scope=False,
    )
    pr = _pr(files_changed=["src/legacy/old.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_template_planner_declares_nothing_so_check_is_inert(session_factory) -> None:
    """Regression: the default (template) planner declares no out-of-scope, so the
    check never engages and the historical behaviour is preserved."""
    orch, run_id = _dispatched_run(session_factory)  # default TemplatePlanner
    pr = _pr(files_changed=["src/legacy/old.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_out_of_scope_takes_precedence_over_plan_scope_drift(session_factory) -> None:
    """A file that is both outside the expected scope *and* explicitly out of
    scope escalates as ``plan_out_of_scope`` (the stronger signal), and no
    duplicate ``plan_scope_drift`` event is recorded for the same diff."""
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_ScopedPlanner(
            expected=["src/features/favourites"],
            out_of_scope=["src/legacy"],
        ),
    )
    pr = _pr(
        files_changed=[
            "src/features/favourites/index.ts",
            "src/legacy/old.ts",
        ]
    )
    assert orch.record_pr(run_id, pr) is RunStatus.REVIEW_REQUIRED

    metas = _audit_meta(session_factory, run_id, AuditEventType.RISK_ESCALATED)
    categories = [m.get("category") for m in metas]
    assert "plan_out_of_scope" in categories
    assert "plan_scope_drift" not in categories


def test_scope_drift_still_fires_when_not_out_of_scope(session_factory) -> None:
    """A file outside the expected scope but *not* on the out-of-scope list still
    escalates via the existing plan-scope drift check - out-of-scope is additive,
    it doesn't suppress drift."""
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_ScopedPlanner(
            expected=["src/features/favourites"],
            out_of_scope=["src/legacy"],
        ),
    )
    pr = _pr(
        files_changed=[
            "src/features/favourites/index.ts",
            "src/unrelated/widget.ts",
        ]
    )
    assert orch.record_pr(run_id, pr) is RunStatus.REVIEW_REQUIRED

    metas = _audit_meta(session_factory, run_id, AuditEventType.RISK_ESCALATED)
    categories = [m.get("category") for m in metas]
    assert "plan_scope_drift" in categories
    assert "plan_out_of_scope" not in categories


def test_forbidden_path_still_takes_precedence(session_factory) -> None:
    """A forbidden path is a hard BLOCK (sticky, never retried) and must win over
    the softer out-of-scope escalation even when both would fire."""
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_ScopedPlanner(out_of_scope=["migrations"]),
    )
    pr = _pr(files_changed=["migrations/0002_add_table.sql"])
    assert orch.record_pr(run_id, pr) is RunStatus.BLOCKED
