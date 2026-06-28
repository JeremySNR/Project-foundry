"""Plan-satisfaction judge (issue #169, slice 3): the headline plan-aware gate.

Beyond *file containment* (plan-scope drift / out-of-scope), this asks whether
the diff actually *satisfies* the approved plan's intent. Two layers:

- the pure ``LlmPlanSatisfactionJudge`` over a ``FakeStructuredLLM`` (offline, no
  network): a "not satisfied" verdict escalates, a "satisfied" verdict does not,
  and any LLM failure degrades to a no-op verdict; and
- the orchestrator PR re-check that escalates an unsatisfied diff to
  REVIEW_REQUIRED (audited ``plan_unsatisfied``), is inert when no judge is
  injected (the default ``plan_satisfaction.provider: none``), degrades to a
  no-op on an LLM outage, and runs *last* so the cheap deterministic gates
  (forbidden paths, scope drift) short-circuit before any LLM call.
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
from foundry.engines.llm import FakeStructuredLLM
from foundry.engines.llm_plan_satisfaction import (
    LlmPlanSatisfactionJudge,
    build_llm_plan_satisfaction_judge,
)
from foundry.engines.planner import TemplatePlanner
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.common import PRStatus, RunStatus
from foundry.schemas.plan import DeliveryPlan
from foundry.schemas.pr import PullRequestState
from foundry.schemas.ticket import RawTicket

READY_DESC = """\
Customers want to favourite items.

