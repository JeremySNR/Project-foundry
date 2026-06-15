"""Delivery metrics - the audit trail turned into ROI evidence.

Answers the buyer's question over any window: "agents shipped 40 PRs this
quarter, 3 blocked, zero unreviewed escalations, $N spent". Everything is
computed from ``foundry_run_outcomes`` at read time; percentiles are taken in
Python because SQLite has no percentile function and the row counts per
period are small.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from foundry.db.models import FoundryAgentJob, FoundryRun, FoundryRunOutcome
from foundry.memory.priors import routing_prior_rows, smoothed_confidence
from foundry.schemas.common import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    RunStatus,
)

# Buckets supported by ``delivery_trends``. Kept small and explicit so the
# endpoint can validate the query param without trusting caller input.
TREND_BUCKETS = ("day", "week")


def _percentile(sorted_values: list[int], fraction: float) -> int | None:
    if not sorted_values:
        return None
    index = max(math.ceil(fraction * len(sorted_values)) - 1, 0)
    return sorted_values[index]


def _band(confidence: int) -> str:
    low = (confidence // 10) * 10
    return f"{low}-{min(low + 9, 100)}"


def delivery_metrics(session, *, since: datetime) -> dict:
    """Aggregate delivery outcomes for runs that finished at/after ``since``."""
    rows: list[FoundryRunOutcome] = (
        session.query(FoundryRunOutcome)
        .filter(FoundryRunOutcome.completed_at >= since)
        .all()
    )

    outcome_counts: dict[str, int] = {}
    merge_times: list[int] = []
    retries = escalations = ci_failures = 0
    total_cost = 0.0
    cost_seen = False
    blocks_by_reason: dict[str, int] = {}
    bands: dict[str, dict[str, int]] = {}

    for row in rows:
        outcome_counts[row.outcome] = outcome_counts.get(row.outcome, 0) + 1
        retries += max(row.jobs_count - 1, 0)
        escalations += row.escalations_count
        ci_failures += row.ci_failures_count
        if row.cost_usd is not None:
            total_cost += row.cost_usd
            cost_seen = True
        if row.time_to_merge_seconds is not None:
            merge_times.append(row.time_to_merge_seconds)
        if row.outcome == "blocked":
            reason = row.blocked_reason_category or "unknown"
            blocks_by_reason[reason] = blocks_by_reason.get(reason, 0) + 1
        # Calibration evidence: how often did each routing-confidence band
        # actually merge? Only routed runs say anything about routing quality.
        if row.repo is not None and row.routed_confidence is not None:
            band = bands.setdefault(
                _band(row.routed_confidence), {"routed": 0, "merged": 0}
            )
            band["routed"] += 1
            if row.outcome == "merged":
                band["merged"] += 1

    # A block is "justified-so-far" when no later run on the same issue merged;
    # a later merge means a human fixed the input and reran it (supersession).
    blocked_rows = [r for r in rows if r.outcome == "blocked"]
    superseded = 0
    if blocked_rows:
        merge_times_by_issue: dict[str, list] = {}
        merges = (
            session.query(
                FoundryRunOutcome.linear_issue_id, FoundryRunOutcome.created_at_run
            )
            .filter(
                FoundryRunOutcome.outcome == "merged",
                FoundryRunOutcome.linear_issue_id.in_(
                    {b.linear_issue_id for b in blocked_rows}
                ),
            )
            .all()
        )
        for issue_id, created_at_run in merges:
            merge_times_by_issue.setdefault(issue_id, []).append(created_at_run)
        superseded = sum(
            1
            for blocked in blocked_rows
            if any(
                t > blocked.created_at_run
                for t in merge_times_by_issue.get(blocked.linear_issue_id, [])
            )
        )

    merge_times.sort()
    precision_by_band = [
        {
            "band": band,
            "routed": counts["routed"],
            "merged": counts["merged"],
            "precision": round(counts["merged"] / counts["routed"], 3),
        }
        for band, counts in sorted(bands.items())
    ]

    # Top routing priors (all-time, not window-limited: priors only grow).
    top_priors = [
        {
            "issue_key_prefix": prefix,
            "work_type": work_type,
            "repo": repo,
            "routed": routed,
            "merged": merged,
            "confidence": smoothed_confidence(merged, routed, cap=100),
        }
        for prefix, work_type, repo, routed, merged in routing_prior_rows(session)[:10]
    ]

    return {
        "since": since.isoformat(),
        "runs_finished": len(rows),
        "outcomes": outcome_counts,
        "prs_shipped": outcome_counts.get("merged", 0),
        "blocked": outcome_counts.get("blocked", 0),
        "rejected": outcome_counts.get("rejected", 0),
        "failed": outcome_counts.get("failed", 0),
        "needs_clarification": outcome_counts.get("needs_clarification", 0),
        "retries_consumed": retries,
        "escalations": escalations,
        "ci_failures": ci_failures,
        "total_cost_usd": round(total_cost, 2) if cost_seen else None,
        "time_to_merge_seconds": {
            "count": len(merge_times),
            "median": _percentile(merge_times, 0.5),
            "p90": _percentile(merge_times, 0.9),
        },
        "blocks_by_reason": blocks_by_reason,
        "blocked_superseded_by_merged_run": superseded,
        "precision_by_confidence_band": precision_by_band,
        "top_priors": top_priors,
    }


def bucket_start(dt: datetime, bucket: str) -> datetime:
    """Snap a completion time to the start of its day/week (UTC, Monday weeks).

    Rows are stored as timezone-aware UTC, but SQLite hands them back naive;
    treat a naive value as UTC rather than letting ``astimezone`` assume the
    process-local zone (which would shift bucket boundaries non-deterministically).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "week":
        return midnight - timedelta(days=midnight.weekday())
    return midnight


