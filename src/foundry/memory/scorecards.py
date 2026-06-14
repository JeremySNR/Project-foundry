"""Agent scorecards - delivery memory turned from "which repo" into "which agent".

Routing priors (``priors.py``) answer *where* work should go; scorecards answer
*who* should do it. Aggregated purely from ``foundry_run_outcomes`` (no re-join of
the audit trail): per provider, and broken down by work type and repo, the merge
rate, retries consumed, and spend. GitHub will never tell you Cursor outperforms
Copilot on your billing service; Foundry can, with receipts - and it compounds
with every run.

The statistic is deliberately the same one priors use: a Beta(1,1)-smoothed
merge rate (``smoothed_confidence``) so small samples stay honest (3 of 3 reads
83, not 100). Only *dispatched* runs - rows where an agent actually shipped, i.e.
``provider IS NOT NULL`` - count, since a run parked or rejected at intake says
nothing about any agent.

This module is read-only reporting. Acting on the numbers (policy-gated
``agent.provider: auto`` dispatch) is deliberately a separate, gated change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, func

from foundry.db.models import FoundryRunOutcome
from foundry.memory.priors import smoothed_confidence

# Default before a provider's history is considered substantial enough to
# compare - mirrors the priors min-sample floor; here it only annotates a
# scorecard (``meets_min_samples``), it does not hide data.
DEFAULT_MIN_SAMPLES = 3

# (provider, work_type, repo, runs, merged, jobs, cost_sum, cost_count) per group.
_ScorecardRow = tuple[str, str | None, str | None, int, int, int, float | None, int]


def scorecard_rows(session, *, since: datetime | None = None) -> list[_ScorecardRow]:
    """The one scorecard aggregate, shared by the metrics API, CLI and dashboard.

    One row per ``(provider, work_type, repo)`` group over every dispatched
    outcome (``provider IS NOT NULL``), most runs first. ``jobs`` is the summed
    ``jobs_count`` so retries = ``jobs - runs`` (each dispatched run has >= 1
    job); ``cost_sum`` ignores rows whose provider reported no cost and
    ``cost_count`` is how many rows did report one.
    """
    query = session.query(
        FoundryRunOutcome.provider,
        FoundryRunOutcome.work_type,
        FoundryRunOutcome.repo,
        func.count(FoundryRunOutcome.run_id),
        func.sum(case((FoundryRunOutcome.outcome == "merged", 1), else_=0)),
        func.sum(FoundryRunOutcome.jobs_count),
        func.sum(FoundryRunOutcome.cost_usd),
        func.sum(
            case((FoundryRunOutcome.cost_usd.isnot(None), 1), else_=0)
        ),
    ).filter(FoundryRunOutcome.provider.isnot(None))
    if since is not None:
        query = query.filter(FoundryRunOutcome.completed_at >= since)
    rows = (
        query.group_by(
            FoundryRunOutcome.provider,
            FoundryRunOutcome.work_type,
            FoundryRunOutcome.repo,
        )
        .order_by(func.count(FoundryRunOutcome.run_id).desc())
        .all()
    )
    return [
        (
            provider,
            work_type,
            repo,
            int(runs),
            int(merged or 0),
            int(jobs or 0),
            float(cost_sum) if cost_sum is not None else None,
            int(cost_count or 0),
        )
        for provider, work_type, repo, runs, merged, jobs, cost_sum, cost_count in rows
    ]


class _Cell:
    """Mutable accumulator for one provider / work-type / repo bucket."""

    __slots__ = ("runs", "merged", "jobs", "cost_sum", "cost_count")

    def __init__(self) -> None:
        self.runs = 0
        self.merged = 0
        self.jobs = 0
        self.cost_sum = 0.0
        self.cost_count = 0

    def add(self, runs: int, merged: int, jobs: int, cost_sum: float | None, cost_count: int) -> None:
        self.runs += runs
        self.merged += merged
        self.jobs += jobs
        if cost_sum is not None:
            self.cost_sum += cost_sum
        self.cost_count += cost_count

    def stat(self, *, min_samples: int) -> dict:
        retries = max(self.jobs - self.runs, 0)
        return {
            "runs": self.runs,
            "merged": self.merged,
            "success_rate": round(self.merged / self.runs, 3) if self.runs else None,
            # Beta-smoothed so a 2-of-2 doesn't read like a 200-of-200.
            "smoothed_success": smoothed_confidence(self.merged, self.runs, cap=100),
            "retries_consumed": retries,
            "avg_retries": round(retries / self.runs, 2) if self.runs else None,
            "total_cost_usd": (
                round(self.cost_sum, 2) if self.cost_count else None
            ),
            "avg_cost_usd": (
                round(self.cost_sum / self.cost_count, 2) if self.cost_count else None
            ),
            "runs_with_cost": self.cost_count,
            "meets_min_samples": self.runs >= min_samples,
        }


def agent_scorecards(
    session, *, since: datetime | None = None, min_samples: int = DEFAULT_MIN_SAMPLES
) -> dict:
    """Per-provider scorecards with work-type and repo breakdowns.

    Pure read over ``foundry_run_outcomes``. ``min_samples`` only flags whether a
    card has enough history to compare (``meets_min_samples``); nothing is hidden.
    """
    overall: dict[str, _Cell] = {}
    by_work_type: dict[str, dict[str | None, _Cell]] = {}
    by_repo: dict[str, dict[str | None, _Cell]] = {}

    for provider, work_type, repo, runs, merged, jobs, cost_sum, cost_count in (
        scorecard_rows(session, since=since)
    ):
        overall.setdefault(provider, _Cell()).add(
            runs, merged, jobs, cost_sum, cost_count
        )
        by_work_type.setdefault(provider, {}).setdefault(work_type, _Cell()).add(
            runs, merged, jobs, cost_sum, cost_count
        )
        by_repo.setdefault(provider, {}).setdefault(repo, _Cell()).add(
            runs, merged, jobs, cost_sum, cost_count
        )

    def _breakdown(cells: dict, key: str) -> list[dict]:
        return [
            {key: name, **cell.stat(min_samples=min_samples)}
            for name, cell in sorted(
                cells.items(), key=lambda kv: (-kv[1].runs, str(kv[0]))
            )
        ]

    providers = [
        {
            "provider": provider,
            **cell.stat(min_samples=min_samples),
            "by_work_type": _breakdown(by_work_type[provider], "work_type"),
            "by_repo": _breakdown(by_repo[provider], "repo"),
        }
        # Most-used agent first; ties broken by name for determinism.
        for provider, cell in sorted(
            overall.items(), key=lambda kv: (-kv[1].runs, kv[0])
        )
    ]
    return {"min_samples": min_samples, "providers": providers}
