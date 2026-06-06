"""Context enrichment stage.

For the MVP, context comes from GitHub (a real implementation will use the GitHub
MCP server). ``StaticContextEnricher`` is a deterministic reference that derives
candidate repositories from explicit signals on the ticket plus an optional repo
catalog of keywords. Core rule: never assume the repo from the title alone;
attach a confidence to every candidate.
"""

from __future__ import annotations

from typing import Protocol

from foundry.schemas.analysis import TicketAnalysis
from foundry.schemas.context import CandidateRepository, ContextBundle
from foundry.schemas.ticket import RawTicket

# Confidence assigned to repositories surfaced by each kind of signal.
_EXPLICIT_REPO_CONFIDENCE = 90
_LINKED_REPO_CONFIDENCE = 85


class ContextEnricher(Protocol):
    def enrich(self, ticket: RawTicket, analysis: TicketAnalysis) -> ContextBundle: ...


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

        def consider(repo: str, confidence: int, reason: str) -> None:
            existing = candidates.get(repo)
            if existing is None or confidence > existing.confidence:
                candidates[repo] = CandidateRepository(
                    repo=repo, confidence=confidence, reason=reason
                )

        for repo in ticket.known_repositories:
            consider(repo, _EXPLICIT_REPO_CONFIDENCE, "Explicitly associated with the issue.")

        for link in ticket.linked_resources:
            if link.repo:
                consider(
                    link.repo,
                    _LINKED_REPO_CONFIDENCE,
                    f"Linked {link.kind} points at this repository.",
                )

        for repo, keywords in self._catalog.items():
            hits = [k for k in keywords if k.lower() in blob]
            if hits:
                # Confidence scales with keyword hits. A single hit reaches 60%
                # (below the 70% dispatch threshold) so one coincidental keyword
                # cannot trigger autonomous work. Two independent hits are needed
                # to cross the threshold.
                confidence = min(50 + 10 * len(hits), 95)
                consider(
                    repo,
                    confidence,
                    f"Ticket mentions {', '.join(sorted(hits))}.",
                )

        related_prs = [r.url for r in ticket.linked_resources if r.kind == "github_pr"]
        related_issues = [
            r.url for r in ticket.linked_resources if r.kind == "github_issue"
        ]
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
