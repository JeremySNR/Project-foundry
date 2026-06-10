"""Delivery metrics - the audit trail turned into ROI evidence.

Answers the buyer's question over any window: "agents shipped 40 PRs this
quarter, 3 blocked, zero unreviewed escalations, $N spent". Everything is
computed from ``foundry_run_outcomes`` at read time; percentiles are taken in
Python because SQLite has no percentile function and the row counts per
period are small.
"""

from __future__ import annotations

import math
from datetime import datetime

from sqlalchemy import case, func

from foundry.db.models import FoundryRunOutcome
from foundry.memory.priors import smoothed_confidence


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
    for blocked in blocked_rows:
        later_merge = (
            session.query(FoundryRunOutcome.run_id)
            .filter(
                FoundryRunOutcome.linear_issue_id == blocked.linear_issue_id,
                FoundryRunOutcome.outcome == "merged",
                FoundryRunOutcome.created_at_run > blocked.created_at_run,
            )
            .first()
        )
        if later_merge is not None:
            superseded += 1

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
    prior_rows = (
        session.query(
            FoundryRunOutcome.issue_key_prefix,
            FoundryRunOutcome.work_type,
            FoundryRunOutcome.repo,
            func.count(FoundryRunOutcome.run_id).label("routed"),
            func.sum(
                case((FoundryRunOutcome.outcome == "merged", 1), else_=0)
            ).label("merged"),
        )
        .filter(FoundryRunOutcome.repo.isnot(None))
        .group_by(
            FoundryRunOutcome.issue_key_prefix,
            FoundryRunOutcome.work_type,
            FoundryRunOutcome.repo,
        )
        .order_by(func.count(FoundryRunOutcome.run_id).desc())
        .limit(10)
        .all()
    )
    top_priors = [
        {
            "issue_key_prefix": prefix,
            "work_type": work_type,
            "repo": repo,
            "routed": int(routed),
            "merged": int(merged or 0),
            "confidence": smoothed_confidence(int(merged or 0), int(routed), cap=100),
        }
        for prefix, work_type, repo, routed, merged in prior_rows
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