Acceptance Criteria:
- A favourites button exists
- Favourites persist across sessions
"""


def _judge(*responses) -> tuple[LlmPlanSatisfactionJudge, FakeStructuredLLM]:
    llm = FakeStructuredLLM(list(responses))
    return LlmPlanSatisfactionJudge(llm), llm


# -- pure judge ------------------------------------------------------------------


def _plan() -> DeliveryPlan:
    return DeliveryPlan(
        goal="Add a favourites feature",
        scope=["src/features/favourites"],
        out_of_scope=["src/legacy"],
        expected_files_or_areas=["src/features/favourites"],
    )


def _pr(**overrides) -> PullRequestState:
    base = dict(
        repo="customer-web",
        pr_number=7,
        url="https://github.com/example/customer-web/pull/7",
        branch="foundry/lin-123-add-customer-favourites",
        status=PRStatus.OPEN,
        files_changed=["src/features/favourites/index.ts"],
        summary="Adds a favourites button and persistence.",
    )
    base.update(overrides)
    return PullRequestState(**base)


def test_satisfied_verdict_does_not_escalate() -> None:
    judge, llm = _judge({"satisfied": True, "reason": ""})
    verdict = judge.judge(_plan(), _pr())
    assert verdict.satisfied is True
    assert verdict.degraded is False
    # The plan goal and PR summary are both rendered into the prompt the model saw.
    assert "Add a favourites feature" in llm.calls[0]["user"]
    assert "favourites button and persistence" in llm.calls[0]["user"]


def test_unsatisfied_verdict_carries_reason() -> None:
    judge, _ = _judge(
        {"satisfied": False, "reason": "goal unaddressed: no persistence layer"}
    )
    verdict = judge.judge(_plan(), _pr())
    assert verdict.satisfied is False
    assert verdict.degraded is False
    assert "persistence" in verdict.reason


def test_llm_error_degrades_to_noop() -> None:
    """An empty canned-response list makes FakeStructuredLLM raise LLMError; the
    judge must degrade to a no-op verdict rather than propagate the error."""
    judge, _ = _judge()  # no responses -> LLMError on first call
    verdict = judge.judge(_plan(), _pr())
    assert verdict.degraded is True
    # Degraded verdicts never escalate (satisfied stays True so the orchestrator
    # treats it as "nothing to escalate").
    assert verdict.satisfied is True


def test_schema_violation_retries_then_degrades() -> None:
    """A malformed response is retried with corrective feedback; exhausting the
    attempts degrades to a no-op rather than raising."""
    judge, llm = _judge({"reason": "missing the required satisfied field"})
    verdict = judge.judge(_plan(), _pr())
    assert verdict.degraded is True
    # First attempt validated-and-failed, second attempt found no canned response.
    assert len(llm.calls) == 2
    assert "rejected by the schema validator" in llm.calls[1]["user"]


def test_factory_builds_judge() -> None:
    judge = build_llm_plan_satisfaction_judge(model="gpt-5.5", client=object())
    assert isinstance(judge, LlmPlanSatisfactionJudge)


# -- orchestrator integration ----------------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


class _ScopedPlanner(TemplatePlanner):
    """Template planner that also declares an expected/out-of-scope file set - a
    stand-in for a code-aware planner that scopes the diff."""

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


def _ready_ticket() -> RawTicket:
    return RawTicket(
        issue_id="i-1",
        issue_key="LIN-123",
        title="Add customer favourites",
        description=READY_DESC,
        known_repositories=["customer-web"],
    )


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


def test_no_judge_is_inert(session_factory) -> None:
    """The default deployment injects no judge, so the check never engages and
    the historical behaviour is byte-for-byte preserved."""
    orch, run_id = _dispatched_run(session_factory)
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN
    assert _audit_meta(session_factory, run_id, AuditEventType.RISK_ESCALATED) == []


def test_satisfied_diff_rides_through(session_factory) -> None:
    judge, llm = _judge({"satisfied": True, "reason": ""})
    orch, run_id = _dispatched_run(session_factory, plan_satisfaction_judge=judge)
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN
    # The judge was consulted on the otherwise-clean diff.
    assert len(llm.calls) == 1


def test_unsatisfied_diff_escalates(session_factory) -> None:
    judge, _ = _judge(
        {"satisfied": False, "reason": "the favourites goal is not addressed"}
    )
    orch, run_id = _dispatched_run(session_factory, plan_satisfaction_judge=judge)
    assert orch.record_pr(run_id, _pr()) is RunStatus.REVIEW_REQUIRED

    metas = _audit_meta(session_factory, run_id, AuditEventType.RISK_ESCALATED)
    meta = next(m for m in metas if m.get("category") == "plan_unsatisfied")
    assert meta["source"] == "llm"
    assert "favourites goal" in meta["reason"]


def test_llm_outage_does_not_escalate(session_factory) -> None:
    """A judge whose LLM is down degrades to a no-op: the run rides through rather
    than being blocked *or* released by the outage."""
    judge, _ = _judge()  # empty -> LLMError -> degraded verdict
    orch, run_id = _dispatched_run(session_factory, plan_satisfaction_judge=judge)
    assert orch.record_pr(run_id, _pr()) is RunStatus.PR_OPEN
    assert _audit_meta(session_factory, run_id, AuditEventType.RISK_ESCALATED) == []


def test_judge_runs_last_after_deterministic_gates(session_factory) -> None:
    """A forbidden-path BLOCK (and any deterministic escalation) short-circuits
    before the LLM judge is ever consulted - no wasted LLM spend, and the harder
    gate wins."""
    judge, llm = _judge({"satisfied": False, "reason": "would escalate if reached"})
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_ScopedPlanner(out_of_scope=["migrations"]),
        plan_satisfaction_judge=judge,
    )
    pr = _pr(files_changed=["migrations/0002_add_table.sql"])
    assert orch.record_pr(run_id, pr) is RunStatus.BLOCKED
    # The judge never ran - the forbidden-path block fired first.
    assert llm.calls == []


def test_judge_not_consulted_when_scope_drift_fires(session_factory) -> None:
    """Plan-scope drift escalates before the judge runs, so a satisfied/unsatisfied
    LLM verdict is irrelevant on a drifting diff and no LLM call is spent."""
    judge, llm = _judge({"satisfied": False, "reason": "irrelevant"})
    orch, run_id = _dispatched_run(
        session_factory,
        planner=_ScopedPlanner(expected=["src/features/favourites"]),
        plan_satisfaction_judge=judge,
    )
    pr = _pr(files_changed=["src/unrelated/widget.ts"])
    assert orch.record_pr(run_id, pr) is RunStatus.REVIEW_REQUIRED

    metas = _audit_meta(session_factory, run_id, AuditEventType.RISK_ESCALATED)
    categories = [m.get("category") for m in metas]
    assert "plan_scope_drift" in categories
    assert "plan_unsatisfied" not in categories
    assert llm.calls == []
