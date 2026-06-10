"""Tests for CatalogContextEnricher - all offline, in-memory SQLite."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from foundry.db.base import create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRepoCatalogEntry
from foundry.engines import HeuristicAnalyzer
from foundry.engines.enrichment import CatalogContextEnricher
from foundry.schemas.ticket import LinkedResource, RawTicket


def _engine_and_sf():
    engine = make_engine()
    create_all(engine)
    return engine, make_session_factory(engine)


_NOW = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
_RECENT = _NOW - timedelta(days=1)
_OLD_SYNCED = _NOW - timedelta(days=30)


def _entry(
    repo: str,
    *,
    description: str = "",
    topics: list[str] | None = None,
    readme_head: str = "",
    recent_pr_titles: list[str] | None = None,
    top_dirs: list[str] | None = None,
    archived: bool = False,
    synced_at: datetime | None = None,
    pushed_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> FoundryRepoCatalogEntry:
    now = _NOW
    return FoundryRepoCatalogEntry(
        repo=repo,
        description=description,
        topics=json.dumps(topics or []),
        readme_head=readme_head,
        recent_pr_titles=json.dumps(recent_pr_titles or []),
        top_dirs=json.dumps(top_dirs or []),
        archived=archived,
        synced_at=synced_at if synced_at is not None else _RECENT,
        pushed_at=pushed_at if pushed_at is not None else _RECENT,
        updated_at=updated_at if updated_at is not None else now,
        created_at=now,
    )


def _seed(sf, entries: list[FoundryRepoCatalogEntry]) -> None:
    with sf() as session:
        for e in entries:
            session.add(e)
        session.commit()


def _ticket(title: str = "", description: str = "", known: list[str] | None = None) -> RawTicket:
    return RawTicket(
        issue_id="i-1",
        issue_key="TKT-1",
        title=title,
        description=description,
        known_repositories=known or [],
    )


def _enrich(sf, ticket: RawTicket, **kwargs) -> "ContextBundle":
    analysis = HeuristicAnalyzer().analyse(ticket)
    enricher = CatalogContextEnricher(sf, now=lambda: _NOW, **kwargs)
    return enricher.enrich(ticket, analysis)


# ---------------------------------------------------------------------------
# 1. Two matched terms cross the threshold
# ---------------------------------------------------------------------------

def test_two_terms_cross_threshold() -> None:
    _, sf = _engine_and_sf()
    _seed(sf, [
        _entry("org/billing", description="invoice reconciliation service"),
    ])
    ticket = _ticket(title="Fix invoice reconciliation bug")
    bundle = _enrich(sf, ticket)

    candidates = {c.repo: c for c in bundle.candidate_repositories}
    assert "org/billing" in candidates
    c = candidates["org/billing"]
    assert c.confidence >= 70
    assert bundle.has_confident_repository()
    # Reason should name both matched terms
    assert "invoice" in c.reason or "reconciliation" in c.reason


# ---------------------------------------------------------------------------
# 2. Single coincidental term does NOT cross threshold
# ---------------------------------------------------------------------------

def test_single_term_does_not_cross_threshold() -> None:
    _, sf = _engine_and_sf()
    _seed(sf, [
        _entry("org/billing", description="invoice processing service"),
    ])
    ticket = _ticket(title="Fix invoice display")
    bundle = _enrich(sf, ticket)

    candidates = {c.repo: c for c in bundle.candidate_repositories}
    if "org/billing" in candidates:
        assert candidates["org/billing"].confidence < 70
    assert not bundle.has_confident_repository()


# ---------------------------------------------------------------------------
# 3. IDF filter: token present in all repos contributes nothing
# ---------------------------------------------------------------------------

def test_idf_filter_kills_ubiquitous_token() -> None:
    _, sf = _engine_and_sf()
    # Seed 10 repos all containing "service" in description
    entries = [
        _entry(f"org/repo{i}", description="generic service layer")
        for i in range(10)
    ]
    _seed(sf, entries)

    # Ticket only mentions "service" (present everywhere) plus one unique term
    # for org/repo0 only
    with sf() as session:
        r0 = session.get(FoundryRepoCatalogEntry, "org/repo0")
        r0.description = "authentication service"
        session.commit()

    ticket = _ticket(title="Fix the service")
    bundle = _enrich(sf, ticket)

    # "service" is in all 10 repos -> filtered by IDF; no repo should confidently match
    for c in bundle.candidate_repositories:
        assert c.confidence < 70


# ---------------------------------------------------------------------------
# 4. Tier-0 wins over catalog
# ---------------------------------------------------------------------------

def test_tier0_wins_over_catalog() -> None:
    _, sf = _engine_and_sf()
    _seed(sf, [
        _entry("org/catalog-match", description="invoice reconciliation payments"),
    ])
    ticket = _ticket(
        title="Fix invoice reconciliation",
        known=["org/explicit-repo"],
    )
    bundle = _enrich(sf, ticket)

    candidates = {c.repo: c for c in bundle.candidate_repositories}
    assert "org/explicit-repo" in candidates
    assert candidates["org/explicit-repo"].confidence == 90
    # Explicit repo wins
    best = bundle.best_repository
    assert best.repo == "org/explicit-repo"
    assert best.confidence == 90


# ---------------------------------------------------------------------------
# 5. Stale by push: confidence capped at 65, stale suffix in reason
# ---------------------------------------------------------------------------

def test_stale_by_push_caps_confidence() -> None:
    _, sf = _engine_and_sf()
    synced = _NOW - timedelta(days=2)
    pushed = _NOW - timedelta(days=1)  # pushed AFTER synced -> stale

    _seed(sf, [
        _entry(
            "org/billing",
            description="invoice reconciliation payments",
            synced_at=synced,
            pushed_at=pushed,
            updated_at=_NOW,
        ),
    ])
    ticket = _ticket(title="Fix invoice reconciliation payments")
    bundle = _enrich(sf, ticket)

    candidates = {c.repo: c for c in bundle.candidate_repositories}
    assert "org/billing" in candidates
    c = candidates["org/billing"]
    assert c.confidence <= 65
    assert "stale" in c.reason.lower()
    assert any("stale" in u.lower() for u in bundle.unknowns)


# ---------------------------------------------------------------------------
# 6. Stale by age: updated_at old -> capped
# ---------------------------------------------------------------------------

def test_stale_by_age_caps_confidence() -> None:
    _, sf = _engine_and_sf()
    old_update = _NOW - timedelta(days=30)
    synced = _NOW - timedelta(days=1)

    _seed(sf, [
        _entry(
            "org/billing",
            description="invoice reconciliation",
            synced_at=synced,
            pushed_at=synced,  # pushed <= synced: not stale by push
            updated_at=old_update,  # but updated_at is old: stale by age
        ),
    ])
    ticket = _ticket(title="invoice reconciliation feature")
    bundle = _enrich(sf, ticket, max_catalog_age_days=7)

    candidates = {c.repo: c for c in bundle.candidate_repositories}
    if "org/billing" in candidates:
        c = candidates["org/billing"]
        # Should be stale (updated 30 days ago, threshold 7)
        assert c.confidence <= 65


# ---------------------------------------------------------------------------
# 7. Empty catalog: behaves like Static (Tier-0 only) + empty-catalog unknown
# ---------------------------------------------------------------------------

def test_empty_catalog_returns_empty_catalog_message() -> None:
    _, sf = _engine_and_sf()
    ticket = _ticket(title="Fix invoice reconciliation", known=["org/explicit"])
    bundle = _enrich(sf, ticket)

    assert any("empty" in u.lower() for u in bundle.unknowns)
    # Tier-0 still works
    candidates = {c.repo: c for c in bundle.candidate_repositories}
    assert "org/explicit" in candidates


# ---------------------------------------------------------------------------
# 8. Archived rows are ignored
# ---------------------------------------------------------------------------

def test_archived_rows_ignored() -> None:
    _, sf = _engine_and_sf()
    _seed(sf, [
        _entry("org/billing-archived", description="invoice service", archived=True),
        _entry("org/billing-active", description="payment gateway"),
    ])
    ticket = _ticket(title="invoice payment issue")
    bundle = _enrich(sf, ticket)

    repos = {c.repo for c in bundle.candidate_repositories}
    assert "org/billing-archived" not in repos


# ---------------------------------------------------------------------------
# 9. Determinism: same inputs produce identical bundles
# ---------------------------------------------------------------------------

def test_determinism() -> None:
    _, sf = _engine_and_sf()
    _seed(sf, [
        _entry("org/billing", description="invoice reconciliation payments"),
        _entry("org/shipping", description="shipment tracking delivery"),
    ])
    ticket = _ticket(title="Fix invoice reconciliation", description="shipment tracking issue")

    bundle_a = _enrich(sf, ticket)
    bundle_b = _enrich(sf, ticket)

    assert bundle_a.candidate_repositories == bundle_b.candidate_repositories
    assert bundle_a.unknowns == bundle_b.unknowns


# ---------------------------------------------------------------------------
# 10. Manual repo_keywords still work and merge by max confidence
# ---------------------------------------------------------------------------

def test_manual_repo_keywords_still_work() -> None:
    _, sf = _engine_and_sf()
    _seed(sf, [
        _entry("org/billing", description="payment gateway service"),
    ])
    ticket = _ticket(title="Fix the stripe checkout invoice")

    # Manual keywords for billing-service with two terms
    bundle = _enrich(
        sf,
        ticket,
        repo_keywords={"org/billing": ["stripe", "invoice"]},
    )

    candidates = {c.repo: c for c in bundle.candidate_repositories}
    assert "org/billing" in candidates
    assert candidates["org/billing"].confidence >= 70
