"""Delivery metrics - the audit trail turned into ROI evidence.

Answers the buyer's question over any window: "agents shipped 40 PRs this
quarter, 3 blocked, zero unreviewed escalations, $N spent". Everything is
computed from ``foundry_run_outcomes`` at read time; percentiles are taken in
Python because SQLite has no percentile function and the row counts per
period are small.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from foundry.db.models import (
    ArtifactType,
    AuditEventType,
    FoundryAgentJob,
    FoundryArtifact,
    FoundryAuditEvent,
    FoundryRun,
    FoundryRunOutcome,
)
from foundry.memory.priors import routing_prior_rows, smoothed_confidence
from foundry.schemas.common import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    RunStatus,
)

# Buckets supported by ``delivery_trends``. Kept small and explicit so the
# endpoint can validate the query param without trusting caller input.
TREND_BUCKETS = ("day", "week")

# Run statuses that mean "parked on a human" - the approval queue. Mirrors the
# dashboard's AWAITING set and ``fleet_status``'s ``awaiting_human`` so the
# three views can never drift on what counts as waiting on a person.
HUMAN_WAIT_STATUSES = (
    RunStatus.WAITING_APPROVAL,
    RunStatus.REVIEW_REQUIRED,
    RunStatus.NEEDS_CLARIFICATION,
)

# Audit events that mark a run *entering* its current human-wait state, mapped
# per status. The approval-queue clock is dated from the *latest* such event -
# i.e. when the run last transitioned into the state it is parked in - not from
# the run row's ``updated_at``. ``updated_at`` carries ``onupdate=utcnow``, so
# *any* later row touch (an N-of-M partial sign-off's APPROVAL_GRANTED, a
# tracker write-back, ...) advances it and would silently reset the SLA clock.
# Audit rows are immutable and never re-dated, so the transition event is the
# faithful wait-start. The markers per parked status:
#   - WAITING_APPROVAL parks at intake, marked by APPROVAL_REQUESTED.
#   - REVIEW_REQUIRED is reached by several paths, each leaving a marker: a
#     diff-aware risk escalation (sensitive area / path-required role / diff too
#     large) -> RISK_ESCALATED; a failed re-dispatch handing the PR back to a
#     human -> AGENT_FAILED; a denied remediation -> RUN_BLOCKED. The latest of
#     these is the current entry (a run can escalate, be retried, re-escalate).
#   - NEEDS_CLARIFICATION is only ever parked at intake and has no dedicated
#     transition event, so it dates from the run's immutable ``created_at``
#     (exact - intake is when it entered the state), handled by the caller.
_WAIT_START_EVENTS_BY_STATUS: dict[RunStatus, tuple[AuditEventType, ...]] = {
    RunStatus.WAITING_APPROVAL: (AuditEventType.APPROVAL_REQUESTED,),
    RunStatus.REVIEW_REQUIRED: (
        AuditEventType.RISK_ESCALATED,
        AuditEventType.AGENT_FAILED,
        AuditEventType.RUN_BLOCKED,
    ),
    RunStatus.NEEDS_CLARIFICATION: (),
}

# The flat set of every marker event, for the single audit-trail query.
_ALL_WAIT_START_EVENTS = tuple(
    {e for events in _WAIT_START_EVENTS_BY_STATUS.values() for e in events}
)

# Run states where Foundry is actively waiting on the *agent* (not a human) and
# spending budget: dispatched to an agent, no PR opened yet. The machine-side
# complement to HUMAN_WAIT_STATUSES. Kept tight to AGENT_RUNNING: PR_OPEN /
# REVIEW_REQUIRED are waiting on CI or reviewers (the product deliberately stops
# at a reviewed PR), not on the agent. The wait clock is dated from the *latest*
# AGENT_STARTED event - the dispatch that put the run in flight, so a retry
# re-dispatch correctly resets the age to the current attempt.
EXECUTION_IN_FLIGHT_STATUSES = (RunStatus.AGENT_RUNNING,)

# Run states where Foundry has shipped a PR and is now waiting on review/CI - the
# review-latency signal ("PRs sitting unreviewed for N hours"). The product
# deliberately *stops at a reviewed PR*, so this is the queue that answers "where
# is delivery stalling on review?". Kept tight to PR_OPEN: REVIEW_REQUIRED is a
# run parked on a *human decision* and is already aged by the approval queue
# (HUMAN_WAIT_STATUSES); AGENT_RUNNING is the agent's own time (the execution
# queue). The review queue ages each PR two complementary ways:
#   - ``unreviewed_seconds`` is dated from the PR_OPENED event - the first time a
#     PR was observed for the run; later pushes emit PR_UPDATED, so a re-push does
#     not reset it (the PR has been open and awaiting review the whole time). This
#     is the "sitting unreviewed for N hours" signal.
#   - ``inactive_seconds`` is dated from the *latest* of PR_OPENED / PR_UPDATED -
#     the last time the PR was observed to change. This is the "stale since last
#     push" signal: it tells an actively-pushed PR (low inactivity) from an
#     abandoned one (high inactivity), which the open-age alone cannot. By
#     construction ``inactive_seconds <= unreviewed_seconds``.
REVIEW_IN_FLIGHT_STATUSES = (RunStatus.PR_OPEN,)

# The PR audit events that count as "the PR changed": its first observation
# (PR_OPENED) and every later push (PR_UPDATED). The latest of these dates the
# staleness clock - see :func:`_pr_activity_since_map`.
_PR_ACTIVITY_EVENTS = (AuditEventType.PR_OPENED, AuditEventType.PR_UPDATED)

# Terminal-failure states a human should triage - the failure-side complement to
# the three in-flight queues above. ``BLOCKED`` is the gate refusing work no
# matter who approves (unroutable, policy-denied, a forbidden-path block, a human
# stop) - sticky and never retried (invariant #7); ``EXECUTION_FAILED`` is a run
# whose agent crashed or produced no PR in its window. Both mean "Foundry stopped
# and a person should look", and unlike the waiting queues a run never *leaves*
# these states (a fresh trigger starts a new run), so the failure queue is bounded
# to a recent window rather than listing every failure ever.
FAILURE_STATUSES = (RunStatus.BLOCKED, RunStatus.EXECUTION_FAILED)

# The audit event that marks a run *entering* each terminal-failure state, mapped
# per status (mirrors :data:`_WAIT_START_EVENTS_BY_STATUS`): ``BLOCKED`` is
# recorded by RUN_BLOCKED, ``EXECUTION_FAILED`` by AGENT_FAILED. The latest such
# event for a run dates *when* it failed and carries *why* in its audit metadata
# (a ``category`` like ``policy_denied``/``pr_window_expired``, or a ``reason``).
_FAILURE_EVENTS_BY_STATUS: dict[RunStatus, tuple[AuditEventType, ...]] = {
    RunStatus.BLOCKED: (AuditEventType.RUN_BLOCKED,),
    RunStatus.EXECUTION_FAILED: (AuditEventType.AGENT_FAILED,),
}

# The flat set of every failure-marker event, for the single audit-trail query.
_ALL_FAILURE_EVENTS = tuple(
    {e for events in _FAILURE_EVENTS_BY_STATUS.values() for e in events}
)


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


# Group label for finished runs that never got routed to a repo (NULL
# ``repo`` - unroutable blocks, needs-clarification, rejected-at-intake). They
# carry real ROI signal (intake-stage attrition), so they bucket under an
# explicit sentinel rather than being silently dropped from the per-repo cut.
UNROUTED_REPO_LABEL = "(unrouted)"

# Group label for finished runs whose work type was never classified (NULL
# ``work_type`` - e.g. rejected-at-intake before analysis settled). Like the
# unrouted-repo sentinel, they carry real signal (intake-stage attrition), so
# they bucket under an explicit label rather than being silently dropped.
UNCLASSIFIED_WORK_TYPE_LABEL = "(unclassified)"


def delivery_by_repo(session, *, since: datetime) -> dict:
    """Delivery outcomes grouped by routed repo - the per-repo cut of
    :func:`delivery_metrics`.

    Answers "which repos are we shipping to, which stall, and where is the
    spend going?" - the repo dimension that the org-wide ``delivery_metrics``
    and the per-provider scorecards don't surface. Read at request time from
    ``foundry_run_outcomes`` (the same denormalized rows the other metrics
    read), so it adds no new write path and can be rebuilt by
    ``foundry-memory backfill``.
    """
    rows: list[FoundryRunOutcome] = (
        session.query(FoundryRunOutcome)
        .filter(FoundryRunOutcome.completed_at >= since)
        .all()
    )

    # Per-repo accumulators. ``cost_seen`` tracks whether *any* row for the repo
    # reported a cost, so a repo whose runs never reported cost yields
    # ``total_cost_usd: None`` (never a conjured $0) - same rule as the org-wide
    # and trend aggregates.
    repos: dict[str, dict] = {}
    for row in rows:
        key = row.repo or UNROUTED_REPO_LABEL
        agg = repos.get(key)
        if agg is None:
            agg = repos[key] = {
                "outcomes": {},
                "retries": 0,
                "escalations": 0,
                "ci_failures": 0,
                "total_cost": 0.0,
                "cost_seen": False,
                "merge_times": [],
                "runs_finished": 0,
            }
        agg["runs_finished"] += 1
        agg["outcomes"][row.outcome] = agg["outcomes"].get(row.outcome, 0) + 1
        agg["retries"] += max(row.jobs_count - 1, 0)
        agg["escalations"] += row.escalations_count
        agg["ci_failures"] += row.ci_failures_count
        if row.cost_usd is not None:
            agg["total_cost"] += row.cost_usd
            agg["cost_seen"] = True
        if row.time_to_merge_seconds is not None:
            agg["merge_times"].append(row.time_to_merge_seconds)

    out_repos = []
    for repo, agg in repos.items():
        merge_times = sorted(agg["merge_times"])
        runs = agg["runs_finished"]
        shipped = agg["outcomes"].get("merged", 0)
        out_repos.append(
            {
                "repo": repo,
                "runs_finished": runs,
                "outcomes": agg["outcomes"],
                "prs_shipped": shipped,
                "blocked": agg["outcomes"].get("blocked", 0),
                "rejected": agg["outcomes"].get("rejected", 0),
                "failed": agg["outcomes"].get("failed", 0),
                "needs_clarification": agg["outcomes"].get("needs_clarification", 0),
                "merge_rate": round(shipped / runs, 3) if runs else 0.0,
                "retries_consumed": agg["retries"],
                "escalations": agg["escalations"],
                "ci_failures": agg["ci_failures"],
                "total_cost_usd": (
                    round(agg["total_cost"], 2) if agg["cost_seen"] else None
                ),
                "time_to_merge_seconds": {
                    "count": len(merge_times),
                    "median": _percentile(merge_times, 0.5),
                    "p90": _percentile(merge_times, 0.9),
                },
            }
        )

    # Most-shipping first, then most-active, with a stable name tie-break so the
    # board ordering is deterministic across requests.
    out_repos.sort(key=lambda r: (-r["prs_shipped"], -r["runs_finished"], r["repo"]))

    return {
        "since": since.isoformat(),
        "runs_finished": len(rows),
        "repos": out_repos,
    }


def delivery_by_work_type(session, *, since: datetime) -> dict:
    """Delivery outcomes grouped by work type - the per-work-type cut of
    :func:`delivery_metrics`, the way :func:`delivery_by_repo` is the per-repo
    cut.

    Answers "which *kinds* of work do we ship reliably, which stall, and where
    does the spend go?" - do bugs sail through while features stall, is the
    retry/escalation budget eaten by tech-debt runs? - the work-type dimension
    the org-wide ``delivery_metrics`` and the per-repo cut don't surface. Read
    at request time from ``foundry_run_outcomes`` (the same denormalized rows
    the other delivery cuts read; ``work_type`` is a stored, indexed column),
    so it adds no new write path and rebuilds with ``foundry-memory backfill``.

    Runs whose ``work_type`` was never classified (NULL) bucket under the
    explicit :data:`UNCLASSIFIED_WORK_TYPE_LABEL` sentinel rather than being
    dropped, mirroring how :func:`delivery_by_repo` handles unrouted runs.
    """
    rows: list[FoundryRunOutcome] = (
        session.query(FoundryRunOutcome)
        .filter(FoundryRunOutcome.completed_at >= since)
        .all()
    )

    # Per-work-type accumulators. ``cost_seen`` tracks whether *any* row for the
    # type reported a cost, so a type whose runs never reported cost yields
    # ``total_cost_usd: None`` (never a conjured $0) - same rule as the org-wide,
    # per-repo and trend aggregates.
    types: dict[str, dict] = {}
    for row in rows:
        key = row.work_type or UNCLASSIFIED_WORK_TYPE_LABEL
        agg = types.get(key)
        if agg is None:
            agg = types[key] = {
                "outcomes": {},
                "retries": 0,
                "escalations": 0,
                "ci_failures": 0,
                "total_cost": 0.0,
                "cost_seen": False,
                "merge_times": [],
                "runs_finished": 0,
            }
        agg["runs_finished"] += 1
        agg["outcomes"][row.outcome] = agg["outcomes"].get(row.outcome, 0) + 1
        agg["retries"] += max(row.jobs_count - 1, 0)
        agg["escalations"] += row.escalations_count
        agg["ci_failures"] += row.ci_failures_count
        if row.cost_usd is not None:
            agg["total_cost"] += row.cost_usd
            agg["cost_seen"] = True
        if row.time_to_merge_seconds is not None:
            agg["merge_times"].append(row.time_to_merge_seconds)

    out_types = []
    for work_type, agg in types.items():
        merge_times = sorted(agg["merge_times"])
        runs = agg["runs_finished"]
        shipped = agg["outcomes"].get("merged", 0)
        out_types.append(
            {
                "work_type": work_type,
                "runs_finished": runs,
                "outcomes": agg["outcomes"],
                "prs_shipped": shipped,
                "blocked": agg["outcomes"].get("blocked", 0),
                "rejected": agg["outcomes"].get("rejected", 0),
                "failed": agg["outcomes"].get("failed", 0),
                "needs_clarification": agg["outcomes"].get("needs_clarification", 0),
                "merge_rate": round(shipped / runs, 3) if runs else 0.0,
                "retries_consumed": agg["retries"],
                "escalations": agg["escalations"],
                "ci_failures": agg["ci_failures"],
                "total_cost_usd": (
                    round(agg["total_cost"], 2) if agg["cost_seen"] else None
                ),
                "time_to_merge_seconds": {
                    "count": len(merge_times),
                    "median": _percentile(merge_times, 0.5),
                    "p90": _percentile(merge_times, 0.9),
                },
            }
        )

    # Most-shipping first, then most-active, with a stable name tie-break so the
    # board ordering is deterministic across requests - same order as the
    # per-repo cut.
    out_types.sort(
        key=lambda r: (-r["prs_shipped"], -r["runs_finished"], r["work_type"])
    )

    return {
        "since": since.isoformat(),
        "runs_finished": len(rows),
        "work_types": out_types,
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


def _empty_delivery_period() -> dict:
    """A fresh per-period accumulator for the delivery-trend aggregations.

    ``_cost`` / ``_cost_seen`` are private: a period where no run reported cost
    must render ``total_cost_usd: None`` (never a conjured $0), matching
    :func:`delivery_metrics`. :func:`_render_delivery_period` strips them.
    """
    return {
        "runs_finished": 0,
        "prs_shipped": 0,
        "blocked": 0,
        "retries_consumed": 0,
        "_cost": 0.0,
        "_cost_seen": False,
    }


def _accumulate_delivery_period(agg: dict, row: FoundryRunOutcome) -> None:
    """Fold one finished-run outcome into a period accumulator."""
    agg["runs_finished"] += 1
    agg["retries_consumed"] += max(row.jobs_count - 1, 0)
    if row.outcome == "merged":
        agg["prs_shipped"] += 1
    elif row.outcome == "blocked":
        agg["blocked"] += 1
    if row.cost_usd is not None:
        agg["_cost"] += row.cost_usd
        agg["_cost_seen"] = True


def _render_delivery_period(period_start: datetime, agg: dict | None) -> dict:
    """Render a period accumulator to its public shape (``None`` agg = an empty,
    zero-filled bucket so a sparkline reads as a continuous series)."""
    agg = agg or _empty_delivery_period()
    return {
        "period_start": period_start.isoformat(),
        "runs_finished": agg["runs_finished"],
        "prs_shipped": agg["prs_shipped"],
        "blocked": agg["blocked"],
        "retries_consumed": agg["retries_consumed"],
        "total_cost_usd": round(agg["_cost"], 2) if agg["_cost_seen"] else None,
    }


def _delivery_axis(populated: list[datetime], bucket: str) -> list[datetime]:
    """The shared, gap-filled time axis spanning the first to the last populated
    period. Stops at the latest data, not wall-clock now, so the series is a pure
    function of the rows."""
    if not populated:
        return []
    step = timedelta(days=1 if bucket == "day" else 7)
    axis: list[datetime] = []
    cursor, last = min(populated), max(populated)
    while cursor <= last:
        axis.append(cursor)
        cursor += step
    return axis


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
        _accumulate_delivery_period(
            periods.setdefault(start, _empty_delivery_period()), row
        )

    axis = _delivery_axis(list(periods), bucket)
    return {
        "since": since.isoformat(),
        "bucket": bucket,
        "periods": [_render_delivery_period(start, periods.get(start)) for start in axis],
    }


def delivery_by_repo_trends(session, *, since: datetime, bucket: str = "week") -> dict:
    """Per-repo delivery outcomes bucketed over time — the repo dimension of
    :func:`delivery_trends`, the way :func:`delivery_by_repo` is to
    :func:`delivery_metrics`.

    Answers "is *this repo* shipping more or stalling over time?" — which the
    org-wide :func:`delivery_trends` (one series across every repo) and the
    point-in-time :func:`delivery_by_repo` (one window total per repo, no
    direction of travel) can't show on their own. Pure read over
    ``foundry_run_outcomes`` (the same finished-run rows the other delivery cuts
    read), so it adds no new write path and rebuilds with ``foundry-memory
    backfill``.

    Every repo's ``series`` is aligned to one shared time axis spanning the
    first to the last *populated* period (across all repos), zero-filled so the
    per-repo sparklines line up column-for-column — the same shape
    :func:`~foundry.memory.scorecards.agent_scorecard_trends` uses. Each repo
    also carries its window totals so a caller can label the trend without a
    second query. Unrouted runs (NULL ``repo``) bucket under the
    :data:`UNROUTED_REPO_LABEL` sentinel, as in :func:`delivery_by_repo`.
    """
    if bucket not in TREND_BUCKETS:
        raise ValueError(f"bucket must be one of {TREND_BUCKETS}, got {bucket!r}")

    rows: list[FoundryRunOutcome] = (
        session.query(FoundryRunOutcome)
        .filter(FoundryRunOutcome.completed_at >= since)
        .all()
    )

    # repo -> period_start -> accumulator, plus a per-repo window total.
    per_period: dict[str, dict[datetime, dict]] = {}
    totals: dict[str, dict] = {}
    for row in rows:
        if row.completed_at is None:
            continue
        key = row.repo or UNROUTED_REPO_LABEL
        start = bucket_start(row.completed_at, bucket)
        _accumulate_delivery_period(
            per_period.setdefault(key, {}).setdefault(start, _empty_delivery_period()),
            row,
        )
        _accumulate_delivery_period(totals.setdefault(key, _empty_delivery_period()), row)

    # One shared axis across every repo so the per-repo series align.
    populated = [start for periods in per_period.values() for start in periods]
    axis = _delivery_axis(populated, bucket)

    out_repos = []
    for repo, total in totals.items():
        runs = total["runs_finished"]
        shipped = total["prs_shipped"]
        periods = per_period[repo]
        out_repos.append(
            {
                "repo": repo,
                "runs_finished": runs,
                "prs_shipped": shipped,
                "blocked": total["blocked"],
                "merge_rate": round(shipped / runs, 3) if runs else 0.0,
                "retries_consumed": total["retries_consumed"],
                "total_cost_usd": (
                    round(total["_cost"], 2) if total["_cost_seen"] else None
                ),
                "series": [
                    _render_delivery_period(start, periods.get(start)) for start in axis
                ],
            }
        )

    # Most-shipping first, then most-active, with a stable name tie-break - the
    # same ordering as delivery_by_repo so the two repo cuts read consistently.
    out_repos.sort(key=lambda r: (-r["prs_shipped"], -r["runs_finished"], r["repo"]))

    return {
        "since": since.isoformat(),
        "bucket": bucket,
        "periods": [start.isoformat() for start in axis],
        "repos": out_repos,
    }


def delivery_by_work_type_trends(
    session, *, since: datetime, bucket: str = "week"
) -> dict:
    """Per-work-type delivery outcomes bucketed over time — the work-type
    dimension of :func:`delivery_trends`, the way :func:`delivery_by_work_type`
    is to :func:`delivery_metrics` and :func:`delivery_by_repo_trends` is to
    :func:`delivery_by_repo`.

    Answers "is *this kind of work* shipping more reliably or stalling over
    time?" — do features sail while bugs slip, is the merge rate for tech-debt
    runs climbing? — which the single-window :func:`delivery_by_work_type` (one
    total per type, no direction of travel) and the org-wide
    :func:`delivery_trends` (one series across every work type) can't show on
    their own. Pure read over ``foundry_run_outcomes`` (the same finished-run
    rows the other delivery cuts read; ``work_type`` is a stored column), so it
    adds no new write path and rebuilds with ``foundry-memory backfill``.

    Every work type's ``series`` is aligned to one shared time axis spanning the
    first to the last *populated* period (across all types), zero-filled so the
    per-type sparklines line up column-for-column — the same shape
    :func:`delivery_by_repo_trends` uses. Each type also carries its window
    totals so a caller can label the trend without a second query. Runs whose
    ``work_type`` was never classified (NULL) bucket under the
    :data:`UNCLASSIFIED_WORK_TYPE_LABEL` sentinel, as in
    :func:`delivery_by_work_type`.
    """
    if bucket not in TREND_BUCKETS:
        raise ValueError(f"bucket must be one of {TREND_BUCKETS}, got {bucket!r}")

    rows: list[FoundryRunOutcome] = (
        session.query(FoundryRunOutcome)
        .filter(FoundryRunOutcome.completed_at >= since)
        .all()
    )

    # work_type -> period_start -> accumulator, plus a per-type window total.
    per_period: dict[str, dict[datetime, dict]] = {}
    totals: dict[str, dict] = {}
    for row in rows:
        if row.completed_at is None:
            continue
        key = row.work_type or UNCLASSIFIED_WORK_TYPE_LABEL
        start = bucket_start(row.completed_at, bucket)
        _accumulate_delivery_period(
            per_period.setdefault(key, {}).setdefault(start, _empty_delivery_period()),
            row,
        )
        _accumulate_delivery_period(totals.setdefault(key, _empty_delivery_period()), row)

    # One shared axis across every work type so the per-type series align.
    populated = [start for periods in per_period.values() for start in periods]
    axis = _delivery_axis(populated, bucket)

    out_types = []
    for work_type, total in totals.items():
        runs = total["runs_finished"]
        shipped = total["prs_shipped"]
        periods = per_period[work_type]
        out_types.append(
            {
                "work_type": work_type,
                "runs_finished": runs,
                "prs_shipped": shipped,
                "blocked": total["blocked"],
                "merge_rate": round(shipped / runs, 3) if runs else 0.0,
                "retries_consumed": total["retries_consumed"],
                "total_cost_usd": (
                    round(total["_cost"], 2) if total["_cost_seen"] else None
                ),
                "series": [
                    _render_delivery_period(start, periods.get(start)) for start in axis
                ],
            }
        )

    # Most-shipping first, then most-active, with a stable name tie-break - the
    # same ordering as delivery_by_work_type so the two work-type cuts read
    # consistently.
    out_types.sort(
        key=lambda r: (-r["prs_shipped"], -r["runs_finished"], r["work_type"])
    )

    return {
        "since": since.isoformat(),
        "bucket": bucket,
        "periods": [start.isoformat() for start in axis],
        "work_types": out_types,
    }


def _as_utc(dt: datetime) -> datetime:
    """Normalise a stored timestamp to UTC-aware (SQLite hands them back naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _wait_since_map(session, runs: list[FoundryRun]) -> dict[str, datetime]:
    """For each run, the time it last transitioned into its current human-wait
    state - the latest marker event *valid for that run's status*.

    Markers are looked up per status (:data:`_WAIT_START_EVENTS_BY_STATUS`) so a
    generic event (e.g. ``RUN_BLOCKED``) only dates the state it actually marks
    the entry of. Runs whose status has no marker - or that carry none yet - are
    absent from the map; the caller falls back to the run's immutable
    ``created_at``.
    """
    if not runs:
        return {}
    # Latest created_at per (run, event_type) over just the marker event types.
    rows = (
        session.query(
            FoundryAuditEvent.run_id,
            FoundryAuditEvent.event_type,
            func.max(FoundryAuditEvent.created_at),
        )
        .filter(
            FoundryAuditEvent.run_id.in_([r.id for r in runs]),
            FoundryAuditEvent.event_type.in_(_ALL_WAIT_START_EVENTS),
        )
        .group_by(FoundryAuditEvent.run_id, FoundryAuditEvent.event_type)
        .all()
    )
    latest_by_run_event: dict[tuple[str, AuditEventType], datetime] = {
        (run_id, event_type): created_at
        for run_id, event_type, created_at in rows
        if created_at is not None
    }

    wait_since: dict[str, datetime] = {}
    for run in runs:
        markers = _WAIT_START_EVENTS_BY_STATUS.get(run.status, ())
        candidates = [
            latest_by_run_event[(run.id, et)]
            for et in markers
            if (run.id, et) in latest_by_run_event
        ]
        if candidates:
            wait_since[run.id] = max(candidates)
    return wait_since


def approval_queue(
    session, *, now: datetime | None = None, sla_seconds: int | None = None
) -> dict:
    """The human-approval queue with per-run wait age - the drill-down behind
    the fleet strip's ``awaiting_human`` count.

    Every run currently parked on a person (:data:`HUMAN_WAIT_STATUSES`), oldest
    first, each with how long it has been waiting (``waiting_seconds``, derived
    from the audit trail - see :data:`_WAIT_START_EVENTS_BY_STATUS`). When
    ``sla_seconds``
    is set, runs waiting at least that long are flagged ``sla_breached`` and
    counted in the ``sla_breaches`` summary; with no SLA the breach signal is
    inert (``sla_breaches`` is 0), matching the historical behaviour.
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    runs: list[FoundryRun] = (
        session.query(FoundryRun)
        .filter(FoundryRun.status.in_(HUMAN_WAIT_STATUSES))
        .all()
    )
    wait_since = _wait_since_map(session, runs)

    entries: list[dict] = []
    for run in runs:
        # Fall back to the immutable created_at (not the drift-prone updated_at)
        # when the run's status carries no transition marker - today only
        # NEEDS_CLARIFICATION, which is always parked at intake, so created_at is
        # exactly when it entered the state.
        since = wait_since.get(run.id) or run.created_at
        # Clamp at 0: a future-dated row (clock skew) is not negative wait time.
        waited = max(0, int((now - _as_utc(since)).total_seconds()))
        breached = sla_seconds is not None and waited >= sla_seconds
        status = run.status.value if isinstance(run.status, RunStatus) else str(run.status)
        entries.append(
            {
                "run_id": run.id,
                "linear_issue_key": run.linear_issue_key,
                "status": status,
                "current_step": run.current_step,
                "risk_level": run.risk_level.value if run.risk_level is not None else None,
                "waiting_since": _as_utc(since).isoformat(),
                "waiting_seconds": waited,
                "sla_breached": breached,
            }
        )

    entries.sort(key=lambda e: e["waiting_seconds"], reverse=True)
    breaches = sum(1 for e in entries if e["sla_breached"])
    return {
        "now": now.isoformat(),
        "sla_seconds": sla_seconds,
        "count": len(entries),
        "oldest_wait_seconds": entries[0]["waiting_seconds"] if entries else None,
        "sla_breaches": breaches,
        "runs": entries,
    }


def _dispatch_since_map(session, runs: list[FoundryRun]) -> dict[str, datetime]:
    """For each run, the time it was last dispatched to an agent - the latest
    ``AGENT_STARTED`` event. A run can be re-dispatched (a remediation retry),
    so the *latest* such event is when the *current* in-flight attempt began.

    Runs that carry no dispatch event yet are absent from the map; the caller
    falls back to the run's immutable ``created_at``.
    """
    if not runs:
        return {}
    rows = (
        session.query(
            FoundryAuditEvent.run_id,
            func.max(FoundryAuditEvent.created_at),
        )
        .filter(
            FoundryAuditEvent.run_id.in_([r.id for r in runs]),
            FoundryAuditEvent.event_type == AuditEventType.AGENT_STARTED,
        )
        .group_by(FoundryAuditEvent.run_id)
        .all()
    )
    return {run_id: created_at for run_id, created_at in rows if created_at is not None}


def execution_queue(
    session, *, now: datetime | None = None, sla_seconds: int | None = None
) -> dict:
    """In-flight agent runs with per-run run-time age - the machine-state
    complement to :func:`approval_queue`, and the drill-down behind the fleet
    strip's ``agents_running`` count.

    Every run currently dispatched to an agent and not yet at a PR
    (:data:`EXECUTION_IN_FLIGHT_STATUSES`), oldest first, each with how long it
    has been running (``running_seconds``, dated from the latest
    ``AGENT_STARTED`` audit event - the dispatch that put it in flight). When
    ``sla_seconds`` is set, runs running at least that long are flagged
    ``sla_breached`` and counted in the ``sla_breaches`` summary - the signal a
    VP/on-call wants for a hung or runaway agent silently burning budget; with
    no SLA the breach signal is inert (``sla_breaches`` is 0), matching the
    historical behaviour byte-for-byte.
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    runs: list[FoundryRun] = (
        session.query(FoundryRun)
        .filter(FoundryRun.status.in_(EXECUTION_IN_FLIGHT_STATUSES))
        .all()
    )
    dispatched_since = _dispatch_since_map(session, runs)

    entries: list[dict] = []
    for run in runs:
        # Fall back to the immutable created_at (not the drift-prone updated_at)
        # when a run somehow has no dispatch event recorded yet.
        since = dispatched_since.get(run.id) or run.created_at
        # Clamp at 0: a future-dated row (clock skew) is not negative run time.
        ran = max(0, int((now - _as_utc(since)).total_seconds()))
        breached = sla_seconds is not None and ran >= sla_seconds
        status = run.status.value if isinstance(run.status, RunStatus) else str(run.status)
        entries.append(
            {
                "run_id": run.id,
                "linear_issue_key": run.linear_issue_key,
                "status": status,
                "current_step": run.current_step,
                "risk_level": run.risk_level.value if run.risk_level is not None else None,
                "running_since": _as_utc(since).isoformat(),
                "running_seconds": ran,
                "sla_breached": breached,
            }
        )

    entries.sort(key=lambda e: e["running_seconds"], reverse=True)
    breaches = sum(1 for e in entries if e["sla_breached"])
    return {
        "now": now.isoformat(),
        "sla_seconds": sla_seconds,
        "count": len(entries),
        "oldest_running_seconds": entries[0]["running_seconds"] if entries else None,
        "sla_breaches": breaches,
        "runs": entries,
    }


def _pr_opened_since_map(session, runs: list[FoundryRun]) -> dict[str, datetime]:
    """For each run, the time its PR was first opened - the ``PR_OPENED`` event.

    A run sees ``PR_OPENED`` once (the first PR observation); subsequent pushes
    emit ``PR_UPDATED``, so the review clock is anchored to when the PR opened and
    is *not* reset by a later push (the PR has been awaiting review the whole
    time). ``func.max`` is defensive - there is normally a single ``PR_OPENED``.

    Runs that carry no ``PR_OPENED`` event yet are absent from the map; the caller
    falls back to the run's immutable ``created_at``.
    """
    if not runs:
        return {}
    rows = (
        session.query(
            FoundryAuditEvent.run_id,
            func.max(FoundryAuditEvent.created_at),
        )
        .filter(
            FoundryAuditEvent.run_id.in_([r.id for r in runs]),
            FoundryAuditEvent.event_type == AuditEventType.PR_OPENED,
        )
        .group_by(FoundryAuditEvent.run_id)
        .all()
    )
    return {run_id: created_at for run_id, created_at in rows if created_at is not None}


def _pr_activity_since_map(session, runs: list[FoundryRun]) -> dict[str, datetime]:
    """For each run, the time its PR was last observed to change - the *latest* of
    its ``PR_OPENED`` / ``PR_UPDATED`` events.

    Where :func:`_pr_opened_since_map` anchors to when the PR *opened* (so a later
    push never resets the review clock), this anchors to the *most recent* push -
    the "stale since last push" clock. A PR pushed to a minute ago is fresh even
    if it opened days ago; one with no activity since it opened is stale. ``func.max``
    takes the latest event across both types.

    Runs that carry no PR activity event yet are absent from the map; the caller
    falls back to the run's immutable ``created_at``.
    """
    if not runs:
        return {}
    rows = (
        session.query(
            FoundryAuditEvent.run_id,
            func.max(FoundryAuditEvent.created_at),
        )
        .filter(
            FoundryAuditEvent.run_id.in_([r.id for r in runs]),
            FoundryAuditEvent.event_type.in_(_PR_ACTIVITY_EVENTS),
        )
        .group_by(FoundryAuditEvent.run_id)
        .all()
    )
    return {run_id: created_at for run_id, created_at in rows if created_at is not None}


def review_queue(
    session,
    *,
    now: datetime | None = None,
    sla_seconds: int | None = None,
    stale_sla_seconds: int | None = None,
) -> dict:
    """Open PRs with per-run review-latency age - the drill-down behind the fleet
    strip's ``prs_open`` count, and the review-side complement to
    :func:`approval_queue` / :func:`execution_queue`.

    Every run currently sitting at an open PR (:data:`REVIEW_IN_FLIGHT_STATUSES`),
    oldest first, each aged two complementary ways:

    - ``unreviewed_seconds`` - how long the PR has been awaiting review, dated from
      the ``PR_OPENED`` audit event (when the PR first opened). When ``sla_seconds``
      is set, PRs open at least that long are flagged ``sla_breached`` and counted
      in ``sla_breaches`` - the "sitting unreviewed for N hours" signal.
    - ``inactive_seconds`` - how long since the PR last changed, dated from the
      *latest* of ``PR_OPENED`` / ``PR_UPDATED``. When ``stale_sla_seconds`` is set,
      PRs idle at least that long are flagged ``stale_breached`` and counted in
      ``stale_breaches`` - the "stale since last push" signal that tells an
      actively-pushed PR from an abandoned one. By construction
      ``inactive_seconds <= unreviewed_seconds``.

    With no SLA the corresponding breach signal is inert (its count is 0), matching
    the historical behaviour byte-for-byte; the ages are still reported. The product
    deliberately stops at a reviewed PR, so this is pure read-only visibility - it
    blocks no run and merges nothing. The queue is ordered by ``unreviewed_seconds``
    (the headline review-latency age), oldest first.
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    runs: list[FoundryRun] = (
        session.query(FoundryRun)
        .filter(FoundryRun.status.in_(REVIEW_IN_FLIGHT_STATUSES))
        .all()
    )
    opened_since = _pr_opened_since_map(session, runs)
    activity_since = _pr_activity_since_map(session, runs)

    entries: list[dict] = []
    for run in runs:
        # Fall back to the immutable created_at (not the drift-prone updated_at)
        # when a run somehow has no PR_OPENED event recorded yet.
        since = opened_since.get(run.id) or run.created_at
        # Staleness is dated from the last push; with no activity event recorded it
        # collapses to the open time, so a PR with only a PR_OPENED reads as idle
        # since it opened (inactive == unreviewed) - never more idle than open.
        active_since = activity_since.get(run.id) or since
        # Clamp at 0: a future-dated row (clock skew) is not negative review time.
        unreviewed = max(0, int((now - _as_utc(since)).total_seconds()))
        inactive = max(0, int((now - _as_utc(active_since)).total_seconds()))
        breached = sla_seconds is not None and unreviewed >= sla_seconds
        stale = stale_sla_seconds is not None and inactive >= stale_sla_seconds
        status = run.status.value if isinstance(run.status, RunStatus) else str(run.status)
        entries.append(
            {
                "run_id": run.id,
                "linear_issue_key": run.linear_issue_key,
                "status": status,
                "current_step": run.current_step,
                "risk_level": run.risk_level.value if run.risk_level is not None else None,
                "pr_opened_since": _as_utc(since).isoformat(),
                "unreviewed_seconds": unreviewed,
                "sla_breached": breached,
                "last_activity_since": _as_utc(active_since).isoformat(),
                "inactive_seconds": inactive,
                "stale_breached": stale,
            }
        )

    entries.sort(key=lambda e: e["unreviewed_seconds"], reverse=True)
    breaches = sum(1 for e in entries if e["sla_breached"])
    stale_breaches = sum(1 for e in entries if e["stale_breached"])
    return {
        "now": now.isoformat(),
        "sla_seconds": sla_seconds,
        "stale_sla_seconds": stale_sla_seconds,
        "count": len(entries),
        "oldest_unreviewed_seconds": entries[0]["unreviewed_seconds"] if entries else None,
        # The most-stale PR (max inactivity), independent of the oldest-open one.
        "oldest_inactive_seconds": (
            max((e["inactive_seconds"] for e in entries), default=None)
        ),
        "sla_breaches": breaches,
        "stale_breaches": stale_breaches,
        "runs": entries,
    }


def _failure_reason(metadata_json: str | None) -> str | None:
    """The human-readable failure reason from a failure event's audit metadata -
    its ``category`` (e.g. ``policy_denied``, ``unroutable``, ``forbidden_path``,
    ``pr_window_expired``, ``human_stopped``) or, failing that, its ``reason``
    string.

    Returns ``None`` when there is no metadata or it can't be parsed - this is a
    read-only reporting path and must never raise on a malformed row.
    """
    if not metadata_json:
        return None
    try:
        meta = json.loads(metadata_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None
    value = meta.get("category") or meta.get("reason")
    return value if isinstance(value, str) else None


def _failure_event_map(
    session, runs: list[FoundryRun]
) -> dict[str, tuple[datetime, str | None]]:
    """For each failed run, the ``(time, reason)`` it entered its terminal-failure
    state - the latest marker event *valid for that run's status*
    (:data:`_FAILURE_EVENTS_BY_STATUS`) and the reason from that event's metadata.

    A generic event (RUN_BLOCKED, AGENT_FAILED) is only honoured for the status it
    actually marks the entry of, so an AGENT_FAILED left on a run that later got
    BLOCKED doesn't date the block. Runs that carry no marker event are absent
    from the map; the caller falls back to the run's immutable ``created_at`` with
    an unknown reason.
    """
    if not runs:
        return {}
    rows = (
        session.query(
            FoundryAuditEvent.run_id,
            FoundryAuditEvent.event_type,
            FoundryAuditEvent.created_at,
            FoundryAuditEvent.metadata_json,
        )
        .filter(
            FoundryAuditEvent.run_id.in_([r.id for r in runs]),
            FoundryAuditEvent.event_type.in_(_ALL_FAILURE_EVENTS),
        )
        .all()
    )
    by_run: dict[str, list[tuple[AuditEventType, datetime, str | None]]] = {}
    for run_id, event_type, created_at, metadata_json in rows:
        if created_at is not None:
            by_run.setdefault(run_id, []).append((event_type, created_at, metadata_json))

    result: dict[str, tuple[datetime, str | None]] = {}
    for run in runs:
        markers = _FAILURE_EVENTS_BY_STATUS.get(run.status, ())
        candidates = [
            (created_at, metadata_json)
            for (event_type, created_at, metadata_json) in by_run.get(run.id, [])
            if event_type in markers
        ]
        if candidates:
            created_at, metadata_json = max(candidates, key=lambda c: c[0])
            result[run.id] = (created_at, _failure_reason(metadata_json))
    return result


def _run_repo_map(session, runs: list[FoundryRun]) -> dict[str, str | None]:
    """For each run, the repo the work landed in - the latest agent job's ``repo``.

    ``FoundryRun`` carries no ``repo`` column (it lives on the per-dispatch
    ``FoundryAgentJob``), so this mirrors the exact "latest job's repo" derivation
    :func:`foundry.memory.outcomes.record_outcome` uses to stamp
    ``FoundryRunOutcome.repo``: jobs ordered with unstarted (NULL ``started_at``)
    first, then by ``started_at`` then ``id`` (deterministic on SQLite *and*
    Postgres), and the most recent job carrying a repo wins. A run that never
    dispatched an agent (parked / blocked at the gate before routing) has no job
    and maps to ``None`` - correctly counted as unrouted by the caller.
    """
    if not runs:
        return {}
    jobs = (
        session.query(FoundryAgentJob)
        .filter(FoundryAgentJob.run_id.in_([r.id for r in runs]))
        # Match record_outcome's backend-independent ordering exactly so the repo
        # this picks is the same one stamped on the outcome row.
        .order_by(
            FoundryAgentJob.started_at.is_(None).desc(),
            FoundryAgentJob.started_at,
            FoundryAgentJob.id,
        )
        .all()
    )
    by_run: dict[str, list[FoundryAgentJob]] = {}
    for job in jobs:
        by_run.setdefault(job.run_id, []).append(job)
    return {
        run.id: next((j.repo for j in reversed(by_run.get(run.id, [])) if j.repo), None)
        for run in runs
    }


def _run_work_type_map(session, runs: list[FoundryRun]) -> dict[str, str | None]:
    """For each run, the work type the ticket was classified as.

    ``FoundryRun`` carries no ``work_type`` column (it is stored only on the
    denormalized ``FoundryRunOutcome``, which a still-active or never-finished run
    may not have), so this derives it the same way
    :func:`foundry.memory.outcomes.record_outcome` does when it stamps
    ``FoundryRunOutcome.work_type``: from the ``work_type`` field of the run's
    latest ``TICKET_ANALYSIS`` artifact. A run with no analysis artifact (or one
    whose analysis never carried a work type) maps to ``None`` - correctly counted
    as unclassified by the caller, mirroring :func:`_run_repo_map`'s unrouted
    ``None``.
    """
    if not runs:
        return {}
    rows = (
        session.query(FoundryArtifact)
        .filter(
            FoundryArtifact.run_id.in_([r.id for r in runs]),
            FoundryArtifact.artifact_type == ArtifactType.TICKET_ANALYSIS,
        )
        # Ascending so the last row per run wins - the latest analysis, exactly the
        # ordering record_outcome's _latest_artifact_contents uses.
        .order_by(FoundryArtifact.version, FoundryArtifact.created_at)
        .all()
    )
    latest: dict[str, str | None] = {}
    for row in rows:
        try:
            content = json.loads(row.content_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(content, dict):
            latest[row.run_id] = content.get("work_type")
    return {run.id: latest.get(run.id) for run in runs}


def failure_queue(
    session, *, since: datetime, now: datetime | None = None
) -> dict:
    """Recently-failed runs needing triage - the incident feed behind the fleet
    strip's blocked/failed counts, the failure-side complement to
    :func:`approval_queue` / :func:`execution_queue` / :func:`review_queue`.

    Every run currently in a terminal-failure state (:data:`FAILURE_STATUSES` -
    ``blocked`` or ``execution_failed``) whose failure happened within the window
    (at or after ``since``), each with how long ago it failed (``failed_seconds``,
    dated from the failure event - see :data:`_FAILURE_EVENTS_BY_STATUS`) and why
    (``reason``, read from that event's audit metadata).

    Ordered **newest first** - the most recent incident on top. This deliberately
    differs from the three waiting queues (which order oldest-first, since a run
    *draining* out of the state makes the longest wait the most urgent): a failed
    run is terminal and never leaves the state, so the queue is a recency-ordered
    incident feed bounded by ``since`` rather than an ever-growing all-time list.

    Read-only - it surfaces what already happened and blocks/merges nothing. A
    blocked run stays blocked (invariant #7); re-triggering the ticket starts a
    fresh run, it does not revive the failed one.
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    since = _as_utc(since)
    runs: list[FoundryRun] = (
        session.query(FoundryRun)
        .filter(FoundryRun.status.in_(FAILURE_STATUSES))
        .all()
    )
    failed_map = _failure_event_map(session, runs)

    entries: list[dict] = []
    for run in runs:
        marked = failed_map.get(run.id)
        # Fall back to the immutable created_at (not the drift-prone updated_at)
        # when a failed run somehow carries no marker event; the reason is unknown.
        if marked is not None:
            failed_at, reason = marked
        else:
            failed_at, reason = run.created_at, None
        failed_at = _as_utc(failed_at)
        if failed_at < since:
            continue  # an older incident, outside the live window
        # Clamp at 0: a future-dated row (clock skew) is not negative elapsed time.
        elapsed = max(0, int((now - failed_at).total_seconds()))
        status = run.status.value if isinstance(run.status, RunStatus) else str(run.status)
        entries.append(
            {
                "run_id": run.id,
                "linear_issue_key": run.linear_issue_key,
                "status": status,
                "current_step": run.current_step,
                "risk_level": run.risk_level.value if run.risk_level is not None else None,
                "reason": reason,
                "failed_since": failed_at.isoformat(),
                "failed_seconds": elapsed,
            }
        )

    entries.sort(key=lambda e: e["failed_seconds"])  # newest first
    return {
        "now": now.isoformat(),
        "since": since.isoformat(),
        "count": len(entries),
        "newest_failure_seconds": entries[0]["failed_seconds"] if entries else None,
        "oldest_failure_seconds": entries[-1]["failed_seconds"] if entries else None,
        "blocked": sum(1 for e in entries if e["status"] == RunStatus.BLOCKED.value),
        "failed": sum(
            1 for e in entries if e["status"] == RunStatus.EXECUTION_FAILED.value
        ),
        "runs": entries,
    }


# The bucket a failure with no parseable reason lands in - an explicit sentinel
# (mirroring ``(unrouted)`` / ``(unclassified)`` in the delivery cuts) so a NULL
# category is a visible row in the breakdown, never silently dropped.
UNKNOWN_FAILURE_CATEGORY = "(unknown)"


def failures_by_category(
    session, *, since: datetime, now: datetime | None = None
) -> dict:
    """Recently-failed runs **rolled up by reason** - the aggregate triage cut that
    complements the per-run :func:`failure_queue` feed.

    Where ``failure_queue`` is a recency-ordered *feed* (every recent incident,
    newest first), this answers the prioritisation question that feed can't: *what
    are the top reasons runs are blocking/failing, and how many of each?* - so a
    spiking systemic blocker (``policy_denied``, ``budget_exceeded``,
    ``forbidden_path``, ...) is visible at a glance instead of buried in a scroll.

    Every run currently in a terminal-failure state (:data:`FAILURE_STATUSES`)
    whose failure happened within the window (at or after ``since``) is grouped by
    its failure ``reason`` (the ``category``/``reason`` from the
    ``RUN_BLOCKED``/``AGENT_FAILED`` audit metadata, via the same
    :func:`_failure_event_map` derivation the feed uses, so the two can't drift).
    Runs with no parseable reason bucket under :data:`UNKNOWN_FAILURE_CATEGORY`.

    Each category carries its ``count`` (with a ``blocked``/``failed`` split), the
    ``newest_failure_seconds`` / ``oldest_failure_seconds`` age span of its
    members, and the ``last_failure`` ISO timestamp. Categories are ordered
    **most-frequent first**, ties broken by most-recent then name - the on-call's
    "fix this first" order.

    Read-only - it surfaces what already happened and blocks/merges nothing. A
    blocked run stays blocked (invariant #7).
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    since = _as_utc(since)
    runs: list[FoundryRun] = (
        session.query(FoundryRun)
        .filter(FoundryRun.status.in_(FAILURE_STATUSES))
        .all()
    )
    failed_map = _failure_event_map(session, runs)

    buckets: dict[str, dict] = {}
    total = blocked_total = failed_total = 0
    for run in runs:
        marked = failed_map.get(run.id)
        # Fall back to the immutable created_at (not the drift-prone updated_at)
        # when a failed run carries no marker event; the reason is unknown.
        if marked is not None:
            failed_at, reason = marked
        else:
            failed_at, reason = run.created_at, None
        failed_at = _as_utc(failed_at)
        if failed_at < since:
            continue  # an older incident, outside the window
        elapsed = max(0, int((now - failed_at).total_seconds()))
        is_blocked = run.status == RunStatus.BLOCKED
        category = reason or UNKNOWN_FAILURE_CATEGORY

        bucket = buckets.get(category)
        if bucket is None:
            bucket = buckets[category] = {
                "category": category,
                "count": 0,
                "blocked": 0,
                "failed": 0,
                "newest_failure_seconds": elapsed,
                "oldest_failure_seconds": elapsed,
                "last_failure": failed_at,
            }
        bucket["count"] += 1
        if is_blocked:
            bucket["blocked"] += 1
            blocked_total += 1
        else:
            bucket["failed"] += 1
            failed_total += 1
        # Smallest elapsed = most recent; largest = oldest.
        bucket["newest_failure_seconds"] = min(bucket["newest_failure_seconds"], elapsed)
        bucket["oldest_failure_seconds"] = max(bucket["oldest_failure_seconds"], elapsed)
        if failed_at > bucket["last_failure"]:
            bucket["last_failure"] = failed_at
        total += 1

    categories = list(buckets.values())
    # Most-frequent first; ties to the most-recently-seen (smallest newest age),
    # then category name for a stable, deterministic order.
    categories.sort(
        key=lambda c: (-c["count"], c["newest_failure_seconds"], c["category"])
    )
    for c in categories:
        c["last_failure"] = c["last_failure"].isoformat()

    return {
        "now": now.isoformat(),
        "since": since.isoformat(),
        "count": total,
        "blocked": blocked_total,
        "failed": failed_total,
        "distinct_categories": len(categories),
        "categories": categories,
    }


def failures_by_repo(
    session, *, since: datetime, now: datetime | None = None
) -> dict:
    """Recently-failed runs **rolled up by routed repo** - the repo-axis triage cut
    that complements :func:`failures_by_category`.

    Where :func:`failures_by_category` answers *what reason* runs are failing for
    (``policy_denied``, ``budget_exceeded``, ...), this answers *which repo* the
    recent failures land in - the on-call's "is one repo the systemic blocker?"
    question, the failure-side mirror of :func:`delivery_by_repo`. The delivery
    surface already has both a repo and a work-type cut; the failure surface had a
    by-category roll-up and a trend but no repo grouping until this.

    Every run currently in a terminal-failure state (:data:`FAILURE_STATUSES`)
    whose failure happened within the window (at or after ``since``) is grouped by
    its routed repo - the latest agent job's repo, via :func:`_run_repo_map` (the
    same "where the work landed" derivation ``record_outcome`` stamps onto
    ``FoundryRunOutcome.repo``, since ``FoundryRun`` itself carries no repo
    column). A run that never dispatched an agent (parked / blocked at the gate
    before routing) buckets under :data:`UNROUTED_REPO_LABEL`, exactly as an
    unrouted outcome does in :func:`delivery_by_repo`. Failure time and the
    blocked/failed split come from the **same** :func:`_failure_event_map` /
    :data:`_FAILURE_EVENTS_BY_STATUS` derivation the feed, the by-category roll-up
    and the trend use, so the totals here can never drift from theirs.

    Each repo carries its ``count`` (with a ``blocked``/``failed`` split), the
    ``newest_failure_seconds`` / ``oldest_failure_seconds`` age span of its
    members, and the ``last_failure`` ISO timestamp. Repos are ordered
    **most-frequent first**, ties broken by most-recent then name - the same
    "fix this first" order as the by-category cut.

    Read-only - it surfaces what already happened and blocks/merges nothing. A
    blocked run stays blocked (invariant #7).
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    since = _as_utc(since)
    runs: list[FoundryRun] = (
        session.query(FoundryRun)
        .filter(FoundryRun.status.in_(FAILURE_STATUSES))
        .all()
    )
    failed_map = _failure_event_map(session, runs)
    repo_map = _run_repo_map(session, runs)

    buckets: dict[str, dict] = {}
    total = blocked_total = failed_total = 0
    for run in runs:
        marked = failed_map.get(run.id)
        # Fall back to the immutable created_at (not the drift-prone updated_at)
        # when a failed run carries no marker event - same rule as the feed.
        failed_at = _as_utc(marked[0] if marked is not None else run.created_at)
        if failed_at < since:
            continue  # an older incident, outside the window
        elapsed = max(0, int((now - failed_at).total_seconds()))
        is_blocked = run.status == RunStatus.BLOCKED
        repo = repo_map.get(run.id) or UNROUTED_REPO_LABEL

        bucket = buckets.get(repo)
        if bucket is None:
            bucket = buckets[repo] = {
                "repo": repo,
                "count": 0,
                "blocked": 0,
                "failed": 0,
                "newest_failure_seconds": elapsed,
                "oldest_failure_seconds": elapsed,
                "last_failure": failed_at,
            }
        bucket["count"] += 1
        if is_blocked:
            bucket["blocked"] += 1
            blocked_total += 1
        else:
            bucket["failed"] += 1
            failed_total += 1
        # Smallest elapsed = most recent; largest = oldest.
        bucket["newest_failure_seconds"] = min(bucket["newest_failure_seconds"], elapsed)
        bucket["oldest_failure_seconds"] = max(bucket["oldest_failure_seconds"], elapsed)
        if failed_at > bucket["last_failure"]:
            bucket["last_failure"] = failed_at
        total += 1

    repos = list(buckets.values())
    # Most-frequent first; ties to the most-recently-seen (smallest newest age),
    # then repo name for a stable, deterministic order - same as by-category.
    repos.sort(key=lambda r: (-r["count"], r["newest_failure_seconds"], r["repo"]))
    for r in repos:
        r["last_failure"] = r["last_failure"].isoformat()

    return {
        "now": now.isoformat(),
        "since": since.isoformat(),
        "count": total,
        "blocked": blocked_total,
        "failed": failed_total,
        "distinct_repos": len(repos),
        "repos": repos,
    }


def failures_by_work_type(
    session, *, since: datetime, now: datetime | None = None
) -> dict:
    """Recently-failed runs **rolled up by work type** - the work-type-axis triage
    cut that complements :func:`failures_by_category` and :func:`failures_by_repo`.

    Where :func:`failures_by_category` answers *what reason* runs are failing for
    and :func:`failures_by_repo` answers *which repo* the failures land in, this
    answers *which kind of work* is failing - the on-call's "do bugs fail while
    features ship?" question, the failure-side mirror of
    :func:`delivery_by_work_type`. The delivery surface already has both a repo and
    a work-type cut; the failure surface had a by-category roll-up, a by-repo
    roll-up and a trend but no work-type grouping until this.

    Every run currently in a terminal-failure state (:data:`FAILURE_STATUSES`)
    whose failure happened within the window (at or after ``since``) is grouped by
    its work type - the ``work_type`` of its latest ``TICKET_ANALYSIS`` artifact,
    via :func:`_run_work_type_map` (the same field ``record_outcome`` stamps onto
    ``FoundryRunOutcome.work_type``, since ``FoundryRun`` itself carries no
    work-type column). A run that was never classified buckets under
    :data:`UNCLASSIFIED_WORK_TYPE_LABEL`, exactly as an unclassified outcome does
    in :func:`delivery_by_work_type`. Failure time and the blocked/failed split
    come from the **same** :func:`_failure_event_map` /
    :data:`_FAILURE_EVENTS_BY_STATUS` derivation the feed, the by-category roll-up,
    the by-repo roll-up and the trend use, so the totals here can never drift from
    theirs.

    Each work type carries its ``count`` (with a ``blocked``/``failed`` split), the
    ``newest_failure_seconds`` / ``oldest_failure_seconds`` age span of its
    members, and the ``last_failure`` ISO timestamp. Work types are ordered
    **most-frequent first**, ties broken by most-recent then name - the same
    "fix this first" order as the by-category and by-repo cuts.

    Read-only - it surfaces what already happened and blocks/merges nothing. A
    blocked run stays blocked (invariant #7).
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    since = _as_utc(since)
    runs: list[FoundryRun] = (
        session.query(FoundryRun)
        .filter(FoundryRun.status.in_(FAILURE_STATUSES))
        .all()
    )
    failed_map = _failure_event_map(session, runs)
    work_type_map = _run_work_type_map(session, runs)

    buckets: dict[str, dict] = {}
    total = blocked_total = failed_total = 0
    for run in runs:
        marked = failed_map.get(run.id)
        # Fall back to the immutable created_at (not the drift-prone updated_at)
        # when a failed run carries no marker event - same rule as the feed.
        failed_at = _as_utc(marked[0] if marked is not None else run.created_at)
        if failed_at < since:
            continue  # an older incident, outside the window
        elapsed = max(0, int((now - failed_at).total_seconds()))
        is_blocked = run.status == RunStatus.BLOCKED
        work_type = work_type_map.get(run.id) or UNCLASSIFIED_WORK_TYPE_LABEL

        bucket = buckets.get(work_type)
        if bucket is None:
            bucket = buckets[work_type] = {
                "work_type": work_type,
                "count": 0,
                "blocked": 0,
                "failed": 0,
                "newest_failure_seconds": elapsed,
                "oldest_failure_seconds": elapsed,
                "last_failure": failed_at,
            }
        bucket["count"] += 1
        if is_blocked:
            bucket["blocked"] += 1
            blocked_total += 1
        else:
            bucket["failed"] += 1
            failed_total += 1
        # Smallest elapsed = most recent; largest = oldest.
        bucket["newest_failure_seconds"] = min(bucket["newest_failure_seconds"], elapsed)
        bucket["oldest_failure_seconds"] = max(bucket["oldest_failure_seconds"], elapsed)
        if failed_at > bucket["last_failure"]:
            bucket["last_failure"] = failed_at
        total += 1

    work_types = list(buckets.values())
    # Most-frequent first; ties to the most-recently-seen (smallest newest age),
    # then work-type name for a stable, deterministic order - same as by-category
    # and by-repo.
    work_types.sort(
        key=lambda w: (-w["count"], w["newest_failure_seconds"], w["work_type"])
    )
    for w in work_types:
        w["last_failure"] = w["last_failure"].isoformat()

    return {
        "now": now.isoformat(),
        "since": since.isoformat(),
        "count": total,
        "blocked": blocked_total,
        "failed": failed_total,
        "distinct_work_types": len(work_types),
        "work_types": work_types,
    }


def _empty_failure_period() -> dict:
    """A fresh per-period accumulator for :func:`failure_trends` - the failure
    count with its blocked/execution-failed split."""
    return {"count": 0, "blocked": 0, "failed": 0}


def _render_failure_period(period_start: datetime, agg: dict | None) -> dict:
    """Render a failure-trend period to its public shape (``None`` agg = an
    empty, zero-filled bucket so a sparkline reads as a continuous series)."""
    agg = agg or _empty_failure_period()
    return {
        "period_start": period_start.isoformat(),
        "count": agg["count"],
        "blocked": agg["blocked"],
        "failed": agg["failed"],
    }


def failure_trends(
    session, *, since: datetime, bucket: str = "day", now: datetime | None = None
) -> dict:
    """Recently-failed runs **bucketed over time** - the direction-of-travel cut
    that complements the per-run :func:`failure_queue` feed and the point-in-time
    :func:`failures_by_category` roll-up.

    Those two answer "what is failing *right now*" (the recent incidents, and the
    top reasons). This answers the question neither can - *"are we failing more
    than usual; is something spiking?"* - by bucketing the same recently-failed
    runs by **when they failed** onto one zero-filled time axis, so a rising (or
    falling) failure rate is visible at a glance. It is to the failure surface
    what :func:`delivery_trends` is to the delivery surface.

    Every run currently in a terminal-failure state (:data:`FAILURE_STATUSES`)
    whose failure happened within the window (at or after ``since``) is bucketed
    by ``bucket_start`` of its failure time - dated from the same
    :func:`_failure_event_map` derivation (the ``RUN_BLOCKED``/``AGENT_FAILED``
    marker) the feed and the by-category roll-up use, so the totals here can never
    drift from theirs. Each period carries its ``count`` with a
    ``blocked``/``failed`` split; empty periods inside the span are zero-filled so
    the series reads as continuous rather than sparse (the same shape and shared
    :func:`_delivery_axis` the delivery trends use - the axis spans the first to
    the last *populated* period, so the series is a pure function of the rows).

    Read-only - it surfaces what already happened and blocks/merges nothing. A
    blocked run stays blocked (invariant #7).
    """
    if bucket not in TREND_BUCKETS:
        raise ValueError(f"bucket must be one of {TREND_BUCKETS}, got {bucket!r}")

    now = _as_utc(now or datetime.now(timezone.utc))
    since = _as_utc(since)
    runs: list[FoundryRun] = (
        session.query(FoundryRun)
        .filter(FoundryRun.status.in_(FAILURE_STATUSES))
        .all()
    )
    failed_map = _failure_event_map(session, runs)

    periods: dict[datetime, dict] = {}
    total = blocked_total = failed_total = 0
    for run in runs:
        marked = failed_map.get(run.id)
        # Fall back to the immutable created_at (not the drift-prone updated_at)
        # when a failed run carries no marker event - same rule as the feed.
        failed_at = _as_utc(marked[0] if marked is not None else run.created_at)
        if failed_at < since:
            continue  # an older incident, outside the window
        agg = periods.setdefault(
            bucket_start(failed_at, bucket), _empty_failure_period()
        )
        agg["count"] += 1
        if run.status == RunStatus.BLOCKED:
            agg["blocked"] += 1
            blocked_total += 1
        else:
            agg["failed"] += 1
            failed_total += 1
        total += 1

    axis = _delivery_axis(list(periods), bucket)
    return {
        "now": now.isoformat(),
        "since": since.isoformat(),
        "bucket": bucket,
        "count": total,
        "blocked": blocked_total,
        "failed": failed_total,
        "periods": [_render_failure_period(start, periods.get(start)) for start in axis],
    }


def failures_by_category_trends(
    session, *, since: datetime, bucket: str = "day", now: datetime | None = None
) -> dict:
    """Recently-failed runs **grouped by reason and bucketed over time** - the
    by-category dimension of :func:`failure_trends`, the way
    :func:`delivery_by_work_type_trends` is to :func:`delivery_trends`.

    The org-wide :func:`failure_trends` shows whether we are failing *more* than
    usual; the point-in-time :func:`failures_by_category` roll-up shows *what* is
    failing most right now. Neither answers the question this does: *is a
    **specific** systemic blocker (``policy_denied``, ``budget_exceeded``,
    ``forbidden_path``, ...) trending up or fading?* - the direction-of-travel of
    each reason, so an on-call can tell a one-off spike from a worsening
    regression.

    Every run currently in a terminal-failure state (:data:`FAILURE_STATUSES`)
    whose failure happened within the window (at or after ``since``) is grouped by
    its failure ``reason`` (the ``category``/``reason`` from the
    ``RUN_BLOCKED``/``AGENT_FAILED`` audit metadata, via the same
    :func:`_failure_event_map` derivation the feed, the by-category roll-up and
    the org-wide trend use, so the totals here can never drift from theirs) and
    bucketed by ``bucket_start`` of its failure time. Runs with no parseable
    reason bucket under :data:`UNKNOWN_FAILURE_CATEGORY`.

    Every category's ``series`` is aligned to one shared time axis spanning the
    first to the last *populated* period (across all categories), zero-filled so
    the per-category sparklines line up column-for-column - the same shape and
    shared :func:`_delivery_axis` the delivery trends use. Each category also
    carries its window totals (``count`` with a ``blocked``/``failed`` split) so a
    caller can label the trend without a second query. Categories are ordered
    **most-frequent first**, ties broken by most-recent then name - the same order
    as :func:`failures_by_category` so the point-in-time and over-time cuts read
    consistently.

    Read-only - it surfaces what already happened and blocks/merges nothing. A
    blocked run stays blocked (invariant #7).
    """
    if bucket not in TREND_BUCKETS:
        raise ValueError(f"bucket must be one of {TREND_BUCKETS}, got {bucket!r}")

    now = _as_utc(now or datetime.now(timezone.utc))
    since = _as_utc(since)
    runs: list[FoundryRun] = (
        session.query(FoundryRun)
        .filter(FoundryRun.status.in_(FAILURE_STATUSES))
        .all()
    )
    failed_map = _failure_event_map(session, runs)

    # category -> period_start -> accumulator, plus per-category window totals and
    # the smallest elapsed seen (the most-recent failure) for the recency tiebreak.
    per_period: dict[str, dict[datetime, dict]] = {}
    totals: dict[str, dict] = {}
    newest_elapsed: dict[str, int] = {}
    total = blocked_total = failed_total = 0
    for run in runs:
        marked = failed_map.get(run.id)
        # Fall back to the immutable created_at (not the drift-prone updated_at)
        # when a failed run carries no marker event - same rule as the feed.
        if marked is not None:
            failed_at, reason = marked
        else:
            failed_at, reason = run.created_at, None
        failed_at = _as_utc(failed_at)
        if failed_at < since:
            continue  # an older incident, outside the window
        elapsed = max(0, int((now - failed_at).total_seconds()))
        category = reason or UNKNOWN_FAILURE_CATEGORY
        is_blocked = run.status == RunStatus.BLOCKED

        agg = per_period.setdefault(category, {}).setdefault(
            bucket_start(failed_at, bucket), _empty_failure_period()
        )
        cat_total = totals.setdefault(category, _empty_failure_period())
        for target in (agg, cat_total):
            target["count"] += 1
            if is_blocked:
                target["blocked"] += 1
            else:
                target["failed"] += 1
        if is_blocked:
            blocked_total += 1
        else:
            failed_total += 1
        prev = newest_elapsed.get(category)
        newest_elapsed[category] = elapsed if prev is None else min(prev, elapsed)
        total += 1

    # One shared axis across every category so the per-category series align.
    populated = [start for periods in per_period.values() for start in periods]
    axis = _delivery_axis(populated, bucket)

    out_categories = []
    for category, cat_total in totals.items():
        periods = per_period[category]
        out_categories.append(
            {
                "category": category,
                "count": cat_total["count"],
                "blocked": cat_total["blocked"],
                "failed": cat_total["failed"],
                "newest_failure_seconds": newest_elapsed[category],
                "series": [
                    _render_failure_period(start, periods.get(start)) for start in axis
                ],
            }
        )

    # Most-frequent first; ties to the most-recently-seen (smallest newest age),
    # then category name - the same order as failures_by_category.
    out_categories.sort(
        key=lambda c: (-c["count"], c["newest_failure_seconds"], c["category"])
    )

    return {
        "now": now.isoformat(),
        "since": since.isoformat(),
        "bucket": bucket,
        "count": total,
        "blocked": blocked_total,
        "failed": failed_total,
        "distinct_categories": len(out_categories),
        "periods": [start.isoformat() for start in axis],
        "categories": out_categories,
    }


def failures_by_repo_trends(
    session, *, since: datetime, bucket: str = "day", now: datetime | None = None
) -> dict:
    """Recently-failed runs **grouped by routed repo and bucketed over time** - the
    by-repo dimension of :func:`failure_trends`, the way
    :func:`failures_by_category_trends` is to it by *reason* and
    :func:`delivery_by_repo_trends` is to :func:`delivery_trends`.

    The org-wide :func:`failure_trends` shows whether we are failing *more* than
    usual; the point-in-time :func:`failures_by_repo` roll-up shows *which repo* is
    failing most right now. Neither answers the question this does: *is **this
    repo's** failure rate climbing or fading over time?* - the direction-of-travel
    per repo, so an on-call can tell a one-off spike in a repo from a worsening
    regression there.

    Every run currently in a terminal-failure state (:data:`FAILURE_STATUSES`)
    whose failure happened within the window (at or after ``since``) is grouped by
    its routed repo - the latest agent job's repo, via :func:`_run_repo_map` (the
    same "where the work landed" derivation ``record_outcome`` stamps onto
    ``FoundryRunOutcome.repo``, since ``FoundryRun`` itself carries no repo
    column) - and bucketed by ``bucket_start`` of its failure time (dated from the
    same :func:`_failure_event_map` / :data:`_FAILURE_EVENTS_BY_STATUS` derivation
    the feed, the by-repo roll-up and the org-wide trend use, so the totals here
    can never drift from theirs). A run that never dispatched an agent (parked /
    blocked at the gate before routing) buckets under :data:`UNROUTED_REPO_LABEL`,
    exactly as in :func:`failures_by_repo` and :func:`delivery_by_repo`.

    Every repo's ``series`` is aligned to one shared time axis spanning the first
    to the last *populated* period (across all repos), zero-filled so the per-repo
    sparklines line up column-for-column - the same shape and shared
    :func:`_delivery_axis` the delivery trends and the by-category trend use. Each
    repo also carries its window totals (``count`` with a ``blocked``/``failed``
    split) so a caller can label the trend without a second query. Repos are
    ordered **most-frequent first**, ties broken by most-recent then name - the
    same order as :func:`failures_by_repo` so the point-in-time and over-time cuts
    read consistently.

    Read-only - it surfaces what already happened and blocks/merges nothing. A
    blocked run stays blocked (invariant #7).
    """
    if bucket not in TREND_BUCKETS:
        raise ValueError(f"bucket must be one of {TREND_BUCKETS}, got {bucket!r}")

    now = _as_utc(now or datetime.now(timezone.utc))
    since = _as_utc(since)
    runs: list[FoundryRun] = (
        session.query(FoundryRun)
        .filter(FoundryRun.status.in_(FAILURE_STATUSES))
        .all()
    )
    failed_map = _failure_event_map(session, runs)
    repo_map = _run_repo_map(session, runs)

    # repo -> period_start -> accumulator, plus per-repo window totals and the
    # smallest elapsed seen (the most-recent failure) for the recency tiebreak.
    per_period: dict[str, dict[datetime, dict]] = {}
    totals: dict[str, dict] = {}
    newest_elapsed: dict[str, int] = {}
    total = blocked_total = failed_total = 0
    for run in runs:
        marked = failed_map.get(run.id)
        # Fall back to the immutable created_at (not the drift-prone updated_at)
        # when a failed run carries no marker event - same rule as the feed.
        failed_at = _as_utc(marked[0] if marked is not None else run.created_at)
        if failed_at < since:
            continue  # an older incident, outside the window
        elapsed = max(0, int((now - failed_at).total_seconds()))
        repo = repo_map.get(run.id) or UNROUTED_REPO_LABEL
        is_blocked = run.status == RunStatus.BLOCKED

        agg = per_period.setdefault(repo, {}).setdefault(
            bucket_start(failed_at, bucket), _empty_failure_period()
        )
        repo_total = totals.setdefault(repo, _empty_failure_period())
        for target in (agg, repo_total):
            target["count"] += 1
            if is_blocked:
                target["blocked"] += 1
            else:
                target["failed"] += 1
        if is_blocked:
            blocked_total += 1
        else:
            failed_total += 1
        prev = newest_elapsed.get(repo)
        newest_elapsed[repo] = elapsed if prev is None else min(prev, elapsed)
        total += 1

    # One shared axis across every repo so the per-repo series align.
    populated = [start for periods in per_period.values() for start in periods]
    axis = _delivery_axis(populated, bucket)

    out_repos = []
    for repo, repo_total in totals.items():
        periods = per_period[repo]
        out_repos.append(
            {
                "repo": repo,
                "count": repo_total["count"],
                "blocked": repo_total["blocked"],
                "failed": repo_total["failed"],
                "newest_failure_seconds": newest_elapsed[repo],
                "series": [
                    _render_failure_period(start, periods.get(start)) for start in axis
                ],
            }
        )

    # Most-frequent first; ties to the most-recently-seen (smallest newest age),
    # then repo name - the same order as failures_by_repo.
    out_repos.sort(
        key=lambda r: (-r["count"], r["newest_failure_seconds"], r["repo"])
    )

    return {
        "now": now.isoformat(),
        "since": since.isoformat(),
        "bucket": bucket,
        "count": total,
        "blocked": blocked_total,
        "failed": failed_total,
        "distinct_repos": len(out_repos),
        "periods": [start.isoformat() for start in axis],
        "repos": out_repos,
    }


def fleet_status(
    session,
    *,
    sla_seconds: int | None = None,
    execution_sla_seconds: int | None = None,
    review_sla_seconds: int | None = None,
    review_stale_sla_seconds: int | None = None,
    now: datetime | None = None,
) -> dict:
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

    # Turn the bare ``awaiting_human`` count into an actionable signal: how long
    # has the oldest parked run been waiting, and how many have breached the SLA.
    # Reuses the same derivation the GET /metrics/approvals drill-down serves, so
    # the strip summary and the queue list can never disagree.
    queue = approval_queue(session, now=now, sla_seconds=sla_seconds)
    # The machine-side equivalent for ``agents_running``: the oldest in-flight
    # agent run and how many have breached the execution SLA. Same derivation the
    # GET /metrics/executions drill-down serves, so they can never disagree.
    execution = execution_queue(session, now=now, sla_seconds=execution_sla_seconds)
    # The review-side equivalent for ``prs_open``: the oldest open PR awaiting
    # review and how many have breached the review SLA, plus the most-stale PR (no
    # push for the longest) and how many have breached the *staleness* SLA. Same
    # derivation the GET /metrics/reviews drill-down serves, so they can never
    # disagree. The two review SLAs are independent knobs.
    review = review_queue(
        session,
        now=now,
        sla_seconds=review_sla_seconds,
        stale_sla_seconds=review_stale_sla_seconds,
    )

    return {
        "total_runs": sum(by_status.values()),
        "runs_active": _count(*ACTIVE_RUN_STATUSES),
        "runs_terminal": _count(*TERMINAL_RUN_STATUSES),
        "awaiting_human": awaiting_human,
        "oldest_wait_seconds": queue["oldest_wait_seconds"],
        "approval_sla_seconds": sla_seconds,
        "approvals_breaching_sla": queue["sla_breaches"],
        "agents_running": _count(RunStatus.AGENT_RUNNING),
        "oldest_execution_seconds": execution["oldest_running_seconds"],
        "execution_sla_seconds": execution_sla_seconds,
        "executions_breaching_sla": execution["sla_breaches"],
        "prs_open": _count(RunStatus.PR_OPEN),
        "oldest_review_seconds": review["oldest_unreviewed_seconds"],
        "review_sla_seconds": review_sla_seconds,
        "reviews_breaching_sla": review["sla_breaches"],
        "oldest_inactive_seconds": review["oldest_inactive_seconds"],
        "review_stale_sla_seconds": review_stale_sla_seconds,
        "reviews_stale": review["stale_breaches"],
        "active_cost_usd": round(active_cost, 2) if active_cost is not None else None,
        "by_status": by_status,
    }
