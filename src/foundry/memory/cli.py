"""Console entry point for delivery memory.

Usage::

    foundry-memory backfill [--recompute]
    foundry-memory show-priors
    foundry-memory show-scorecards
    foundry-memory show-scorecard-trends [--bucket week|day] [--days D]
    foundry-memory recommend-agent [--work-type T] [--repo R] [--candidates a,b]
                                   [--min-samples N] [--days D]
    foundry-memory fleet
    foundry-memory failures [--days D]

``backfill`` derives outcome rows for terminal runs that finished before the
``foundry_run_outcomes`` table existed (or that a fail-soft hook missed);
``--recompute`` re-derives every terminal run from the audit trail.
``show-priors`` prints the mined routing priors; ``show-scorecards`` prints
per-provider agent performance; ``show-scorecard-trends`` prints that same
per-provider merge rate bucketed over time (is an agent improving or sliding?);
``recommend-agent`` turns those scorecards into
a single, explainable provider recommendation for a piece of work (the
decision-support read behind the future ``agent.provider: auto`` - reporting
only).

``fleet`` and ``failures`` are the **offline twins** of the operational fleet
metrics endpoints (``GET /metrics/fleet`` / ``GET /metrics/failures``): they read
the DB directly and call the same ``memory/metrics.py`` derivations the API
serves, so an on-call engineer or auditor with DB access but no running API /
bearer token can still answer "is everything healthy right now?" (``fleet`` - the
live snapshot, honouring the same ``dashboard.*_sla_seconds`` knobs) and "what
just broke and needs a human?" (``failures`` - recently blocked/execution-failed
runs, newest first, bounded to a recent window). Mirrors how ``foundry-evidence``
is the offline twin of the evidence endpoints. Both are read-only and block
nothing.

Settings come from ``FOUNDRY_CONFIG`` and the usual environment variable
overrides.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="foundry-memory",
        description="Manage Foundry's delivery memory (per-run outcomes and priors).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    backfill_p = sub.add_parser(
        "backfill", help="Derive outcome rows for terminal runs that lack one."
    )
    backfill_p.add_argument(
        "--recompute",
        action="store_true",
        default=False,
        help="Re-derive outcomes for ALL terminal runs, not just missing ones.",
    )

    sub.add_parser("show-priors", help="Print the mined routing priors.")
    sub.add_parser(
        "show-scorecards", help="Print per-provider agent scorecards."
    )

    trends_p = sub.add_parser(
        "show-scorecard-trends",
        help="Print per-provider merge rate bucketed over time.",
    )
    trends_p.add_argument(
        "--bucket",
        default="week",
        choices=("day", "week"),
        help="Time bucket for the trend (default: week).",
    )
    trends_p.add_argument(
        "--days",
        type=int,
        default=90,
        help="Only consider outcomes from the last N days (default: 90).",
    )

    rec_p = sub.add_parser(
        "recommend-agent",
        help="Recommend the agent provider with the best track record for given work.",
    )
    rec_p.add_argument(
        "--work-type", default=None, help="Work type to score for (e.g. feature, bug)."
    )
    rec_p.add_argument("--repo", default=None, help="Narrow the recommendation to a repo.")
    rec_p.add_argument(
        "--candidates",
        default=None,
        help="Comma-separated provider allow-list (only recommend these).",
    )
    rec_p.add_argument(
        "--min-samples",
        type=int,
        default=None,
        help="Runs a provider needs before it is eligible (default 3).",
    )
    rec_p.add_argument(
        "--days",
        type=int,
        default=None,
        help="Only consider outcomes from the last N days (default: all history).",
    )

    sub.add_parser(
        "fleet",
        help="Print the live fleet snapshot (offline twin of GET /metrics/fleet).",
    )

    fail_p = sub.add_parser(
        "failures",
        help="Print recently-failed runs needing triage (offline twin of "
        "GET /metrics/failures).",
    )
    fail_p.add_argument(
        "--days",
        type=int,
        default=7,
        help="Only show runs that failed in the last N days (default: 7).",
    )

    args = parser.parse_args()
    if args.command == "backfill":
        _run_backfill(args)
    elif args.command == "show-priors":
        _run_show_priors()
    elif args.command == "show-scorecards":
        _run_show_scorecards()
    elif args.command == "show-scorecard-trends":
        _run_show_scorecard_trends(args)
    elif args.command == "recommend-agent":
        _run_recommend(args)
    elif args.command == "fleet":
        _run_fleet()
    elif args.command == "failures":
        _run_failures(args)


def _session_factory():
    from foundry.config import Settings
    from foundry.db.base import init_schema, make_engine, make_session_factory

    settings = Settings.load(os.environ.get("FOUNDRY_CONFIG"), env=os.environ)
    engine = make_engine(settings.database_url)
    init_schema(engine)
    return settings, make_session_factory(engine)


def _run_backfill(args: argparse.Namespace) -> None:
    from foundry.db.models import FoundryRun, FoundryRunOutcome
    from foundry.memory.outcomes import record_outcome
    from foundry.schemas.common import TERMINAL_RUN_STATUSES

    _settings, session_factory = _session_factory()

    written = failed = 0
    with session_factory() as session:
        query = session.query(FoundryRun).filter(
            FoundryRun.status.in_(TERMINAL_RUN_STATUSES)
        )
        if not args.recompute:
            existing = session.query(FoundryRunOutcome.run_id)
            query = query.filter(FoundryRun.id.notin_(existing))
        runs = query.order_by(FoundryRun.created_at).all()
        for run in runs:
            try:
                record_outcome(session, run)
                written += 1
            except Exception as exc:
                failed += 1
                print(f"warning: run {run.id}: {exc}", file=sys.stderr)
        session.commit()

    verb = "recomputed" if args.recompute else "backfilled"
    print(f"Backfill complete: {written} outcomes {verb}, {failed} failed.")
    sys.exit(1 if failed and not written else 0)


def _run_show_priors() -> None:
    from foundry.memory.priors import routing_prior_rows, smoothed_confidence

    _settings, session_factory = _session_factory()
    with session_factory() as session:
        rows = routing_prior_rows(session)
    if not rows:
        print("No routed outcomes recorded yet - run 'foundry-memory backfill' first.")
        return
    print(f"{'team':<8} {'work type':<14} {'repository':<40} {'merged/routed':<14} conf")
    for prefix, work_type, repo, routed, merged in rows:
        conf = smoothed_confidence(merged, routed, cap=100)
        print(
            f"{prefix:<8} {(work_type or '-'):<14} {repo:<40} "
            f"{f'{merged}/{routed}':<14} {conf}"
        )


def _run_show_scorecards() -> None:
    from foundry.memory.scorecards import agent_scorecards

    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = agent_scorecards(session)
    providers = report["providers"]
    if not providers:
        print(
            "No dispatched outcomes recorded yet - "
            "run 'foundry-memory backfill' first."
        )
        return

    def _cost(stat: dict) -> str:
        return "-" if stat["total_cost_usd"] is None else f"${stat['total_cost_usd']}"

    for card in providers:
        flag = "" if card["meets_min_samples"] else "  (below min samples)"
        print(
            f"\n{card['provider']}: {card['merged']}/{card['runs']} merged "
            f"(conf {card['smoothed_success']}), {card['retries_consumed']} retries, "
            f"{_cost(card)} spend{flag}"
        )
        for wt in card["by_work_type"]:
            print(
                f"    {(wt['work_type'] or '-'):<16} "
                f"{wt['merged']}/{wt['runs']} merged  conf {wt['smoothed_success']}"
            )
        for repo in card["by_repo"]:
            print(
                f"    @ {(repo['repo'] or '-'):<38} "
                f"{repo['merged']}/{repo['runs']} merged  conf {repo['smoothed_success']}"
            )


def _run_show_scorecard_trends(args: argparse.Namespace) -> None:
    from datetime import datetime, timedelta, timezone

    from foundry.memory.scorecards import agent_scorecard_trends

    if args.days < 1:
        print("error: --days must be >= 1", file=sys.stderr)
        sys.exit(2)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = agent_scorecard_trends(session, since=since, bucket=args.bucket)

    providers = report["providers"]
    if not providers:
        print(
            "No dispatched outcomes recorded yet - "
            "run 'foundry-memory backfill' first."
        )
        return

    # Compact per-period view: the smoothed merge rate for each period, so the
    # direction of travel is readable in the terminal ('-' = no run that period).
    periods = report["periods"]
    label = "week of" if args.bucket == "week" else "day"
    print(f"Per-provider merge confidence by {args.bucket} (last {args.days}d):\n")
    for card in providers:
        flag = "" if card["meets_min_samples"] else "  (below min samples)"
        print(
            f"{card['provider']}: {card['merged']}/{card['runs']} merged overall "
            f"(conf {card['smoothed_success']}){flag}"
        )
        for period_iso, cell in zip(periods, card["series"]):
            day = period_iso[:10]
            if cell["runs"]:
                print(
                    f"    {label} {day}  conf {cell['smoothed_success']:>3}  "
                    f"({cell['merged']}/{cell['runs']} merged)"
                )
            else:
                print(f"    {label} {day}  conf   -  (no runs)")
        print()


def _run_recommend(args: argparse.Namespace) -> None:
    from datetime import datetime, timedelta, timezone

    from foundry.memory.scorecards import DEFAULT_MIN_SAMPLES, recommend_provider

    candidates = (
        [c.strip() for c in args.candidates.split(",") if c.strip()]
        if args.candidates
        else None
    )
    since = None
    if args.days is not None:
        if args.days < 1:
            print("error: --days must be >= 1", file=sys.stderr)
            sys.exit(2)
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
    min_samples = (
        args.min_samples if args.min_samples is not None else DEFAULT_MIN_SAMPLES
    )

    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = recommend_provider(
            session,
            work_type=args.work_type,
            repo=args.repo,
            candidates=candidates,
            since=since,
            min_samples=min_samples,
        )

    print(f"Scope: {report['scope']}  (min samples {report['min_samples']})")
    if report["recommended"]:
        print(f"Recommended: {report['reason']}")
    else:
        print(f"Recommended: none - {report['reason']}")

    if report["ranked"]:
        print(f"\n{'provider':<20} {'merged/runs':<12} {'conf':<5} {'avg $':<8} eligible")
        for card in report["ranked"]:
            cost = "-" if card["avg_cost_usd"] is None else f"${card['avg_cost_usd']}"
            tally = f"{card['merged']}/{card['runs']}"
            conf = str(card["smoothed_success"] if card["smoothed_success"] is not None else "-")
            print(
                f"{card['provider']:<20} {tally:<12} {conf:<5} {cost:<8} "
                f"{'yes' if card['eligible'] else 'no'}"
            )


def _fmt_age(seconds: int | None) -> str:
    """Human-readable elapsed time, '-' when no value (no run in that state)."""
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def _sla_note(breaches: int, sla_seconds: int | None) -> str:
    """' (N breaching SLA)' / ' (SLA Ns)' / '' — match the dashboard strip's signal."""
    if sla_seconds is None:
        return ""
    if breaches:
        return f"  ({breaches} breaching SLA {sla_seconds}s)"
    return f"  (SLA {sla_seconds}s, none breaching)"


