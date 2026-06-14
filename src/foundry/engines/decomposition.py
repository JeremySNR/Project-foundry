"""Epic decomposition - split an epic ticket into per-repo child tickets.

The *producer* half of the parent/child run model (issue #35). The read side
(rollup, ``GET /runs/{id}/epic``, cross-run evidence export, dashboard board)
and the per-repo forbidden-path slice already landed; this is the piece that
turns one epic ticket spanning several repositories into one independently
gated child run per repo, so the README's own motivating example - a
codebase-wide migration across repos - can finally be expressed.

``decompose_epic`` is a deterministic reference implementation in the same
spirit as :class:`~foundry.engines.analyzer.HeuristicAnalyzer` and the heuristic
risk classifier: no model, fully offline, conservative. It recognises two epic
shapes, in priority order:

1. An explicit **Repositories** section - a heading such as ``Repositories`` /
   ``Repos`` / ``Affected repositories`` followed by bullet lines naming a repo
   and (optionally) its per-repo scope::

       ## Repositories
       - billing-api: migrate the ledger writes
       - customer-web: update the checkout call

   Checkbox markers (``- [ ]`` / ``- [x]``) are tolerated. The repo name is the
   single token before the first ``:``; anything after it is that repo's scope.

2. No such section, but **>= 2 distinct ``known_repositories``** on the ticket -
   one child per repo, each carrying the epic's full description.

Fewer than two distinct repositories => **not an epic** (``is_epic=False``); the
caller runs the ticket as an ordinary single run.

Every child carries the epic's acceptance criteria so each child run is
independently *ready* (and therefore gateable) on its own, and is scoped to
exactly one repo via ``known_repositories`` so enrichment routes it with the
high "explicit ticket association" confidence rather than guessing.
"""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from foundry.engines.analyzer import _extract_acceptance_criteria
from foundry.schemas.ticket import RawTicket

# Heading that opens an explicit per-repo breakdown. Plain text, markdown bold
# (**...**) and ATX headings (## ...) are all tolerated, mirroring the
# acceptance-criteria heading parser in the analyzer.
_REPOS_HEADING = re.compile(
    r"(?:\*{1,2}|#{1,6}\s*)?"
    r"\b(repositories|repos|affected repositories|affected repos|target repos|"
    r"target repositories)\b"
    r"\s*:?\s*(?:\*{1,2})?\s*$",
    re.IGNORECASE,
)

# A bullet line, optionally a task-list checkbox. Group 1 is the bullet content.
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.):])\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$")

# A repo slug: letters, digits and the punctuation real org/repo names use. No
# whitespace - a multi-word "repo" is almost certainly prose, not a repo name,
# so we decline to treat it as one (conservative: better a missed epic than a
# child run pointed at a repo that does not exist).
_REPO_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class EpicDecomposition(BaseModel):
    """Result of attempting to split an epic ticket into per-repo children."""

    model_config = ConfigDict(extra="forbid")

    is_epic: bool
    # One child ticket per scoped repo, in the order they appeared. Empty when
    # ``is_epic`` is False.
    children: list[RawTicket] = Field(default_factory=list)
    # Why we did (or did not) treat the ticket as an epic - mirrors the
    # explainable reason strings used elsewhere (priors, risk evidence).
    reason: str = ""
    # Decisions a reviewer should know about (e.g. acceptance criteria shared
    # across all children), recorded like the analyzer's ``assumptions``.
    assumptions: list[str] = Field(default_factory=list)


def _slug(repo: str) -> str:
    """A filesystem/id-safe token derived from a repo name (``a/b`` -> ``a-b``)."""
    return re.sub(r"[^A-Za-z0-9]+", "-", repo).strip("-").lower()


def _parse_repo_section(description: str) -> list[tuple[str, str]]:
    """Return ``(repo, scope)`` pairs from an explicit Repositories section.

    Deterministic and forgiving: once the heading is seen, consecutive bullet
    lines are collected until a blank line or a non-bullet line ends the
    section (same shape as the analyzer's acceptance-criteria extractor). A
    bullet whose pre-``:`` token is not a repo-like slug is skipped rather than
    guessed at.
    """
    lines = description.splitlines()
    pairs: list[tuple[str, str]] = []
    in_section = False
    for line in lines:
        if _REPOS_HEADING.match(line.strip()):
            in_section = True
            continue
        if not in_section:
            continue
        match = _BULLET.match(line)
        if match:
            content = match.group(1).strip()
            repo, _, scope = content.partition(":")
            repo = repo.strip()
            if _REPO_TOKEN.match(repo):
                pairs.append((repo, scope.strip()))
            continue
        if line.strip() == "":
            continue
        # A non-blank, non-bullet line ends the section.
        break
    return pairs


