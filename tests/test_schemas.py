"""Schema contract tests for the run artifacts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.schemas import (
    CandidateRepository,
    ContextBundle,
    DeliveryPlan,
    ImplementationReadiness,
    TicketAnalysis,
    WorkType,
)


def test_ready_analysis_is_buildable(ready_analysis: TicketAnalysis) -> None:
    assert ready_analysis.is_ready_to_build is True


def test_ready_without_acceptance_criteria_is_not_buildable() -> None:
    analysis = TicketAnalysis(
        ticket_id="LIN-1",
        title="Vague idea",
        work_type=WorkType.FEATURE,
        summary="Do something nice",
        acceptance_criteria=[],
        ambiguity_score=80,
        implementation_readiness=ImplementationReadiness.READY,
        confidence=50,
    )
    # Even if the LLM claims "ready", missing acceptance criteria blocks build.
    assert analysis.is_ready_to_build is False


def test_invalid_llm_output_is_rejected() -> None:
    # Unknown field (extra="forbid") and out-of-range score both rejected.
    with pytest.raises(ValidationError):
        TicketAnalysis(
            ticket_id="LIN-2",
            title="x",
            work_type=WorkType.BUG,
            summary="s",
            ambiguity_score=200,  # out of range
            implementation_readiness=ImplementationReadiness.READY,
            confidence=50,
        )

    with pytest.raises(ValidationError):
        TicketAnalysis.model_validate(
            {
                "ticket_id": "LIN-3",
                "title": "x",
                "work_type": "feature",
                "summary": "s",
                "ambiguity_score": 10,
                "implementation_readiness": "ready",
                "confidence": 50,
                "hallucinated_field": True,  # forbidden extra
            }
        )


def test_context_best_repository(confident_context: ContextBundle) -> None:
    assert confident_context.best_repository is not None
    assert confident_context.best_repository.repo == "customer-web"


def test_context_no_repo_match_is_not_confident() -> None:
    bundle = ContextBundle()
    assert bundle.best_repository is None
    assert bundle.has_confident_repository() is False


def test_context_low_confidence_blocks() -> None:
    bundle = ContextBundle(
        candidate_repositories=[
            CandidateRepository(repo="maybe", confidence=40, reason="weak match")
        ]
    )
    assert bundle.has_confident_repository() is False


def test_context_ambiguous_multiple_repos_is_not_confident() -> None:
    bundle = ContextBundle(
        candidate_repositories=[
            CandidateRepository(repo="a", confidence=85, reason="match"),
            CandidateRepository(repo="b", confidence=80, reason="also match"),
        ]
    )
    # Two repos above threshold is ambiguous -> needs human confirmation.
    assert bundle.has_confident_repository() is False


def test_delivery_plan_validates(delivery_plan: DeliveryPlan) -> None:
    # round-trips through JSON schema without loss
    dumped = delivery_plan.model_dump_json()
    restored = DeliveryPlan.model_validate_json(dumped)
    assert restored == delivery_plan
    assert restored.implementation_steps[0].step == 1


def test_delivery_plan_step_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        DeliveryPlan.model_validate(
            {
                "goal": "g",
                "implementation_steps": [
                    {"step": 0, "description": "d", "expected_output": "o"}
                ],
            }
        )