def _run_fleet() -> None:
    from foundry.memory.metrics import fleet_status

    settings, session_factory = _session_factory()
    with session_factory() as session:
        snap = fleet_status(
            session,
            sla_seconds=settings.approval_sla_seconds,
            execution_sla_seconds=settings.execution_sla_seconds,
            review_sla_seconds=settings.review_sla_seconds,
            review_stale_sla_seconds=settings.review_stale_sla_seconds,
        )

    cost = "-" if snap["active_cost_usd"] is None else f"${snap['active_cost_usd']}"
    print("Fleet snapshot (live):\n")
    print(f"  runs total      {snap['total_runs']}")
    print(f"  runs active     {snap['runs_active']}")
    print(f"  runs terminal   {snap['runs_terminal']}")
    print(
        f"  awaiting human  {snap['awaiting_human']}  "
        f"(oldest wait {_fmt_age(snap['oldest_wait_seconds'])})"
        f"{_sla_note(snap['approvals_breaching_sla'], snap['approval_sla_seconds'])}"
    )
    print(
        f"  agents running  {snap['agents_running']}  "
        f"(oldest run {_fmt_age(snap['oldest_execution_seconds'])})"
        f"{_sla_note(snap['executions_breaching_sla'], snap['execution_sla_seconds'])}"
    )
    print(
        f"  PRs open        {snap['prs_open']}  "
        f"(oldest review {_fmt_age(snap['oldest_review_seconds'])})"
        f"{_sla_note(snap['reviews_breaching_sla'], snap['review_sla_seconds'])}"
    )
    print(
        f"  PRs stale       oldest inactive {_fmt_age(snap['oldest_inactive_seconds'])}"
        f"{_sla_note(snap['reviews_stale'], snap['review_stale_sla_seconds'])}"
    )
    print(f"  spend committed {cost}")

    if snap["by_status"]:
        print("\n  by status:")
        for status, count in sorted(snap["by_status"].items()):
            print(f"    {status:<22} {count}")


def _run_failures(args: argparse.Namespace) -> None:
    from datetime import datetime, timedelta, timezone

    from foundry.memory.metrics import failure_queue

    if args.days < 1:
        print("error: --days must be >= 1", file=sys.stderr)
        sys.exit(2)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = failure_queue(session, since=since)

    runs = report["runs"]
    if not runs:
        print(f"No runs failed in the last {args.days}d - nothing to triage.")
        return

    print(
        f"Failed runs needing triage (last {args.days}d): "
        f"{report['count']} total, {report['blocked']} blocked, "
        f"{report['failed']} execution-failed (newest first):\n"
    )
    print(f"{'failed':<9} {'status':<18} {'issue':<14} {'run':<14} reason")
    for run in runs:
        issue = run["linear_issue_key"] or "-"
        reason = run["reason"] or "(unknown)"
        print(
            f"{_fmt_age(run['failed_seconds']):<9} {run['status']:<18} "
            f"{issue:<14} {run['run_id'][:12]:<14} {reason}"
        )
