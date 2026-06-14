"""Delivery-memory routing priors - history as a first-class routing signal.

"This team's invoicing tickets landed in billing-service 14 of 16 times" is a
stronger signal than any keyword match, and only Foundry has it because only
Foundry sees the whole ticket->PR loop. The statistic is deliberately simple
and explainable: a Beta(1,1)-smoothed merge rate over *routed* runs (runs
where an agent was actually dispatched to a repo), keyed by the issue-key
prefix (the team proxy) and the analyzed work type, falling back to the
prefix alone when the pair is too thin.

Guard rails, all configurable:

- a minimum sample size before history influences routing at all;
- a confidence cap (default 89) so an explicit repo association on the ticket
  (90) always outranks history;
- the smoothed rate must clear 0.5 - history that mostly failed is a reason
  to stay quiet, not to route.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func

from foundry.memory.outcomes import issue_key_prefix
from foundry.schemas.analysis import TicketAnalysis
from foundry.schemas.context import CandidateRepository
from foundry.schemas.ticket import RawTicket

_log = logging.getLogger(__name__)

# (routed, merged) counts per repo.
_Cell = tuple[int, int]


def smoothed_confidence(merged: int, routed: int, *, cap: int) -> int:
    """Beta(1,1)-smoothed merge rate as a 0-100 confidence, capped.

    14 of 16 merged -> round(100 * 15/18) = 83. Smoothing keeps small samples
    honest: 3 of 3 is 80, not 100.
    """
    return min(cap, round(100 * (merged + 1) / (routed + 2)))


def routing_prior_rows(session) -> list[tuple[str, str | None, str, int, int]]:
    """The one priors aggregate, shared by the enricher, metrics and CLI.

    ``(issue_key_prefix, work_type, repo, routed, merged)`` per group, over
    every outcome where an agent was actually dispatched to a repo, most
    routed first.
    """
    from foundry.db.models import FoundryRunOutcome

    rows = (
        session.query(
            FoundryRunOutcome.issue_key_prefix,
            FoundryRunOutcome.work_type,
            FoundryRunOutcome.repo,
            func.count(FoundryRunOutcome.run_id),
            func.sum(case((FoundryRunOutcome.outcome == "merged", 1), else_=0)),
        )
        .filter(FoundryRunOutcome.repo.isnot(None))
        .group_by(
            FoundryRunOutcome.issue_key_prefix,
            FoundryRunOutcome.work_type,
            FoundryRunOutcome.repo,
        )
        .order_by(func.count(FoundryRunOutcome.run_id).desc())
        .all()
    )
    return [
        (prefix, work_type, repo, int(routed), int(merged or 0))
        for prefix, work_type, repo, routed, merged in rows
    ]


class DeliveryMemoryPriors:
    """Mines ``foundry_run_outcomes`` into candidate-repository priors."""

    def __init__(
        self,
        session_factory: Any,
        *,
        min_samples: int = 3,
        confidence_cap: int = 89,
        cache_ttl_seconds: int = 300,
    ) -> None:
        self._session_factory = session_factory
        self._min_samples = min_samples
        self._confidence_cap = confidence_cap
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cached_at: datetime | None = None
        # (prefix, work_type) -> repo -> (routed, merged); work_type None rows
        # are folded into the prefix-level aggregate only.
        self._by_pair: dict[tuple[str, str], dict[str, _Cell]] = {}
        self._by_prefix: dict[str, dict[str, _Cell]] = {}

    # ------------------------------------------------------------------ stats
    def _stats(self) -> None:
        """(Re)build the in-process aggregate when the cache has expired."""
        now = datetime.now(timezone.utc)
        if (
            self._cached_at is not None
            and (now - self._cached_at).total_seconds() < self._cache_ttl_seconds
        ):
            return
        with self._session_factory() as session:
            rows = routing_prior_rows(session)
        by_pair: dict[tuple[str, str], dict[str, _Cell]] = {}
        by_prefix: dict[str, dict[str, _Cell]] = {}
        for prefix, work_type, repo, routed, merged in rows:
            if not prefix or not repo:
                continue
            if work_type:
                cell = by_pair.setdefault((prefix, work_type), {})
                old = cell.get(repo, (0, 0))
                cell[repo] = (old[0] + routed, old[1] + merged)
            agg = by_prefix.setdefault(prefix, {})
            old = agg.get(repo, (0, 0))
            agg[repo] = (old[0] + routed, old[1] + merged)
        self._by_pair = by_pair
        self._by_prefix = by_prefix
        self._cached_at = now

    def invalidate(self) -> None:
        self._cached_at = None

    # ------------------------------------------------------------- candidates
    def candidates_for(
        self, ticket: RawTicket, analysis: TicketAnalysis
    ) -> list[CandidateRepository]:
        """Historical candidates for this ticket, strongest evidence first."""
        try:
            self._stats()
        except Exception:
            # Priors are an enhancement; routing must work without them.
            _log.exception("delivery-memory priors unavailable; skipping")
            return []

        prefix = issue_key_prefix(ticket.issue_key)
        if not prefix:
            return []
        work_type = analysis.work_type.value

        cells = self._by_pair.get((prefix, work_type), {})
        scope = f"{prefix} {work_type.replace('_', ' ')}"
        if sum(routed for routed, _ in cells.values()) < self._min_samples:
            # The (team, work-type) pair is too thin; fall back to the team.
            cells = self._by_prefix.get(prefix, {})
            scope = prefix

        candidates: list[CandidateRepository] = []
        for repo, (routed, merged) in sorted(cells.items()):
            if routed < self._min_samples:
                continue
            confidence = smoothed_confidence(
                merged, routed, cap=self._confidence_cap
            )
            if (merged + 1) / (routed + 2) < 0.5:
                continue
            candidates.append(
                CandidateRepository(
                    repo=repo,
                    confidence=confidence,
                    reason=(
                        f"Delivery memory: {merged} of {routed} {scope} "
                        "tickets merged in this repository."
                    ),
                )
            )
        candidates.sort(key=lambda c: -c.confidence)
        return candidates
