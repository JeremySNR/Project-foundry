"""Tests for the deterministic reference intelligence engines."""

from __future__ import annotations

from foundry.engines import (
    HeuristicAnalyzer,
    HeuristicRiskClassifier,
    StaticContextEnricher,
    TemplatePlanner,
    branch_name_for,
)
from foundry.schemas.common import (
    AgentMode,
    ApprovalRole,
    ImplementationReadiness,
    OverallRisk,
    WorkType,
)
from foundry.schemas.ticket import LinkedResource, RawTicket

READY_DESC = """\
We want customers to be able to favourite items.

Acceptance Criteria:
- A favourites button exists on each item
- Favourites persist across sessions
"""


def _ready_ticket(**overrides) -> RawTicket:
    base = dict(
        issue_id="i-1",
        issue_key="LIN-123",
        title="Add customer favourites",
        description=READY_DESC,
        known_repositories=["customer-web"],
    )
    base.update(overrides)
    return RawTicket(**base)


# -- analyzer -----------------------------------------------------------------


def test_clear_feature_with_ac_is_ready() -> None:
    analysis = HeuristicAnalyzer().analyse(_ready_ticket())
    assert analysis.work_type is WorkType.FEATURE
    assert analysis.implementation_readiness is ImplementationReadiness.READY
    assert len(analysis.acceptance_criteria) == 2
    assert analysis.is_ready_to_build is True


def test_vague_feature_needs_clarification() -> None:
    ticket = RawTicket(issue_id="i", issue_key="LIN-9", title="Make it nicer")
    analysis = HeuristicAnalyzer().analyse(ticket)
    assert analysis.implementation_readiness is ImplementationReadiness.NEEDS_CLARIFICATION
    assert "acceptance criteria" in analysis.missing_information


def test_bug_without_repro_needs_clarification() -> None:
    ticket = RawTicket(
        issue_id="i",
        issue_key="LIN-7",
        title="Checkout button is broken",
        description="It errors sometimes.",
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    assert analysis.work_type is WorkType.BUG
    assert "reproduction steps" in analysis.missing_information
    assert analysis.is_ready_to_build is False


def test_empty_ticket_is_not_buildable() -> None:
    analysis = HeuristicAnalyzer().analyse(
        RawTicket(issue_id="i", issue_key="LIN-0", title="x")
    )
    assert analysis.is_ready_to_build is False
    assert analysis.ambiguity_score >= 50


def test_question_is_not_suitable() -> None:
    ticket = RawTicket(
        issue_id="i",
        issue_key="LIN-Q",
        title="Question: how do we handle refunds?",
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    assert analysis.work_type is WorkType.QUESTION
    assert analysis.implementation_readiness is ImplementationReadiness.NOT_SUITABLE


def test_analysis_is_deterministic() -> None:
    a = HeuristicAnalyzer().analyse(_ready_ticket())
    b = HeuristicAnalyzer().analyse(_ready_ticket())
    assert a == b


# -- enrichment ---------------------------------------------------------------


def test_known_repo_is_confident() -> None:
    ticket = _ready_ticket()
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    assert context.has_confident_repository() is True
    assert context.best_repository.repo == "customer-web"


def test_no_repo_signal_yields_unknowns() -> None:
    ticket = RawTicket(issue_id="i", issue_key="LIN-3", title="Do a thing", description="x")
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    assert context.has_confident_repository() is False
    assert context.unknowns


def test_catalog_keyword_match() -> None:
    ticket = RawTicket(
        issue_id="i",
        issue_key="LIN-4",
        title="Improve favourites",
        description="Acceptance Criteria:\n- favourites work",
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    enricher = StaticContextEnricher(repo_catalog={"customer-web": ["favourites"]})
    context = enricher.enrich(ticket, analysis)
    assert any(c.repo == "customer-web" for c in context.candidate_repositories)


def test_linked_pr_surfaces_related_pr() -> None:
    ticket = _ready_ticket(
        linked_resources=[
            LinkedResource(kind="github_pr", url="https://github.com/x/y/pull/5", repo="y")
        ]
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    assert "https://github.com/x/y/pull/5" in context.related_prs


# -- risk ---------------------------------------------------------------------


def test_clean_feature_is_low_risk() -> None:
    ticket = _ready_ticket()
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    risk = HeuristicRiskClassifier().classify(ticket, analysis, context)
    assert risk.overall_risk is OverallRisk.LOW
    assert risk.allowed_agent_mode is AgentMode.DRAFT_PR


def test_auth_ticket_flags_sensitive_and_requires_engineering() -> None:
    ticket = _ready_ticket(
        title="Change login session token handling",
        description="Acceptance Criteria:\n- auth tokens rotate\nThis touches auth/login.",
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    risk = HeuristicRiskClassifier().classify(ticket, analysis, context)
    assert risk.sensitive_areas.auth is True
    assert risk.overall_risk is OverallRisk.HIGH
    assert ApprovalRole.ENGINEERING in risk.required_approvals
    assert risk.allowed_agent_mode is AgentMode.HUMAN_ONLY


def test_no_repo_match_is_blocked_risk() -> None:
    ticket = RawTicket(
        issue_id="i",
        issue_key="LIN-5",
        title="Add favourites",
        description="Acceptance Criteria:\n- it works",
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)  # no repo signal
    risk = HeuristicRiskClassifier().classify(ticket, analysis, context)
    assert risk.overall_risk is OverallRisk.BLOCKED


# -- planner ------------------------------------------------------------------


def test_ready_plan_has_agent_instructions() -> None:
    ticket = _ready_ticket()
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    risk = HeuristicRiskClassifier().classify(ticket, analysis, context)
    plan = TemplatePlanner().plan(ticket, analysis, context, risk)
    assert plan.agent_instructions is not None
    assert "LIN-123" in plan.agent_instructions
    assert len(plan.implementation_steps) == 2
    assert plan.affected_repositories == ["customer-web"]


def test_not_ready_plan_has_no_agent_instructions() -> None:
    ticket = RawTicket(issue_id="i", issue_key="LIN-6", title="Vague")
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    risk = HeuristicRiskClassifier().classify(ticket, analysis, context)
    plan = TemplatePlanner().plan(ticket, analysis, context, risk)
    assert plan.agent_instructions is None


def test_branch_name_is_sanitised() -> None:
    ticket = _ready_ticket(title="Add Customer Favourites!!!")
    assert branch_name_for(ticket) == "foundry/lin-123-add-customer-favourites"
