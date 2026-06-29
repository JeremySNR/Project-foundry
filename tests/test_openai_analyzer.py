"""Tests for the OpenAI-backed analyzer (using a fake StructuredLLM, no network)."""

from __future__ import annotations

import pytest

from foundry.engines import (
    FakeStructuredLLM,
    HeuristicAnalyzer,
    LLMError,
    OpenAITicketAnalyzer,
)
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


def test_persistently_invalid_degrades_to_heuristic_loudly() -> None:
    # Mirrors the risk classifiers' degrade-to-floor design: when the LLM
    # cannot produce usable output, intake still gets the deterministic
    # analysis, with the degradation recorded in the artifact (assumptions).
    bad = _valid_response(work_type="not_a_type")
    llm = FakeStructuredLLM([bad, bad])
    ticket = _ticket(description="Acceptance criteria:\n- The heart saves the item")
    analysis = OpenAITicketAnalyzer(llm, max_attempts=2).analyse(ticket)

    expected = HeuristicAnalyzer().analyse(ticket)
    assert analysis.implementation_readiness is expected.implementation_readiness
    assert analysis.acceptance_criteria == expected.acceptance_criteria
    assert any("LLM analysis unavailable" in a for a in analysis.assumptions)


def test_non_object_response_degrades_to_heuristic_not_crash() -> None:
    # A StructuredLLM that returns a non-object (a JSON array/scalar slipping
    # past a non-strict model) previously raised an uncaught TypeError from the
    # identity-field assignments, aborting intake. It must degrade to the
    # heuristic floor like any other unusable LLM output.
    class _NonDict:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            return ["not", "an", "object"]

    llm = _NonDict()
    ticket = _ticket(description="Acceptance criteria:\n- The heart saves the item")
    analysis = OpenAITicketAnalyzer(llm, max_attempts=2).analyse(ticket)

    expected = HeuristicAnalyzer().analyse(ticket)
    assert analysis.implementation_readiness is expected.implementation_readiness
    assert any("LLM analysis unavailable" in a for a in analysis.assumptions)
    # The non-object response is retried like any invalid output before degrading.
    assert llm.calls == 2


def test_sdk_failure_degrades_to_heuristic_and_keeps_hard_rules() -> None:
    # An LLM raising (rate limit, timeout, outage) degrades the same way -
    # and the conservative heuristic still parks an AC-less ticket.
    class _Raising:
        def generate(self, **kwargs):
            raise LLMError("simulated OpenAI outage")

    analysis = OpenAITicketAnalyzer(_Raising()).analyse(_ticket())
    assert analysis.is_ready_to_build is False
    assert any("LLM analysis unavailable" in a for a in analysis.assumptions)


def test_llm_outage_does_not_fail_intake(session_factory_for_openai) -> None:
    # Issue #12: an OpenAI outage previously aborted intake_and_plan with zero
    # audit trace. Now the run is created from the heuristic fallback.
    class _Raising:
        def generate(self, **kwargs):
            raise LLMError("simulated OpenAI outage")

    orch = FoundryOrchestrator(
        session_factory_for_openai,
        analyzer=OpenAITicketAnalyzer(_Raising()),
    )
    run_id = orch.intake_and_plan(
        _ticket(
            description="Acceptance criteria:\n- The heart saves the item",
            known_repositories=["customer-web"],
        ),
        trigger_type="label",
    )
    assert orch.get_run(run_id).status.value == "waiting_approval"


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
