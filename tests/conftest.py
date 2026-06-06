"""Test fixtures and import-path setup.

Adds ``src`` to ``sys.path`` so tests run even when the package is not installed
(e.g. a clean checkout). When installed via ``pip install -e .`` this is a no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from foundry.schemas import (  # noqa: E402
    CandidateRepository,
    ContextBundle,
    DeliveryPlan,
    ImplementationReadiness,
    ImplementationStep,
    TestPlan,
    TicketAnalysis,
    WorkType,
)


@pytest.fixture
def ready_analysis() -> TicketAnalysis:
    return TicketAnalysis(
        ticket_id="LIN-123",
        title="Add customer favourites",
        work_type=WorkType.FEATURE,
        summary="Let customers favourite items.",
        user_problem="Customers cannot save items.",
        business_value="Increases retention.",
        acceptance_criteria=["A favourites button exists", "Favourites persist"],
        missing_information=[],
        assumptions=["Auth already exists"],
        ambiguity_score=10,
        implementation_readiness=ImplementationReadiness.READY,
        confidence=88,
    )


@pytest.fixture
def confident_context() -> ContextBundle:
    return ContextBundle(
        candidate_repositories=[
            CandidateRepository(
                repo="customer-web",
                confidence=82,
                reason="Repo contains favourites UI components.",
            )
        ],
        test_commands=["npm test", "npm run lint"],
    )


@pytest.fixture
def delivery_plan() -> DeliveryPlan:
    return DeliveryPlan(
        goal="Add customer favourites",
        scope=["Favourites UI", "API client call"],
        out_of_scope=["Recommendations engine"],
        affected_repositories=["customer-web"],
        expected_files_or_areas=["src/features/favourites"],
        implementation_steps=[
            ImplementationStep(
                step=1, description="Add favourites state", expected_output="state slice"
            )
        ],
        test_plan=TestPlan(unit_tests=["favourites reducer"]),
        agent_instructions="Implement favourites per the plan.",
    )
