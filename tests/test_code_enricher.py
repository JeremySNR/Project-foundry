"""Tests for CodeContextEnricher - all offline, in-memory SQLite."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from foundry.db.base import create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRepoCatalogEntry
from foundry.engines import HeuristicAnalyzer
from foundry.engines.code_context import CodeContextEnricher
from foundry.schemas.context import ContextBundle
from foundry.schemas.ticket import RawTicket


def _engine_and_sf():
    engine = make_engine()
    create_all(engine)
    return engine, make_session_factory(engine)


_NOW = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
_RECENT = _NOW - timedelta(days=1)


def _entry(
    repo: str,
    *,
    description: str = "",
    tree_paths: list[str] | None = None,
    test_layout: list[str] | None = None,
    codeowners: list[dict] | None = None,
    manifests: list[dict] | None = None,
    languages: dict[str, int] | None = None,
    tree_truncated: bool = False,
    synced_at: datetime | None = None,
    pushed_at: datetime | None = None,
) -> FoundryRepoCatalogEntry:
    return FoundryRepoCatalogEntry(
        repo=repo,
        description=description,
        topics=json.dumps([]),
        readme_head="",
        recent_pr_titles=json.dumps([]),
        top_dirs=json.dumps([]),
        tree_paths=json.dumps(tree_paths or []),
        tree_truncated=tree_truncated,
        test_layout=json.dumps(test_layout or []),
        codeowners=json.dumps(codeowners or []),
        manifests=json.dumps(manifests or []),
        languages=json.dumps(languages or {}),
        archived=False,
        synced_at=synced_at if synced_at is not None else _RECENT,
        pushed_at=pushed_at if pushed_at is not None else _RECENT,
        updated_at=_NOW,
        created_at=_NOW,
    )


_BILLING_PATHS = [
    "pyproject.toml",
    ".github/CODEOWNERS",
    ".github/workflows/ci.yml",
    "src/billing/invoice.py",
    "src/billing/reconciliation.py",
    "tests/test_invoice.py",
]

_BILLING_MANIFESTS = [
    {
        "path": "pyproject.toml",
        "kind": "pyproject",
        "dependencies": ["fastapi", "stripe"],
        "test_command": "pytest",
    }
]

_BILLING_OWNERS = [{"pattern": "src/billing/", "owners": ["@org/payments"]}]


def _billing_entry(**overrides) -> FoundryRepoCatalogEntry:
    defaults = dict(
        description="",
        tree_paths=_BILLING_PATHS,
        test_layout=["tests/", "test_*.py"],
        codeowners=_BILLING_OWNERS,
        manifests=_BILLING_MANIFESTS,
        languages={"py": 4},
    )
    defaults.update(overrides)
    return _entry("org/billing", **defaults)


def _seed(sf, entries) -> None:
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


def _enrich(sf, ticket: RawTicket, **kwargs) -> ContextBundle:
    analysis = HeuristicAnalyzer().analyse(ticket)
    enricher = CodeContextEnricher(sf, now=lambda: _NOW, **kwargs)
    return enricher.enrich(ticket, analysis)


# ---------------------------------------------------------------------------
# 1. Path tokens lift a repo over the threshold; reason cites code evidence
# ---------------------------------------------------------------------------

def test_path_match_crosses_threshold_with_code_evidence() -> None:
    _, sf = _engine_and_sf()
    _seed(sf, [_billing_entry()])
    ticket = _ticket(title="Fix invoice reconciliation bug")
    bundle = _enrich(sf, ticket)

    best = bundle.best_repository
    assert best is not None and best.repo == "org/billing"
    assert best.confidence >= 70
    assert "Code evidence:" in best.reason
    assert "src/billing/invoice.py" in best.reason
    assert "@org/payments" in best.reason


# ---------------------------------------------------------------------------
# 2. code_facts, candidate_files and test_commands populated for the best repo
# ---------------------------------------------------------------------------

def test_code_facts_attached_to_confident_candidate() -> None:
    _, sf = _engine_and_sf()
    _seed(sf, [_billing_entry()])
    bundle = _enrich(sf, _ticket(title="Fix invoice reconciliation bug"))

    assert len(bundle.code_facts) == 1
    facts = bundle.code_facts[0]
    assert facts.repo == "org/billing"
    assert "tests/" in facts.test_layout
    assert facts.codeowners[0].owners == ["@org/payments"]
    assert facts.manifests[0].kind == "pyproject"
    assert "GitHub Actions CI" in facts.conventions

    paths = [f.path for f in bundle.candidate_files]
    assert "src/billing/invoice.py" in paths
    assert all(f.reason for f in bundle.candidate_files)
    assert "pytest" in bundle.test_commands


# ---------------------------------------------------------------------------
# 3. Explicit association still wins over heavy code matches
# ---------------------------------------------------------------------------

def test_explicit_association_still_wins() -> None:
    _, sf = _engine_and_sf()
    _seed(sf, [_billing_entry()])
    ticket = _ticket(
        title="Fix invoice reconciliation bug", known=["org/other-repo"]
    )
    bundle = _enrich(sf, ticket)

    best = bundle.best_repository
    assert best.repo == "org/other-repo"
    assert best.confidence == 90


# ---------------------------------------------------------------------------
# 4. Stale catalog row stays capped below the dispatch threshold
# ---------------------------------------------------------------------------

def test_stale_entry_with_code_evidence_still_capped() -> None:
    _, sf = _engine_and_sf()
    _seed(sf, [
        _billing_entry(
            synced_at=_NOW - timedelta(days=30),
            pushed_at=_NOW - timedelta(days=1),
        )
    ])
    bundle = _enrich(sf, _ticket(title="Fix invoice reconciliation bug"))

    candidate = {c.repo: c for c in bundle.candidate_repositories}["org/billing"]
    assert candidate.confidence <= 65
    assert not bundle.has_confident_repository()
    # No confident repo, so no code facts are attached
    assert bundle.code_facts == []


# ---------------------------------------------------------------------------
# 5. Confident repo without synced code facts degrades with actionable unknown
# ---------------------------------------------------------------------------

def test_missing_code_facts_degrades_with_unknown() -> None:
    _, sf = _engine_and_sf()
    _seed(sf, [
        _entry(
            "org/billing",
            description="invoice reconciliation service",
            tree_paths=[],
        )
    ])
    bundle = _enrich(sf, _ticket(title="Fix invoice reconciliation bug"))

    assert bundle.best_repository.repo == "org/billing"
    assert bundle.best_repository.confidence >= 70
    assert bundle.code_facts == []
    assert any("foundry-catalog sync --code-facts" in u for u in bundle.unknowns)


# ---------------------------------------------------------------------------
# 6. Bundle with code facts survives the artifact JSON round-trip
# ---------------------------------------------------------------------------

def test_bundle_round_trips_through_json() -> None:
    _, sf = _engine_and_sf()
    _seed(sf, [_billing_entry()])
    bundle = _enrich(sf, _ticket(title="Fix invoice reconciliation bug"))
    assert bundle.code_facts

    restored = ContextBundle.model_validate(json.loads(bundle.model_dump_json()))
    assert restored == bundle


# ---------------------------------------------------------------------------
# 7. Bundles persisted before code_facts existed still validate
# ---------------------------------------------------------------------------

def test_legacy_bundle_without_code_facts_validates() -> None:
    legacy = {
        "candidate_repositories": [
            {"repo": "org/billing", "confidence": 80, "reason": "Catalog match."}
        ],
        "candidate_files": [],
        "related_prs": [],
        "related_issues": [],
        "test_commands": [],
        "docs": [],
        "unknowns": [],
    }
    bundle = ContextBundle.model_validate(legacy)
    assert bundle.code_facts == []
