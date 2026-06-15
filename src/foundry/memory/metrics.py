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

from foundry.db.models import (
    AuditEventType,
    FoundryAgentJob,
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
