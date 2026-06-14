"""Tests for the LLM-backed planner (using a fake StructuredLLM, no network)."""

from __future__ import annotations

import pytest

from foundry.agents.provider import SecretLeakError, assert_no_secrets
from foundry.engines import (
    FakeStructuredLLM,
    HeuristicAnalyzer,
    HeuristicRiskClassifier,
    LLMError,
    LlmPlanner,
    StaticContextEnricher,
    TemplatePlanner,
)
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas import (
    CandidateFile,
    CandidateRepository,
    CodingAgentJobInput,
    ContextBundle,
    ManifestFacts,
    RepoCodeFacts,
)
from foundry.schemas.ticket import RawTicket

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


def _code_context() -> ContextBundle:
    """A confident repo plus code-aware evidence (candidate files + facts)."""
    return ContextBundle(
        candidate_repositories=[
            CandidateRepository(
                repo="customer-web",
                confidence=88,
                reason="Repo contains favourites UI components.",
            )
        ],
        candidate_files=[
            CandidateFile(
                path="src/features/favourites/state.ts",
                reason="Existing favourites state slice.",
            ),
            CandidateFile(
                path="src/api/favourites.ts", reason="API client for favourites."
            ),
        ],
        code_facts=[
            RepoCodeFacts(
                repo="customer-web",
                test_layout=["src/**/*.test.ts"],
                manifests=[
                    ManifestFacts(
                        path="package.json",
                        kind="package_json",
                        test_command="npm test",
                    )
                ],
                conventions=["GitHub Actions CI"],
            )
        ],
        test_commands=["npm test"],
    )


def _valid_output(**overrides) -> dict:
    base = {
        "implementation_steps": [
            {
                "description": "Add a favourites button to the item card",
                "files": ["src/features/favourites/Button.tsx"],
                "expected_output": "A button wired to the favourites action.",
            },
            {
                "description": "Persist favourites through the API client",
                "files": ["src/api/favourites.ts"],
            },
        ],
        "expected_files_or_areas": ["src/features/favourites/state.ts"],
        "unit_tests": ["src/features/favourites/Button.test.tsx"],
        "integration_tests": [],
        "verify_commands": ["npm test"],
        "rollback_considerations": ["Revert the PR; no data migration involved."],
        "out_of_scope": ["Recommendations engine."],
    }
    base.update(overrides)
    return base


def _plan(llm, ticket=None, context=None):
    ticket = ticket or _ready_ticket()
    context = context if context is not None else _code_context()
    analysis = HeuristicAnalyzer().analyse(ticket)
    risk = HeuristicRiskClassifier().classify(ticket, analysis, context)
    return LlmPlanner(llm).plan(ticket, analysis, context, risk), analysis


# -- enriched, file-level output ----------------------------------------------


def test_enriched_plan_has_file_level_steps_and_expected_files() -> None:
    llm = FakeStructuredLLM([_valid_output()])
    plan, _ = _plan(llm)

    assert len(plan.implementation_steps) == 2
    # Step descriptions carry the named files.
    assert "src/features/favourites/Button.tsx" in plan.implementation_steps[0].description
    # expected_files_or_areas unions the dedicated field with files named on steps.
    assert "src/features/favourites/state.ts" in plan.expected_files_or_areas
    assert "src/api/favourites.ts" in plan.expected_files_or_areas
    # Test locations and verify commands flow through.
    assert plan.test_plan.unit_tests == ["src/features/favourites/Button.test.tsx"]
    assert "npm test" in plan.test_plan.manual_checks
    # Instructions are populated and name the files + a verify command.
    assert plan.agent_instructions is not None
    assert "src/api/favourites.ts" in plan.agent_instructions
    assert "Verify with:" in plan.agent_instructions


def test_identity_and_scope_stay_deterministic() -> None:
    # The model returns its own steps, but goal/scope/repo come from analysis.
    llm = FakeStructuredLLM([_valid_output()])
    plan, analysis = _plan(llm)
    assert plan.goal == analysis.summary
    assert plan.scope == list(analysis.acceptance_criteria)
    assert plan.affected_repositories == ["customer-web"]
    assert "LIN-123" in plan.agent_instructions


def test_constraints_block_is_deterministic_not_from_model() -> None:
    # The model omits constraints entirely; Foundry still renders the guardrail
    # block and the draft-PR closing — the model can never relax a constraint.
    llm = FakeStructuredLLM([_valid_output()])
    plan, _ = _plan(llm)
    assert "Do not modify files matching:" in plan.agent_instructions
    assert "migrations/**" in plan.agent_instructions
    assert "Do not perform database migrations." in plan.agent_instructions
    assert "open a draft PR" in plan.agent_instructions
    # And the system prompt tells the model not to restate them.
    assert "Foundry adds those itself" in llm.calls[0]["system"]


