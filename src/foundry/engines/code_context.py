"""Code-aware context enrichment.

``CodeContextEnricher`` extends the catalog enricher with code facts gathered
by ``foundry-catalog sync --code-facts``: file-tree paths become a scored
field so routing can match a ticket against actual code layout, reason strings
cite concrete paths and CODEOWNERS owners, and the resulting ``ContextBundle``
carries structured ``RepoCodeFacts`` for downstream engines (risk, planner).

All confidence machinery is inherited unchanged: explicit association (90) and
linked resources (85) still win, delivery-memory priors stay capped (89), and
stale catalog rows stay capped below the dispatch threshold (65). Code
evidence improves recall and explainability - it never creates a new
confidence tier and never weakens a gate.
"""

from __future__ import annotations

import json
import re
from typing import Any

from foundry.catalog.code_facts import derive_conventions, infer_test_commands
from foundry.schemas.analysis import TicketAnalysis
from foundry.schemas.common import REPO_CONFIDENCE_THRESHOLD
from foundry.schemas.context import (
    CandidateFile,
    CodeOwnersRule,
    ContextBundle,
    ManifestFacts,
    RepoCodeFacts,
)
from foundry.schemas.ticket import RawTicket

from .enrichment import _FIELD_WEIGHTS, CatalogContextEnricher, _tokens

# Path tokens carry real signal (a ticket naming "invoice" matching
# src/billing/invoice.py) but are noisier than topics/name, so they sit
# between description (2.0) and name/topics (3.0).
_PATHS_FIELD_WEIGHT = 2.5

# Guard against token explosion on huge trees: scoring is O(query x fields).
_MAX_PATH_TOKENS = 5000

_MAX_CANDIDATE_FILES = 10
_MAX_EVIDENCE_PATHS = 3


def _path_tokens(path: str) -> set[str]:
    """Tokens from a path's directory segments and file stem."""
    stem = re.sub(r"\.[a-z0-9]+$", "", path.lower())
    return set(_tokens(re.sub(r"[-_./]", " ", stem)))


def _load_json(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw or "") if raw else default
    except (json.JSONDecodeError, TypeError):
        return default


class CodeContextEnricher(CatalogContextEnricher):
    """Catalog enricher upgraded with code facts from the synced file trees."""

    _field_weights = {**_FIELD_WEIGHTS, "paths": _PATHS_FIELD_WEIGHT}

    def _document(self, entry: Any) -> dict[str, set[str]]:
        doc = super()._document(entry)
        tokens: set[str] = set()
        for path in _load_json(entry.tree_paths, []):
            tokens |= _path_tokens(path)
            if len(tokens) >= _MAX_PATH_TOKENS:
                break
        doc["paths"] = tokens
        return doc

    def _evidence_suffix(self, entry: Any, matched_terms: set[str]) -> str:
        paths = _load_json(entry.tree_paths, [])
        if not paths:
            return ""
        matching = _matching_paths(paths, matched_terms, limit=_MAX_EVIDENCE_PATHS)
        if not matching:
            return ""
        suffix = f" Code evidence: {', '.join(path for path, _ in matching)}"
        owners = _owners_for_paths(entry, [path for path, _ in matching])
        if owners:
            suffix += f"; owners: {', '.join(owners)}"
        return suffix + "."

    def enrich(self, ticket: RawTicket, analysis: TicketAnalysis) -> ContextBundle:
        from foundry.db.models import FoundryRepoCatalogEntry

        bundle = super().enrich(ticket, analysis)

        confident = [
            c
            for c in bundle.candidate_repositories
            if c.confidence >= REPO_CONFIDENCE_THRESHOLD
        ]
        if not confident:
            return bundle

        ticket_tokens = set(_tokens(ticket.text_blob()))
        with self._session_factory() as session:
            for candidate in confident:
                entry = session.get(FoundryRepoCatalogEntry, candidate.repo)
                if entry is None:
                    continue
                tree_paths = _load_json(entry.tree_paths, [])
                if not tree_paths:
                    bundle.unknowns.append(
                        f"No code facts for {candidate.repo} - run "
                        "'foundry-catalog sync --code-facts' to gather them."
                    )
                    continue
                facts = _facts_from_entry(entry, tree_paths)
                bundle.code_facts.append(facts)
                if candidate.repo == (
                    bundle.best_repository.repo if bundle.best_repository else None
                ):
                    bundle.candidate_files.extend(
                        _candidate_files(tree_paths, ticket_tokens)
                    )
                    for command in infer_test_commands(
                        [m.model_dump() for m in facts.manifests]
                    ):
                        if command not in bundle.test_commands:
                            bundle.test_commands.append(command)
        return bundle


def _facts_from_entry(entry: Any, tree_paths: list[str]) -> RepoCodeFacts:
    return RepoCodeFacts(
        repo=entry.repo,
        default_branch=entry.default_branch,
        test_layout=_load_json(entry.test_layout, []),
        codeowners=[
            CodeOwnersRule(**rule) for rule in _load_json(entry.codeowners, [])
        ],
        manifests=[
            ManifestFacts(**manifest) for manifest in _load_json(entry.manifests, [])
        ],
        languages=_load_json(entry.languages, {}),
        conventions=derive_conventions(tree_paths),
        tree_truncated=bool(entry.tree_truncated),
    )


def _matching_paths(
    paths: list[str], terms: set[str], *, limit: int
) -> list[tuple[str, set[str]]]:
    """Paths whose tokens overlap ``terms``, best overlap then shallowest first."""
    scored: list[tuple[int, int, str, set[str]]] = []
    for path in paths:
        overlap = _path_tokens(path) & terms
        if overlap:
            scored.append((-len(overlap), path.count("/"), path, overlap))
    scored.sort()
    return [(path, overlap) for _, _, path, overlap in scored[:limit]]


def _candidate_files(paths: list[str], ticket_tokens: set[str]) -> list[CandidateFile]:
    return [
        CandidateFile(
            path=path,
            reason=f"Path matches ticket terms: {', '.join(sorted(overlap))}.",
        )
        for path, overlap in _matching_paths(
            paths, ticket_tokens, limit=_MAX_CANDIDATE_FILES
        )
    ]


def _owners_for_paths(entry: Any, paths: list[str]) -> list[str]:
    """Owners whose CODEOWNERS pattern matches any of the evidence paths.

    Simplified matching (exact path, directory prefix, or '*' wildcard) - good
    enough for evidence strings; this is not an authorization mechanism.
    """
    rules = _load_json(entry.codeowners, [])
    owners: list[str] = []
    for rule in rules:
        pattern = str(rule.get("pattern", "")).lstrip("/")
        if not pattern:
            continue
        for path in paths:
            if _codeowners_match(pattern, path):
                for owner in rule.get("owners", []):
                    if owner not in owners:
                        owners.append(owner)
                break
    return owners[:3]


def _codeowners_match(pattern: str, path: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if "*" in pattern:
        regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        return re.fullmatch(regex, path) is not None
    return path == pattern or path.startswith(pattern + "/")