def delivery_trends(session, *, since: datetime, bucket: str = "week") -> dict:
    """Delivery outcomes bucketed over time — the "is throughput trending up
    or down?" view the single-window :func:`delivery_metrics` can't answer.

    Each period reports PRs shipped, blocked runs, total runs finished, retries
    consumed and agent spend. Empty periods inside the window are filled with
    zeros so a sparkline reads as a continuous series rather than a sparse one.
    ``cost_usd`` stays ``None`` for a period where no provider reported cost,
    matching :func:`delivery_metrics` (never conjure a $0 from missing data).
    """
    if bucket not in TREND_BUCKETS:
        raise ValueError(f"bucket must be one of {TREND_BUCKETS}, got {bucket!r}")

    rows: list[FoundryRunOutcome] = (
        session.query(FoundryRunOutcome)
        .filter(FoundryRunOutcome.completed_at >= since)
        .all()
    )

    periods: dict[datetime, dict] = {}
    for row in rows:
        if row.completed_at is None:
            continue
        start = bucket_start(row.completed_at, bucket)
        period = periods.setdefault(
            start,
            {
                "runs_finished": 0,
                "prs_shipped": 0,
                "blocked": 0,
                "retries_consumed": 0,
                "_cost": 0.0,
                "_cost_seen": False,
            },
        )
        period["runs_finished"] += 1
        period["retries_consumed"] += max(row.jobs_count - 1, 0)
        if row.outcome == "merged":
            period["prs_shipped"] += 1
        elif row.outcome == "blocked":
            period["blocked"] += 1
        if row.cost_usd is not None:
            period["_cost"] += row.cost_usd
            period["_cost_seen"] = True

    step = timedelta(days=1 if bucket == "day" else 7)
    series: list[dict] = []
    if periods:
        # Fill every bucket between the first and last *populated* period so a
        # sparkline reads as a continuous series. We stop at the latest data,
        # not wall-clock now, so the series is a pure function of the rows.
        cursor = min(periods)
        last = max(periods)
        while cursor <= last:
            agg = periods.get(cursor)
            if agg is None:
                series.append(
                    {
                        "period_start": cursor.isoformat(),
                        "runs_finished": 0,
                        "prs_shipped": 0,
                        "blocked": 0,
                        "retries_consumed": 0,
                        "total_cost_usd": None,
                    }
                )
            else:
                series.append(
                    {
                        "period_start": cursor.isoformat(),
                        "runs_finished": agg["runs_finished"],
                        "prs_shipped": agg["prs_shipped"],
                        "blocked": agg["blocked"],
                        "retries_consumed": agg["retries_consumed"],
                        "total_cost_usd": round(agg["_cost"], 2)
                        if agg["_cost_seen"]
                        else None,
                    }
                )
            cursor += step

    return {
        "since": since.isoformat(),
        "bucket": bucket,
        "periods": series,
    }


def fleet_status(session) -> dict:
    """Live operational snapshot — every run's *current* state across the org.

    This is the "what are the agents doing right now" view the historical
    delivery metrics can't give: :func:`delivery_metrics` and
    :func:`delivery_trends` aggregate ``FoundryRunOutcome`` (finished runs only)
    over a time window, whereas this reads ``FoundryRun`` live and takes a
    snapshot of *now* — no ``since`` window. It feeds the dashboard's fleet
    strip: runs in flight, the human-approval queue depth, agents currently
    running, PRs open, and spend committed by runs that have not yet finished.

    ``active_cost_usd`` sums provider-reported cost across agent jobs belonging
    to runs still in an active state; it stays ``None`` when no in-flight job
    reported cost (never a conjured ``$0`` from missing data, matching
    :func:`delivery_metrics`).
    """
    by_status: dict[str, int] = {}
    for status, count in (
        session.query(FoundryRun.status, func.count(FoundryRun.id))
        .group_by(FoundryRun.status)
        .all()
    ):
        key = status.value if isinstance(status, RunStatus) else str(status)
        by_status[key] = count

    def _count(*statuses: RunStatus) -> int:
        return sum(by_status.get(s.value, 0) for s in statuses)

    # The human queue: runs parked on a person. ``waiting_approval`` and
    # ``review_required`` are active; ``needs_clarification`` is terminal but
    # re-triggerable and still needs a human to act — mirror the dashboard's
    # approval-queue filter so the strip count matches the filtered list.
    awaiting_human = _count(
        RunStatus.WAITING_APPROVAL,
        RunStatus.REVIEW_REQUIRED,
        RunStatus.NEEDS_CLARIFICATION,
    )

    active_cost = (
        session.query(func.sum(FoundryAgentJob.cost_usd))
        .join(FoundryRun, FoundryAgentJob.run_id == FoundryRun.id)
        .filter(
            FoundryRun.status.in_(ACTIVE_RUN_STATUSES),
            FoundryAgentJob.cost_usd.isnot(None),
        )
        .scalar()
    )

    return {
        "total_runs": sum(by_status.values()),
        "runs_active": _count(*ACTIVE_RUN_STATUSES),
        "runs_terminal": _count(*TERMINAL_RUN_STATUSES),
        "awaiting_human": awaiting_human,
        "agents_running": _count(RunStatus.AGENT_RUNNING),
        "prs_open": _count(RunStatus.PR_OPEN),
        "active_cost_usd": round(active_cost, 2) if active_cost is not None else None,
        "by_status": by_status,
    }