def test_prompt_carries_candidate_files_and_code_facts() -> None:
    llm = FakeStructuredLLM([_valid_output()])
    _plan(llm)
    user = llm.calls[0]["user"]
    assert "src/features/favourites/state.ts" in user
    assert "Candidate files" in user
    assert "npm test" in user
    assert "GitHub Actions CI" in user
    assert "UNTRUSTED DATA" in llm.calls[0]["system"]


# -- floor discipline: no model call unless the floor would dispatch -----------


def test_not_ready_ticket_skips_llm_and_matches_template() -> None:
    ticket = RawTicket(issue_id="i", issue_key="LIN-6", title="Vague")
    context = StaticContextEnricher().enrich(ticket, HeuristicAnalyzer().analyse(ticket))
    llm = FakeStructuredLLM([_valid_output()])
    plan, _ = _plan(llm, ticket=ticket, context=context)
    # The model is never consulted for a non-buildable ticket.
    assert llm.calls == []
    assert plan.agent_instructions is None


def test_no_confident_repo_skips_llm() -> None:
    # Ready analysis but two repos above threshold => ambiguous => not dispatchable.
    ambiguous = ContextBundle(
        candidate_repositories=[
            CandidateRepository(repo="a", confidence=90, reason="x"),
            CandidateRepository(repo="b", confidence=85, reason="y"),
        ],
    )
    llm = FakeStructuredLLM([_valid_output()])
    plan, _ = _plan(llm, context=ambiguous)
    assert llm.calls == []
    assert plan.agent_instructions is None


# -- degrade-to-floor ----------------------------------------------------------


def test_llm_failure_degrades_to_template_with_note() -> None:
    class _Raising:
        def generate(self, **kwargs):
            raise LLMError("simulated outage")

    ticket = _ready_ticket()
    context = _code_context()
    analysis = HeuristicAnalyzer().analyse(ticket)
    risk = HeuristicRiskClassifier().classify(ticket, analysis, context)

    template = TemplatePlanner().plan(ticket, analysis, context, risk)
    degraded = LlmPlanner(_Raising()).plan(ticket, analysis, context, risk)

    # Falls back to the template plan (still has instructions, not None) and
    # records the degradation honestly.
    assert degraded.agent_instructions == template.agent_instructions
    assert degraded.implementation_steps == template.implementation_steps
    assert any("LLM planning unavailable" in q for q in degraded.open_questions)


def test_empty_steps_degrades_to_template() -> None:
    llm = FakeStructuredLLM([_valid_output(implementation_steps=[])])
    plan, _ = _plan(llm)
    # A ready ticket with no model steps is no better than the template plan.
    assert any("LLM planning unavailable" in q for q in plan.open_questions)
    assert len(plan.implementation_steps) == 2  # template: one per AC


def test_invalid_then_valid_retries_and_succeeds() -> None:
    invalid = {"implementation_steps": [{"description": "x", "bogus_field": 1}]}
    llm = FakeStructuredLLM([invalid, _valid_output()])
    plan, _ = _plan(llm)
    assert len(plan.implementation_steps) == 2
    assert len(llm.calls) == 2
    assert "invalid" in llm.calls[1]["user"].lower()


def test_persistently_invalid_degrades() -> None:
    bad = {"implementation_steps": [{"description": "x", "bogus_field": 1}]}
    llm = FakeStructuredLLM([bad, bad])
    plan, _ = _plan(llm)
    assert any("LLM planning unavailable" in q for q in plan.open_questions)


# -- secret-leak guard still covers the enriched instructions ------------------


def test_secret_in_llm_plan_is_caught_by_guard() -> None:
    # invariant #6: a secret the model slipped into a step must not reach a
    # provider. The guard scans the whole serialized job input, including the
    # LLM-produced agent_instructions and delivery_plan.
    leaky = _valid_output(
        implementation_steps=[
            {
                "description": "wire token authorization: sk-supersecretvalue123",
                "files": ["src/api/favourites.ts"],
            }
        ]
    )
    llm = FakeStructuredLLM([leaky])
    plan, _ = _plan(llm)
    job_input = CodingAgentJobInput(
        run_id="run-1",
        repo="customer-web",
        branch_name="foundry/lin-123",
        ticket_url="https://example.com/LIN-123",
        delivery_plan=plan,
        agent_instructions=plan.agent_instructions,
    )
    with pytest.raises(SecretLeakError):
        assert_no_secrets(job_input)


# -- drops into the orchestrator behind the planner seam ----------------------


def test_drops_in_as_orchestrator_planner(session_factory) -> None:
    llm = FakeStructuredLLM([_valid_output()])
    orch = FoundryOrchestrator(
        session_factory,
        enricher=StaticContextEnricher(repo_catalog={"customer-web": ["favourite"]}),
        planner=LlmPlanner(llm),
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    run = orch.get_run(run_id)
    assert run.status.value == "waiting_approval"
    assert len(llm.calls) == 1


@pytest.fixture
def session_factory():
    from foundry.db import create_all, make_engine, make_session_factory

    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)
