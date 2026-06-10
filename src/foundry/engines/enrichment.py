"""Context enrichment stage.

For the MVP, context comes from GitHub (a real implementation will use the GitHub
MCP server). ``StaticContextEnricher`` is a deterministic reference that derives
candidate repositories from explicit signals on the ticket plus an optional repo
catalog of keywords. Core rule: never assume the repo from the title alone;
attach a confidence to every candidate.

``CatalogContextEnricher`` extends this with catalog-backed scoring against
per-repo metadata synced from the GitHub org, with freshness-aware confidence
capping so stale data never triggers autonomous dispatch.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Protocol

from foundry.schemas.analysis import TicketAnalysis
from foundry.schemas.context import CandidateRepository, ContextBundle
from foundry.schemas.ticket import RawTicket

# Confidence assigned to repositories surfaced by each kind of signal.
_EXPLICIT_REPO_CONFIDENCE = 90
_LINKED_REPO_CONFIDENCE = 85

# Stale-cap: catalog-derived confidence is held below the 70 dispatch threshold.
_STALE_CONFIDENCE_CAP = 65

_STOPWORDS = frozenset(
    "the and for with that this from are was should would when then than "
    "can our your has have not but all any out new use using add fix bug "
    "issue ticket user".split()
)

# Catalog field weights used in scoring.
_FIELD_WEIGHTS: dict[str, float] = {
    "name": 3.0,
    "topics": 3.0,
    "description": 2.0,
    "pr_titles": 2.0,
    "dirs": 2.0,
    "readme": 1.0,
}


class ContextEnricher(Protocol):
    def enrich(self, ticket: RawTicket, analysis: TicketAnalysis) -> ContextBundle: ...


# --------------------------------------------------------------------------- #
# Shared Tier-0 helpers                                                        #
# --------------------------------------------------------------------------- #

def _consider(
    candidates: dict[str, CandidateRepository],
    repo: str,
    confidence: int,
    reason: str,
) -> None:
    """Max-merge a candidate into the dict: keep the higher confidence."""
    existing = candidates.get(repo)
    if existing is None or confidence > existing.confidence:
        candidates[repo] = CandidateRepository(
            repo=repo, confidence=confidence, reason=reason
        )


def _apply_tier0(ticket: RawTicket, candidates: dict[str, CandidateRepository]) -> None:
    """Apply explicit (90) and linked-resource (85) signals — always wins."""
    for repo in ticket.known_repositories:
        _consider(candidates, repo, _EXPLICIT_REPO_CONFIDENCE, "Explicitly associated with the issue.")
    for link in ticket.linked_resources:
        if link.repo:
            _consider(
                candidates,
                link.repo,
                _LINKED_REPO_CONFIDENCE,
                f"Linked {link.kind} points at this repository.",
            )


def _related_prs_issues(
    ticket: RawTicket,
) -> tuple[list[str], list[str]]:
    related_prs = [r.url for r in ticket.linked_resources if r.kind == "github_pr"]
    related_issues = [r.url for r in ticket.linked_resources if r.kind == "github_issue"]
    return related_prs, related_issues


# --------------------------------------------------------------------------- #
# Static enricher                                                              #
# --------------------------------------------------------------------------- #

class StaticContextEnricher:
    """Reference enricher driven by explicit ticket signals + a keyword catalog."""

    def __init__(
        self,
        *,
        repo_catalog: dict[str, list[str]] | None = None,
        default_test_commands: list[str] | None = None,
    ) -> None:
        # repo name -> keywords that, if present in the ticket, suggest that repo.
        self._catalog = repo_catalog or {}
        self._default_test_commands = default_test_commands or []

    def enrich(self, ticket: RawTicket, analysis: TicketAnalysis) -> ContextBundle:
        blob = ticket.text_blob()
        candidates: dict[str, CandidateRepository] = {}

        _apply_tier0(ticket, candidates)

        for repo, keywords in self._catalog.items():
            hits = [k for k in keywords if k.lower() in blob]
            if hits:
                # Confidence scales with keyword hits. A single hit reaches 60%
                # (below the 70% dispatch threshold) so one coincidental keyword
                # cannot trigger autonomous work. Two independent hits are needed
                # to cross the threshold.
                confidence = min(50 + 10 * len(hits), 95)
                _consider(
                    candidates,
                    repo,
                    confidence,
                    f"Ticket mentions {', '.join(sorted(hits))}.",
                )

        related_prs, related_issues = _related_prs_issues(ticket)
        unknowns = [] if candidates else ["No candidate repository could be identified."]

        return ContextBundle(
            candidate_repositories=sorted(
                candidates.values(), key=lambda c: c.confidence, reverse=True
            ),
            related_prs=related_prs,
            related_issues=related_issues,
            test_commands=list(self._default_test_commands),
            unknowns=unknowns,
        )


# --------------------------------------------------------------------------- #
# Catalog scoring helpers                                                       #
# --------------------------------------------------------------------------- #

def _tokens(text: str) -> list[str]:
    """Lowercase, alphanumeric tokens >= 3 chars, minus stopwords."""
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in raw if len(t) >= 3 and t not in _STOPWORDS]


def _repo_document(entry: Any) -> dict[str, set[str]]:
    """Build a field -> token-set mapping for a catalog entry."""
    name_part = entry.repo.split("/")[-1] if "/" in entry.repo else entry.repo
    name_tokens = set(_tokens(re.sub(r"[-_]", " ", name_part)))

    topics_list: list[str] = []
    try:
        topics_list = json.loads(entry.topics or "[]")
    except (json.JSONDecodeError, TypeError):
        pass
    topics_tokens = set(_tokens(" ".join(topics_list)))

    description_tokens = set(_tokens(entry.description or ""))

    pr_titles_list: list[str] = []
    try:
        pr_titles_list = json.loads(entry.recent_pr_titles or "[]")
    except (json.JSONDecodeError, TypeError):
        pass
    pr_tokens = set(_tokens(" ".join(pr_titles_list)))

    dirs_list: list[str] = []
    try:
        dirs_list = json.loads(entry.top_dirs or "[]")
    except (json.JSONDecodeError, TypeError):
        pass
    dirs_tokens = set(_tokens(" ".join(dirs_list)))

    readme_tokens = set(_tokens(entry.readme_head or ""))

    return {
        "name": name_tokens,
        "topics": topics_tokens,
        "description": description_tokens,
        "pr_titles": pr_tokens,
        "dirs": dirs_tokens,
        "readme": readme_tokens,
    }


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Normalize to UTC-aware datetime (SQLite may return naive datetimes)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_stale(entry: Any, now: datetime, max_age_days: int) -> bool:
    """True when the entry is stale by push or by age."""
    pushed = _ensure_utc(entry.pushed_at)
    synced = _ensure_utc(entry.synced_at)
    if pushed is not None and synced is not None and pushed > synced:
        return True
    updated = _ensure_utc(entry.updated_at)
    if updated is None:
        return True
    age_threshold = now.timestamp() - max_age_days * 86400
    return updated.timestamp() < age_threshold


def _sync_age_str(entry: Any, now: datetime) -> str:
    synced = _ensure_utc(entry.synced_at)
    if synced is None:
        return "never synced"
    delta = now - synced
    days = delta.days
    if days == 0:
        return "synced today"
    if days == 1:
        return "synced 1d ago"
    return f"synced {days}d ago"


# --------------------------------------------------------------------------- #
# Catalog enricher                                                             #
# --------------------------------------------------------------------------- #

class CatalogContextEnricher:
    """Scores ticket text against the synced repo catalog, with freshness-aware confidence."""

    def __init__(
        self,
        session_factory: Any,
        *,
        repo_keywords: dict[str, list[str]] | None = None,
        default_test_commands: list[str] | None = None,
        max_catalog_age_days: int = 7,
        now: Any = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._session_factory = session_factory
        self._repo_keywords = repo_keywords or {}
        self._default_test_commands = default_test_commands or []
        self._max_catalog_age_days = max_catalog_age_days
        self._now = now

    def enrich(self, ticket: RawTicket, analysis: TicketAnalysis) -> ContextBundle:
        from foundry.db.models import FoundryRepoCatalogEntry

        blob = ticket.text_blob()
        now = self._now()
        candidates: dict[str, CandidateRepository] = {}
        unknowns: list[str] = []

        # Step 1: Tier 0 — explicit and linked repos
        _apply_tier0(ticket, candidates)

        # Step 2: Manual keywords (legacy, still honoured)
        if self._repo_keywords:
            for repo, keywords in self._repo_keywords.items():
                hits = [k for k in keywords if k.lower() in blob]
                if hits:
                    confidence = min(50 + 10 * len(hits), 95)
                    _consider(
                        candidates,
                        repo,
                        confidence,
                        f"Ticket mentions {', '.join(sorted(hits))}.",
                    )

        # Step 3: Catalog scoring
        with self._session_factory() as session:
            entries = (
                session.query(FoundryRepoCatalogEntry)
                .filter_by(archived=False)
                .all()
            )

        if not entries:
            unknowns.append(
                "Repo catalog is empty - run 'foundry-catalog sync' to populate it."
            )
        else:
            catalog_candidates = self._score_catalog(entries, blob, now)
            stale_capped = False
            for repo, confidence, reason in catalog_candidates:
                _consider(candidates, repo, confidence, reason)
                if confidence <= _STALE_CONFIDENCE_CAP:
                    stale_capped = True
            if stale_capped:
                unknowns.append(
                    "Repo catalog data is stale for some candidates - run 'foundry-catalog sync'."
                )

        # Step 6: Bundle
        related_prs, related_issues = _related_prs_issues(ticket)
        if not candidates:
            unknowns.append("No candidate repository could be identified.")

        sorted_candidates = sorted(
            candidates.values(),
            key=lambda c: (-c.confidence,),
        )

        docs = [
            f"{c.repo}: {_entry_description(entries if entries else [], c.repo)}"
            for c in sorted_candidates
            if _entry_description(entries if entries else [], c.repo)
        ]

        return ContextBundle(
            candidate_repositories=sorted_candidates,
            related_prs=related_prs,
            related_issues=related_issues,
            test_commands=list(self._default_test_commands),
            docs=docs,
            unknowns=unknowns,
        )

    def _score_catalog(
        self,
        entries: list[Any],
        blob: str,
        now: datetime,
    ) -> list[tuple[str, int, str]]:
        """Score all non-archived catalog entries against the ticket blob."""
        query_tokens = _tokens(blob)
        if not query_tokens:
            return []

        # Build documents
        docs: dict[str, dict[str, set[str]]] = {}
        for entry in entries:
            docs[entry.repo] = _repo_document(entry)

        # IDF filter: drop tokens present in too many repos
        repo_count = len(docs)
        idf_threshold = max(3, int(repo_count * 0.25))
        doc_freq: dict[str, int] = defaultdict(int)
        for repo_doc in docs.values():
            all_repo_tokens: set[str] = set()
            for field_tokens in repo_doc.values():
                all_repo_tokens |= field_tokens
            for tok in all_repo_tokens:
                doc_freq[tok] += 1

        surviving_query = [t for t in query_tokens if doc_freq.get(t, 0) <= idf_threshold]

        results: list[tuple[str, int, str]] = []
        entry_map = {e.repo: e for e in entries}

        for repo, repo_doc in docs.items():
            matched_terms: set[str] = set()
            term_fields: dict[str, list[str]] = {}
            weighted_score: float = 0.0

            for tok in surviving_query:
                matched_in: list[str] = []
                for field, field_tokens in repo_doc.items():
                    if tok in field_tokens:
                        matched_in.append(field)
                        weighted_score += _FIELD_WEIGHTS[field]
                if matched_in:
                    matched_terms.add(tok)
                    term_fields[tok] = matched_in

            if not matched_terms:
                continue

            confidence = min(50 + 10 * len(matched_terms), 95)

            # Step 4: freshness capping
            entry = entry_map[repo]
            stale = _is_stale(entry, now, self._max_catalog_age_days)
            sync_age = _sync_age_str(entry, now)

            # Build reason string
            terms_str = ", ".join(f"'{t}'" for t in sorted(matched_terms))
            # Summarise strongest matched fields
            field_counts: Counter_like = defaultdict(int)
            for fields in term_fields.values():
                for f in fields:
                    field_counts[f] += 1  # type: ignore[index]
            sorted_fields = sorted(field_counts.items(), key=lambda x: -x[1])
            top_fields = [f for f, _ in sorted_fields[:3]]
            fields_str = ", ".join(top_fields)

            if stale:
                confidence = min(confidence, _STALE_CONFIDENCE_CAP)
                synced_utc = _ensure_utc(entry.synced_at)
                stale_days = int((now - synced_utc).days) if synced_utc else 0
                stale_suffix = f" (stale: last synced {stale_days}d ago)"
                reason = (
                    f"Catalog match: {terms_str} ({fields_str}; {sync_age}).{stale_suffix}"
                )
            else:
                reason = f"Catalog match: {terms_str} ({fields_str}; {sync_age})."

            results.append((repo, confidence, reason))

        # Sort: descending confidence, then descending weighted score, then lexicographic
        results.sort(key=lambda x: (-x[1], x[0]))
        return results


def _entry_description(entries: list[Any], repo: str) -> str | None:
    for e in entries:
        if e.repo == repo:
            return e.description
    return None


# Alias for type annotation (avoid importing collections.Counter for type hints)
Counter_like = defaultdict