def _dedupe_first(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep the first occurrence of each repo, preserving order."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for repo, scope in pairs:
        if repo not in seen:
            seen.add(repo)
            out.append((repo, scope))
    return out


def _render_ac_block(criteria: list[str]) -> str:
    if not criteria:
        return ""
    body = "\n".join(f"- {c}" for c in criteria)
    return f"Acceptance Criteria:\n{body}"


def _child_ticket(
    parent: RawTicket,
    *,
    index: int,
    repo: str,
    scope: str,
    ac_block: str,
) -> RawTicket:
    """Build one scoped child ticket from the epic and a (repo, scope) pair."""
    headline = scope or parent.title
    description_parts = [
        headline,
        f"Part of epic {parent.issue_key or parent.issue_id}: {parent.title}",
    ]
    if ac_block:
        description_parts.append(ac_block)
    return RawTicket(
        # Derived, distinct id so each child is its own run under the
        # one-active-run-per-issue index; stable for a given epic + repo.
        issue_id=f"{parent.issue_id}::{_slug(repo)}",
        issue_key=f"{parent.issue_key}-{index}" if parent.issue_key else "",
        title=f"{parent.title} — {repo}",
        description="\n\n".join(description_parts),
        labels=list(parent.labels),
        # Scope to exactly one repo so enrichment routes with explicit-
        # association confidence instead of guessing across several.
        known_repositories=[repo],
    )


def decompose_epic(ticket: RawTicket) -> EpicDecomposition:
    """Split ``ticket`` into per-repo child tickets, or decline (see module doc).

    Pure and deterministic: same ticket in, same decomposition out, no I/O.
    """
    section = _dedupe_first(_parse_repo_section(ticket.description))
    criteria = _extract_acceptance_criteria(ticket.description)
    ac_block = _render_ac_block(criteria)
    assumptions: list[str] = []
    if criteria:
        assumptions.append(
            "epic acceptance criteria applied to every child run"
        )

    if len(section) >= 2:
        children = [
            _child_ticket(
                ticket, index=i + 1, repo=repo, scope=scope, ac_block=ac_block
            )
            for i, (repo, scope) in enumerate(section)
        ]
        repos = ", ".join(repo for repo, _ in section)
        return EpicDecomposition(
            is_epic=True,
            children=children,
            reason=(
                f"explicit repositories section lists {len(section)} repos: {repos}"
            ),
            assumptions=assumptions,
        )

    # Fallback: no structured section, but the ticket is explicitly associated
    # with several repos - decompose one child per repo carrying the full epic.
    repos = _dedupe_first([(r, "") for r in ticket.known_repositories])
    if len(repos) >= 2:
        children = [
            _child_ticket(
                ticket, index=i + 1, repo=repo, scope="", ac_block=ac_block
            )
            for i, (repo, _) in enumerate(repos)
        ]
        names = ", ".join(repo for repo, _ in repos)
        return EpicDecomposition(
            is_epic=True,
            children=children,
            reason=(
                f"ticket is associated with {len(repos)} repositories: {names}"
            ),
            assumptions=assumptions,
        )

    return EpicDecomposition(
        is_epic=False,
        children=[],
        reason=(
            "fewer than two distinct repositories - not an epic; "
            "run as a single ordinary run"
        ),
    )


class EpicDecomposer(Protocol):
    """Split an epic ticket into per-repo children, or decline.

    The orchestrator depends only on this protocol, so a deterministic or an
    LLM-backed decomposer slots in without any other change. Mirrors the
    ``TicketAnalyzer`` / ``DeliveryPlanner`` engine seams.
    """

    def decompose(self, ticket: RawTicket) -> EpicDecomposition: ...


class HeuristicDecomposer:
    """The deterministic :func:`decompose_epic` as an :class:`EpicDecomposer`.

    The no-model, offline reference decomposer and the orchestrator's default.
    :class:`~foundry.engines.llm_decomposition.LlmDecomposer` enriches it (it
    keeps this as a non-overridable floor); see that module.
    """

    def decompose(self, ticket: RawTicket) -> EpicDecomposition:
        return decompose_epic(ticket)
