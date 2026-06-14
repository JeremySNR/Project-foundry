"""Delivery-memory priors: history as a routing signal, bounded and explainable."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foundry.db import create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRun, FoundryRunOutcome
from foundry.engines.enrichment import CatalogContextEnricher, StaticContextEnricher
from foundry.memory.priors import DeliveryMemoryPriors, smoothed_confidence
from foundry.schemas import (
    ImplementationReadiness,
    TicketAnalysis,
    WorkType,
)
from foundry.schemas.common import RunStatus
from foundry.schemas.ticket import RawTicket


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


def _seed_outcomes(
    session_factory,
    *,
    prefix: str = "ENG",
    work_type: str = "feature",
    repo: str = "acme/billing-service",
    merged: int = 0,
    blocked: int = 0,
    start: int = 0,
) -> None:
    """Insert outcome rows (with their parent runs) directly."""
    rows = [("merged", i) for i in range(merged)] + [
        ("blocked", merged + i) for i in range(blocked)
    ]
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        for outcome, i in rows:
            n = start + i
            run_id = f"run-{prefix}-{work_type}-{repo}-{n}"
            session.add(
                FoundryRun(
                    id=run_id,
                    linear_issue_id=f"issue-{prefix}-{n}",
                    linear_issue_key=f"{prefix}-{n}",
                    status=RunStatus.COMPLETE
                    if outcome == "merged"
                    else RunStatus.BLOCKED,
                    trigger_type="label",
                )
            )
            session.add(
                FoundryRunOutcome(
                    run_id=run_id,
                    linear_issue_id=f"issue-{prefix}-{n}",
                    issue_key_prefix=prefix,
                    outcome=outcome,
                    repo=repo,
                    work_type=work_type,
                    trigger_type="label",
                    created_at_run=now,
                    completed_at=now,
                    jobs_count=1,
                )
            )
        session.commit()


def _ticket(issue_key: str = "ENG-99") -> RawTicket:
    return RawTicket(issue_id="i-99", issue_key=issue_key, title="Fix invoice totals")


def _analysis(work_type: WorkType = WorkType.FEATURE) -> TicketAnalysis:
    return TicketAnalysis(
        ticket_id="ENG-99",
        title="Fix invoice totals",
        work_type=work_type,
        summary="x",
        user_problem="x",
        business_value="x",
        acceptance_criteria=["a"],
        ambiguity_score=10,
        implementation_readiness=ImplementationReadiness.READY,
        confidence=80,
    )


def test_smoothed_confidence_matches_worked_example() -> None:
    # 14 of 16 merged -> round(100 * 15/18) = 83.
    assert smoothed_confidence(14, 16, cap=89) == 83
    # Small samples stay modest: 3 of 3 -> 80, not 100.
    assert smoothed_confidence(3, 3, cap=89) == 80
    # The cap holds even on overwhelming history.
    assert smoothed_confidence(100, 100, cap=89) == 89


def test_priors_emit_candidate_with_audit_friendly_reason(session_factory) -> None:
    _seed_outcomes(session_factory, merged=14, blocked=2)
    priors = DeliveryMemoryPriors(session_factory)
    candidates = priors.candidates_for(_ticket(), _analysis())
    assert len(candidates) == 1
    c = candidates[0]
    assert c.repo == "acme/billing-service"
    assert c.confidence == 83
    assert c.reason == (
        "Delivery memory: 14 of 16 ENG feature tickets merged in this repository."
    )


def test_min_samples_gate_keeps_thin_history_quiet(session_factory) -> None:
    _seed_outcomes(session_factory, merged=2)
    priors = DeliveryMemoryPriors(session_factory, min_samples=3)
    assert priors.candidates_for(_ticket(), _analysis()) == []


def test_mostly_failed_history_is_not_a_routing_signal(session_factory) -> None:
    _seed_outcomes(session_factory, merged=1, blocked=5)
    priors = DeliveryMemoryPriors(session_factory)
    assert priors.candidates_for(_ticket(), _analysis()) == []


def test_falls_back_to_prefix_when_work_type_pair_is_thin(session_factory) -> None:
    # Plenty of ENG bug history, but the incoming ticket is a feature.
    _seed_outcomes(session_factory, work_type="bug", merged=6)
    priors = DeliveryMemoryPriors(session_factory)
    candidates = priors.candidates_for(_ticket(), _analysis(WorkType.FEATURE))
    assert len(candidates) == 1
    assert candidates[0].reason == (
        "Delivery memory: 6 of 6 ENG tickets merged in this repository."
    )


def test_unknown_team_prefix_yields_nothing(session_factory) -> None:
    _seed_outcomes(session_factory, merged=6)
    priors = DeliveryMemoryPriors(session_factory)
    assert priors.candidates_for(_ticket("OPS-1"), _analysis()) == []


def test_confidence_cap_is_configurable(session_factory) -> None:
    _seed_outcomes(session_factory, merged=50)
    priors = DeliveryMemoryPriors(session_factory, confidence_cap=75)
    assert priors.candidates_for(_ticket(), _analysis())[0].confidence == 75


def test_cache_serves_stale_until_invalidated(session_factory) -> None:
    _seed_outcomes(session_factory, merged=6)
    priors = DeliveryMemoryPriors(session_factory, cache_ttl_seconds=3600)
    assert len(priors.candidates_for(_ticket(), _analysis())) == 1
    _seed_outcomes(
        session_factory, repo="acme/other-service", merged=6, start=100
    )
    # Cached: the new repo is not seen yet.
    assert len(priors.candidates_for(_ticket(), _analysis())) == 1
    priors.invalidate()
    assert len(priors.candidates_for(_ticket(), _analysis())) == 2


# -- enricher integration ---------------------------------------------------------


def test_catalog_enricher_merges_prior_candidates(session_factory) -> None:
    _seed_outcomes(session_factory, merged=14, blocked=2)
    enricher = CatalogContextEnricher(
        session_factory, priors=DeliveryMemoryPriors(session_factory)
    )
    bundle = enricher.enrich(_ticket(), _analysis())
    best = bundle.best_repository
    assert best is not None
    assert best.repo == "acme/billing-service"
    assert best.confidence == 83
    assert "Delivery memory" in best.reason


def test_explicit_repo_still_beats_prior(session_factory) -> None:
    _seed_outcomes(session_factory, merged=50)  # would cap at 89
    enricher = CatalogContextEnricher(
        session_factory, priors=DeliveryMemoryPriors(session_factory)
    )
    ticket = RawTicket(
        issue_id="i-99",
        issue_key="ENG-99",
        title="Fix invoice totals",
        known_repositories=["acme/billing-service"],
    )
    bundle = enricher.enrich(ticket, _analysis())
    best = bundle.best_repository
    assert best.confidence == 90
    assert best.reason == "Explicitly associated with the issue."


def test_enricher_without_priors_is_unchanged(session_factory) -> None:
    _seed_outcomes(session_factory, merged=14)
    bundle = CatalogContextEnricher(session_factory).enrich(_ticket(), _analysis())
    assert all("Delivery memory" not in c.reason for c in bundle.candidate_repositories)


def test_static_enricher_is_untouched(session_factory) -> None:
    bundle = StaticContextEnricher().enrich(_ticket(), _analysis())
    assert bundle.candidate_repositories == []
