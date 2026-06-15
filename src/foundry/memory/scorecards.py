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

This module is read-only reporting. ``recommend_provider`` turns the same
numbers into a *decision* - which agent should ship a given piece of work -
but it still only reports: it is the selection logic the future policy-gated
``agent.provider: auto`` dispatch will call, surfaced ahead of that change so
the decision can be inspected before anything acts on it. Actually dispatching
on the recommendation remains a deliberately separate, gated change.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import case, func

from foundry.db.models import FoundryRunOutcome
from foundry.memory.metrics import TREND_BUCKETS, bucket_start
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


def _beta_rate(merged: int, runs: int) -> float:
    """Beta(1,1)-smoothed merge rate in [0, 1] (the un-scaled ``smoothed_confidence``)."""
    return (merged + 1) / (runs + 2) if runs >= 0 else 0.0


def recommend_provider(
    session,
    *,
    work_type: str | None = None,
    repo: str | None = None,
    candidates: list[str] | None = None,
    since: datetime | None = None,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict:
    """Recommend the agent provider with the best track record for this work.

    The selection logic the future ``agent.provider: auto`` dispatch will call,
    kept deliberately separate from - and shipped ahead of - that gated change
    so the *decision* can be inspected (via the metrics API and the CLI) before
    anything acts on it. Reporting only: nothing here dispatches.

    Mirrors the routing-priors guard rails (``priors.py``) so the two
    delivery-memory signals behave the same way and stay explainable:

    - **Scope, narrowest-with-evidence first.** With both ``work_type`` and
      ``repo`` given, outcomes for that exact pair are preferred; if the pair
      has fewer than ``min_samples`` runs it falls back to ``work_type`` alone,
      then to all dispatched history - a recommendation always rests on enough
      data, just at a coarser grain when it must.
    - **Per-provider floor.** A provider needs >= ``min_samples`` runs in the
      chosen scope to be *eligible*; thin history still appears in the ranking
      but never wins.
    - **Win on history that mostly worked.** The Beta(1,1)-smoothed merge rate
      must clear 0.5 - an agent that mostly failed is a reason to stay quiet,
      not to route to it.
    - **Candidate allow-list.** ``candidates`` restricts the field to providers
      you can actually dispatch (e.g. the ones with credentials configured), so
      the engine never recommends an agent you cannot use.

    Returns a ranked, fully annotated report. ``recommended`` is the top
    eligible provider's name, or ``None`` when nothing qualifies - always with a
    human-readable ``reason`` suitable for an audit trail.
    """
    allow = set(candidates) if candidates is not None else None

    pair_cells: dict[str, _Cell] = {}
    wt_cells: dict[str, _Cell] = {}
    all_cells: dict[str, _Cell] = {}

    for provider, row_wt, row_repo, runs, merged, jobs, cost_sum, cost_count in (
        scorecard_rows(session, since=since)
    ):
        if allow is not None and provider not in allow:
            continue
        all_cells.setdefault(provider, _Cell()).add(runs, merged, jobs, cost_sum, cost_count)
        if work_type is not None and row_wt == work_type:
            wt_cells.setdefault(provider, _Cell()).add(
                runs, merged, jobs, cost_sum, cost_count
            )
            if repo is not None and row_repo == repo:
                pair_cells.setdefault(provider, _Cell()).add(
                    runs, merged, jobs, cost_sum, cost_count
                )

    # Narrowest scope first; fall through to the next when this one is too thin.
    ladder: list[tuple[str, dict[str, _Cell]]] = []
    if work_type is not None and repo is not None:
        ladder.append((f"{work_type} in {repo}", pair_cells))
    if work_type is not None:
        ladder.append((work_type, wt_cells))
    ladder.append(("all dispatched work", all_cells))

    scope_label, cells = ladder[-1]
    for label, scope_cells in ladder:
        if sum(cell.runs for cell in scope_cells.values()) >= min_samples:
            scope_label, cells = label, scope_cells
            break

    ranked: list[dict] = []
    for provider, cell in cells.items():
        stat = cell.stat(min_samples=min_samples)
        eligible = stat["meets_min_samples"] and _beta_rate(cell.merged, cell.runs) >= 0.5
        ranked.append(
            {
                "provider": provider,
                "eligible": eligible,
                "runs": stat["runs"],
                "merged": stat["merged"],
                "success_rate": stat["success_rate"],
                "smoothed_success": stat["smoothed_success"],
                "avg_cost_usd": stat["avg_cost_usd"],
                "meets_min_samples": stat["meets_min_samples"],
            }
        )

    # Eligible first; then the strongest history; ties broken towards more
    # evidence, then the cheaper agent, then the name (deterministic).
    def _rank_key(card: dict) -> tuple:
        cost = card["avg_cost_usd"]
        return (
            not card["eligible"],
            -(card["smoothed_success"] or 0),
            -card["runs"],
            cost if cost is not None else float("inf"),
            card["provider"],
        )

    ranked.sort(key=_rank_key)

    top = ranked[0] if ranked and ranked[0]["eligible"] else None
    if top is not None:
        recommended = top["provider"]
        reason = (
            f"{top['provider']}: {top['merged']} of {top['runs']} {scope_label} "
            f"runs merged (confidence {top['smoothed_success']})"
        )
        if top["avg_cost_usd"] is not None:
            reason += f", ~${top['avg_cost_usd']}/run"
        reason += "."
    else:
        recommended = None
        reason = (
            f"No agent has >= {min_samples} {scope_label} runs with a "
            "majority-merged history yet; not enough evidence to recommend one."
        )

    return {
        "work_type": work_type,
        "repo": repo,
        "scope": scope_label,
        "min_samples": min_samples,
        "candidates": sorted(allow) if allow is not None else None,
        "recommended": recommended,
        "reason": reason,
        "ranked": ranked,
    }


def _trend_cell(period_start: datetime, cell: _Cell | None) -> dict:
    """One period in a provider's trend series.

    A period the provider had no dispatched run in reports zeros with
    ``success_rate``/``smoothed_success``/``total_cost_usd`` left ``None`` -
    never a conjured 0.5/50 from an empty Beta prior or a $0 from missing cost,
    mirroring how :func:`metrics.delivery_trends` fills empty periods.
    """
    if cell is None or cell.runs == 0:
        return {
            "period_start": period_start.isoformat(),
            "runs": 0,
            "merged": 0,
            "success_rate": None,
            "smoothed_success": None,
            "retries_consumed": 0,
            "total_cost_usd": None,
        }
    stat = cell.stat(min_samples=0)
    return {
        "period_start": period_start.isoformat(),
        "runs": stat["runs"],
        "merged": stat["merged"],
        "success_rate": stat["success_rate"],
        "smoothed_success": stat["smoothed_success"],
        "retries_consumed": stat["retries_consumed"],
        "total_cost_usd": stat["total_cost_usd"],
    }


def agent_scorecard_trends(
    session,
    *,
    since: datetime,
    bucket: str = "week",
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict:
    """Per-provider scorecards bucketed over time - "is this agent improving?".

    The temporal cut of :func:`agent_scorecards`: where that reports one
    merge rate per provider over the whole window (a snapshot), this breaks
    each provider's dispatched outcomes into ``day``/``week`` periods so the
    direction of travel is visible - a flat 70% all-time hides whether an agent
    climbed 50%->90% (route more to it) or slid 90%->50% (pull back). Reporting
    only, like the rest of this module; nothing dispatches.

    Pure read over ``foundry_run_outcomes`` (dispatched rows only, i.e.
    ``provider IS NOT NULL``). Every provider's ``series`` is aligned to one
    shared time axis spanning the first to the last *populated* period (across
    all providers), zero-filled so the sparklines line up and read as
    continuous series. The axis stops at the latest data, not wall-clock now, so
    the result is a pure function of the rows. Each provider also carries its
    window totals (the same shape :func:`agent_scorecards` uses) so a caller can
    label the trend without a second query. ``min_samples`` only annotates the
    window total's ``meets_min_samples`` flag; nothing is hidden.
    """
    if bucket not in TREND_BUCKETS:
        raise ValueError(f"bucket must be one of {TREND_BUCKETS}, got {bucket!r}")

    rows: list[FoundryRunOutcome] = (
        session.query(FoundryRunOutcome)
        .filter(
            FoundryRunOutcome.provider.isnot(None),
            FoundryRunOutcome.completed_at >= since,
        )
        .all()
    )

    # provider -> period_start -> _Cell, plus a per-provider window total.
    per_period: dict[str, dict[datetime, _Cell]] = {}
    totals: dict[str, _Cell] = {}
    for row in rows:
        if row.completed_at is None:
            continue
        start = bucket_start(row.completed_at, bucket)
        merged = 1 if row.outcome == "merged" else 0
        cost_count = 1 if row.cost_usd is not None else 0
        per_period.setdefault(row.provider, {}).setdefault(start, _Cell()).add(
            1, merged, row.jobs_count, row.cost_usd, cost_count
        )
        totals.setdefault(row.provider, _Cell()).add(
            1, merged, row.jobs_count, row.cost_usd, cost_count
        )

    # One shared axis: every bucket between the first and last populated period
    # across all providers, so each provider's series lines up column-for-column.
    populated = [start for periods in per_period.values() for start in periods]
    axis: list[datetime] = []
    if populated:
        step = timedelta(days=1 if bucket == "day" else 7)
        cursor, last = min(populated), max(populated)
        while cursor <= last:
            axis.append(cursor)
            cursor += step

    providers = []
    # Most-used provider first; ties broken by name for determinism (matches
    # agent_scorecards).
    for provider, total in sorted(totals.items(), key=lambda kv: (-kv[1].runs, kv[0])):
        periods = per_period[provider]
        window = total.stat(min_samples=min_samples)
        providers.append(
            {
                "provider": provider,
                "runs": window["runs"],
                "merged": window["merged"],
                "success_rate": window["success_rate"],
                "smoothed_success": window["smoothed_success"],
                "meets_min_samples": window["meets_min_samples"],
                "series": [_trend_cell(start, periods.get(start)) for start in axis],
            }
        )

    return {
        "since": since.isoformat(),
        "bucket": bucket,
        "min_samples": min_samples,
        "periods": [start.isoformat() for start in axis],
        "providers": providers,
    }
