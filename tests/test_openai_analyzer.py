"""Tests for the OpenAI-backed analyzer (using a fake StructuredLLM, no network)."""

from __future__ import annotations

import pytest

from foundry.engines import FakeStructuredLLM, LLMError, OpenAITicketAnalyzer
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.common import ImplementationReadiness, WorkType
from foundry.schemas.ticket import RawTicket


def _ticket(**overrides) -> RawTicket:
    base = dict(
        issue_id="i-1",
        issue_key="LIN-123",
        title="Add customer favourites",
        description="When a customer taps the heart, the item should save.",
    )
    base.update(overrides)
    return RawTicket(**base)


def _valid_response(**overrides) -> dict:
    base = {
        "work_type": "feature",
        "summary": "Let customers favourite items.",
        "user_problem": "Customers cannot save items.",
        "business_value": "Retention.",
        "acceptance_criteria": ["A favourites control exists", "Favourites persist"],
        "missing_information": [],
        "assumptions": ["Auth already exists"],
        "ambiguity_score": 15,
        "implementation_readiness": "ready",
        "confidence": 85,
    }
    base.update(overrides)
    return base


def test_analyse_returns_validated_analysis() -> None:
    llm = FakeStructuredLLM([_valid_response()])
    analysis = OpenAITicketAnalyzer(llm).analyse(_ticket())
    assert analysis.work_type is WorkType.FEATURE
    assert analysis.implementation_readiness is ImplementationReadiness.READY
    assert analysis.is_ready_to_build is True


def test_identity_fields_come_from_ticket_not_model() -> None:
    # Model tries to hallucinate a different id/title; analyzer overrides them.
    llm = FakeStructuredLLM([_valid_response(ticket_id="WRONG", title="hallucinated")])
    analysis = OpenAITicketAnalyzer(llm).analyse(_ticket())
    assert analysis.ticket_id == "LIN-123"
    assert analysis.title == "Add customer favourites"


def test_no_acceptance_criteria_is_not_buildable() -> None:
    # Even if the model claims "ready", missing AC means not buildable.
    llm = FakeStructuredLLM(
        [_valid_response(acceptance_criteria=[], implementation_readiness="ready")]
    )
    analysis = OpenAITicketAnalyzer(llm).analyse(_ticket())
    assert analysis.is_ready_to_build is False


def test_system_prompt_carries_hard_rules_and_ticket_text() -> None:
    llm = FakeStructuredLLM([_valid_response()])
    OpenAITicketAnalyzer(llm).analyse(_ticket())
    call = llm.calls[0]
    assert "do NOT write code" in call["system"]
    assert "acceptance criteria" in call["system"].lower()
    assert "Add customer favourites" in call["user"]


def test_invalid_then_valid_retries_and_succeeds() -> None:
    invalid = _valid_response(ambiguity_score=500)  # out of range -> rejected
    llm = FakeStructuredLLM([invalid, _valid_response()])
    analysis = OpenAITicketAnalyzer(llm, max_attempts=2).analyse(_ticket())
    assert analysis.is_ready_to_build is True
    assert len(llm.calls) == 2
    # The retry prompt includes corrective feedback.
    assert "invalid" in llm.calls[1]["user"].lower()


def test_persistently_invalid_raises() -> None:
    bad = _valid_response(work_type="not_a_type")
    llm = FakeStructuredLLM([bad, bad])
    with pytest.raises(LLMError):
        OpenAITicketAnalyzer(llm, max_attempts=2).analyse(_ticket())


def test_drops_in_as_orchestrator_analyzer(session_factory_for_openai) -> None:
    # The OpenAI analyzer satisfies the same protocol the orchestrator expects.
    llm = FakeStructuredLLM([_valid_response()])
    orch = FoundryOrchestrator(
        session_factory_for_openai,
        analyzer=OpenAITicketAnalyzer(llm),
    )
    run_id = orch.intake_and_plan(
        _ticket(known_repositories=["customer-web"]), trigger_type="label"
    )
    run = orch.get_run(run_id)
    # A ready ticket with a confident repo reaches waiting-for-approval.
    assert run.status.value == "waiting_approval"


@pytest.fixture
def session_factory_for_openai():
    from foundry.db import create_all, make_engine, make_session_factory

    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)
