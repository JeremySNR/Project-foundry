"""Plan-scope drift escalation: the consumer of the LLM planner's
``DeliveryPlan.expected_files_or_areas`` (the long-promised plan-vs-diff check).

Two layers:
- the pure ``files_outside_scope`` matcher (exact / glob / prefix / bare-area), and
- the orchestrator PR re-check that escalates a straying diff to REVIEW_REQUIRED,
  is inert for the default (template) planner that declares no expected areas,
  and is disabled by the ``enforce_plan_scope`` kill switch.
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
from foundry.engines.risk import files_outside_scope
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
    """No declared scope (the template planner's default) checks nothing."""
    assert files_outside_scope([], ["src/anything.ts", "deploy/helm.yaml"]) == []


def test_whitespace_only_scope_is_inert() -> None:
    assert files_outside_scope(["  ", "", "  /"], ["src/x.ts"]) == []


def test_exact_file_is_in_scope() -> None:
    assert files_outside_scope(["src/a.ts"], ["src/a.ts"]) == []


def test_directory_prefix_covers_nested_files() -> None:
    scope = ["src/features/favourites"]
    assert files_outside_scope(scope, ["src/features/favourites/index.ts"]) == []
    # A sibling directory sharing a name prefix is *not* covered.
    assert files_outside_scope(scope, ["src/features/favourites-old/x.ts"]) == [
        "src/features/favourites-old/x.ts"
    ]


def test_glob_entry_matches() -> None:
    assert files_outside_scope(["src/**/*.ts"], ["src/a/b/c.ts"]) == []
    assert files_outside_scope(["src/*.py"], ["src/app.py"]) == []


def test_bare_area_name_matches_path_segment() -> None:
    """An LLM planner naming an *area* ("favourites") rather than a file path
    still scopes the diff: the area matches as a whole path segment."""
    scope = ["favourites"]
    assert files_outside_scope(scope, ["src/features/favourites/index.ts"]) == []
    # A mere substring is not a segment match, so it is reported as drift.
    assert files_outside_scope(scope, ["src/myfavourites.ts"]) == [
        "src/myfavourites.ts"
    ]


def test_straying_files_are_returned() -> None:
    scope = ["src/features/favourites"]
    drift = files_outside_scope(
        scope,
        ["src/features/favourites/index.ts", "src/auth/session.ts", "README.md"],
    )
    assert drift == ["src/auth/session.ts", "README.md"]


def test_entry_normalisation() -> None:
    """Leading ``./`` and trailing ``/`` are trimmed before matching."""
    assert files_outside_scope(["./src/api/"], ["src/api/app.py"]) == []


def test_file_in_scope_if_any_entry_matches() -> None:
    scope = ["src/api", "docs"]
    assert files_outside_scope(scope, ["docs/guide.md", "src/api/app.py"]) == []


# -- orchestrator integration ----------------------------------------------------


class _ScopedPlanner(TemplatePlanner):
    """Template planner that also declares ``expected_files_or_areas`` - i.e. a
    stand-in for a code-aware (LLM) planner that scopes the diff."""

    def __init__(self, expected: list[str]) -> None:
        self._expected = expected

    def plan(self, ticket, analysis, context, risk):
        plan = super().plan(ticket, analysis, context, risk)
        return plan.model_copy(update={"expected_files_or_areas": self._expected})


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


def test_diff_within_plan_scope_does_not_escalate(session_factory) -> None:
    orch, run_id = _dispatched_run(
        session_factory, planner=_ScopedPlanner(["src/features/favourites"])
    )
    pr = _pr(files_changed=["src/features/favourites/index.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_diff_outside_plan_scope_escalates(session_factory) -> None:
    """The plan scoped the favourites area; the agent also touched an unrelated
    config file. Hand it to a human."""
    orch, run_id = _dispatched_run(
        session_factory, planner=_ScopedPlanner(["src/features/favourites"])
    )
    pr = _pr(
        files_changed=[
            "src/features/favourites/index.ts",
            "src/unrelated/widget.ts",
        ]
    )
    assert orch.record_pr(run_id, pr) is RunStatus.REVIEW_REQUIRED

    metas = _audit_meta(session_factory, run_id, AuditEventType.RISK_ESCALATED)
    meta = next(m for m in metas if m.get("category") == "plan_scope_drift")
    assert meta["unexpected_files"] == ["src/unrelated/widget.ts"]


def test_plan_scope_drift_kill_switch_disables_check(session_factory) -> None:
    """With ``enforce_plan_scope=False`` a straying diff rides through as before."""
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_ScopedPlanner(["src/features/favourites"]),
        enforce_plan_scope=False,
    )
    pr = _pr(files_changed=["src/totally/elsewhere.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_template_planner_declares_no_scope_so_check_is_inert(session_factory) -> None:
    """Regression: the default (template) planner declares no expected areas, so
    the check never engages and the historical behaviour is byte-for-byte
    preserved even for an arbitrary diff."""
    orch, run_id = _dispatched_run(session_factory)  # default TemplatePlanner
    pr = _pr(files_changed=["src/totally/elsewhere.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


def test_forbidden_path_still_takes_precedence_over_plan_drift(session_factory) -> None:
    """A forbidden path is a hard BLOCK (sticky, never retried) and must win over
    the softer plan-drift escalation even when both would fire."""
    orch, run_id = _dispatched_run(
        session_factory, planner=_ScopedPlanner(["src/features/favourites"])
    )
    pr = _pr(files_changed=["migrations/0002_add_table.sql"])
    assert orch.record_pr(run_id, pr) is RunStatus.BLOCKED
