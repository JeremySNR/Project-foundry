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
    foundry-memory failures-by-category [--days D]
    foundry-memory failures-by-repo [--days D]
    foundry-memory failures-by-work-type [--days D]
    foundry-memory failures-trends [--bucket day|week] [--days D]
    foundry-memory failures-by-category-trends [--bucket day|week] [--days D]
    foundry-memory approvals
    foundry-memory executions
    foundry-memory reviews
    foundry-memory delivery [--days D]
    foundry-memory delivery-trends [--bucket week|day] [--days D]
    foundry-memory delivery-by-repo [--days D]
    foundry-memory delivery-by-work-type [--days D]
    foundry-memory delivery-by-repo-trends [--bucket week|day] [--days D]
    foundry-memory delivery-by-work-type-trends [--bucket week|day] [--days D]

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

``approvals``, ``executions`` and ``reviews`` are the offline twins of the three
*in-flight* queue drill-downs (``GET /metrics/approvals`` / ``/metrics/executions``
/ ``/metrics/reviews``) - the per-run cuts behind the ``fleet`` snapshot's
``awaiting_human`` / ``agents_running`` / ``prs_open`` counts. Each lists the runs
currently parked in that state, oldest first, with its age and (when the matching
``dashboard.*_sla_seconds`` knob is set) whether it has breached - so an on-call
engineer can answer "what is the oldest thing waiting, and is it overdue?" from the
command line. Like ``fleet``/``failures`` they call the same ``memory/metrics.py``
derivations the endpoints serve, so the CLI and API verdicts can't drift.

``delivery``, ``delivery-trends``, ``delivery-by-repo``,
``delivery-by-work-type``, ``delivery-by-repo-trends`` and
``delivery-by-work-type-trends`` are the offline twins
of the **delivery** metrics endpoints (``GET /metrics/delivery`` /
``/metrics/delivery/trends`` / ``/metrics/delivery/by-repo`` /
``/metrics/delivery/by-work-type`` / ``/metrics/delivery/by-repo/trends`` /
``/metrics/delivery/by-work-type/trends``) - the
org-wide "where work ships, stalls and spends" cut and its trend / per-repo /
per-work-type dimensions. They answer "what did we ship in the window, where, by
what kind of work, and at what cost?" and "is throughput trending up or down?"
offline. Same ``--days`` (default
90) and ``--bucket`` (default ``week``) defaults as the endpoints, calling the
same ``memory/metrics.py`` derivations, so the CLI and API verdicts can't drift.
(``delivery`` omits the ``top_priors`` block the endpoint carries - that is what
``show-priors`` already prints offline.)

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

    fail_cat_p = sub.add_parser(
        "failures-by-category",
        help="Print recent failures rolled up by reason, most-frequent first "
        "(offline twin of GET /metrics/failures/by-category).",
    )
    fail_cat_p.add_argument(
        "--days",
        type=int,
        default=7,
        help="Only count runs that failed in the last N days (default: 7).",
    )

    fail_repo_p = sub.add_parser(
        "failures-by-repo",
        help="Print recent failures grouped by routed repo, most-frequent first "
        "(offline twin of GET /metrics/failures/by-repo).",
    )
    fail_repo_p.add_argument(
        "--days",
        type=int,
        default=7,
        help="Only count runs that failed in the last N days (default: 7).",
    )

    fail_wt_p = sub.add_parser(
        "failures-by-work-type",
        help="Print recent failures grouped by work type, most-frequent first "
        "(offline twin of GET /metrics/failures/by-work-type).",
    )
    fail_wt_p.add_argument(
        "--days",
        type=int,
        default=7,
        help="Only count runs that failed in the last N days (default: 7).",
    )

    fail_trends_p = sub.add_parser(
        "failures-trends",
        help="Print recent failures bucketed over time, oldest period first "
        "(offline twin of GET /metrics/failures/trends).",
    )
    fail_trends_p.add_argument(
        "--days",
        type=int,
        default=30,
        help="Only count runs that failed in the last N days (default: 30).",
    )
    fail_trends_p.add_argument(
        "--bucket",
        default="day",
        choices=("day", "week"),
        help="Time bucket for the trend (default: day).",
    )

    fail_cat_trends_p = sub.add_parser(
        "failures-by-category-trends",
        help="Print recent failures grouped by reason and bucketed over time, "
        "most-frequent first (offline twin of "
        "GET /metrics/failures/by-category/trends).",
    )
    fail_cat_trends_p.add_argument(
        "--days",
        type=int,
        default=30,
        help="Only count runs that failed in the last N days (default: 30).",
    )
    fail_cat_trends_p.add_argument(
        "--bucket",
        default="day",
        choices=("day", "week"),
        help="Time bucket for the trend (default: day).",
    )

    fail_repo_trends_p = sub.add_parser(
        "failures-by-repo-trends",
        help="Print recent failures grouped by routed repo and bucketed over time, "
        "most-frequent first (offline twin of "
        "GET /metrics/failures/by-repo/trends).",
    )
    fail_repo_trends_p.add_argument(
        "--days",
        type=int,
        default=30,
        help="Only count runs that failed in the last N days (default: 30).",
    )
    fail_repo_trends_p.add_argument(
        "--bucket",
        default="day",
        choices=("day", "week"),
        help="Time bucket for the trend (default: day).",
    )

    sub.add_parser(
        "approvals",
        help="Print runs awaiting human approval, oldest first (offline twin of "
        "GET /metrics/approvals).",
    )
    sub.add_parser(
        "executions",
        help="Print in-flight agent runs, oldest first (offline twin of "
        "GET /metrics/executions).",
    )
    sub.add_parser(
        "reviews",
        help="Print open PRs awaiting review, oldest first (offline twin of "
        "GET /metrics/reviews).",
    )

    def _add_days(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--days",
            type=int,
            default=90,
            help="Only consider runs that finished in the last N days (default: 90).",
        )

    def _add_bucket(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--bucket",
            default="week",
            choices=("day", "week"),
            help="Time bucket for the trend (default: week).",
        )

    _add_days(
        sub.add_parser(
            "delivery",
            help="Print org-wide delivery metrics (offline twin of "
            "GET /metrics/delivery).",
        )
    )

    deliv_trends_p = sub.add_parser(
        "delivery-trends",
        help="Print delivery outcomes bucketed over time (offline twin of "
        "GET /metrics/delivery/trends).",
    )
    _add_bucket(deliv_trends_p)
    _add_days(deliv_trends_p)

    _add_days(
        sub.add_parser(
            "delivery-by-repo",
            help="Print delivery outcomes grouped by routed repo (offline twin of "
            "GET /metrics/delivery/by-repo).",
        )
    )

    _add_days(
        sub.add_parser(
            "delivery-by-work-type",
            help="Print delivery outcomes grouped by work type (offline twin of "
            "GET /metrics/delivery/by-work-type).",
        )
    )

    deliv_repo_trends_p = sub.add_parser(
        "delivery-by-repo-trends",
        help="Print per-repo delivery outcomes bucketed over time (offline twin of "
        "GET /metrics/delivery/by-repo/trends).",
    )
    _add_bucket(deliv_repo_trends_p)
    _add_days(deliv_repo_trends_p)

    deliv_wt_trends_p = sub.add_parser(
        "delivery-by-work-type-trends",
        help="Print per-work-type delivery outcomes bucketed over time (offline "
        "twin of GET /metrics/delivery/by-work-type/trends).",
    )
    _add_bucket(deliv_wt_trends_p)
    _add_days(deliv_wt_trends_p)

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
    elif args.command == "failures-by-category":
        _run_failures_by_category(args)
    elif args.command == "failures-by-repo":
        _run_failures_by_repo(args)
    elif args.command == "failures-by-work-type":
        _run_failures_by_work_type(args)
    elif args.command == "failures-trends":
        _run_failures_trends(args)
    elif args.command == "failures-by-category-trends":
        _run_failures_by_category_trends(args)
    elif args.command == "failures-by-repo-trends":
        _run_failures_by_repo_trends(args)
    elif args.command == "approvals":
        _run_approvals()
    elif args.command == "executions":
        _run_executions()
    elif args.command == "reviews":
        _run_reviews()
    elif args.command == "delivery":
        _run_delivery(args)
    elif args.command == "delivery-trends":
        _run_delivery_trends(args)
    elif args.command == "delivery-by-repo":
        _run_delivery_by_repo(args)
    elif args.command == "delivery-by-work-type":
        _run_delivery_by_work_type(args)
    elif args.command == "delivery-by-repo-trends":
        _run_delivery_by_repo_trends(args)
    elif args.command == "delivery-by-work-type-trends":
        _run_delivery_by_work_type_trends(args)


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


def _fmt_cost(cost: float | None) -> str:
    """'$X' / '-' - None means no run reported a cost, never a conjured $0
    (matching the delivery aggregates, which leave ``total_cost_usd`` None)."""
    return "-" if cost is None else f"${cost}"


def _since_from_days(days: int):
    """The window start for a ``--days N`` flag, or exit(2) on a bad value -
    the CLI mirror of the endpoints' ``days >= 1`` guard."""
    from datetime import datetime, timedelta, timezone

    if days < 1:
        print("error: --days must be >= 1", file=sys.stderr)
        sys.exit(2)
    return datetime.now(timezone.utc) - timedelta(days=days)


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


def _run_failures_by_category(args: argparse.Namespace) -> None:
    from foundry.memory.metrics import failures_by_category

    since = _since_from_days(args.days)

    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = failures_by_category(session, since=since)

    categories = report["categories"]
    if not categories:
        print(f"No runs failed in the last {args.days}d - nothing to triage.")
        return

    print(
        f"Failures by category (last {args.days}d): {report['count']} total across "
        f"{report['distinct_categories']} categories, {report['blocked']} blocked, "
        f"{report['failed']} execution-failed (most frequent first):\n"
    )
    print(f"{'count':<7} {'blocked':<8} {'failed':<7} {'newest':<9} {'oldest':<9} reason")
    for cat in categories:
        print(
            f"{cat['count']:<7} {cat['blocked']:<8} {cat['failed']:<7} "
            f"{_fmt_age(cat['newest_failure_seconds']):<9} "
            f"{_fmt_age(cat['oldest_failure_seconds']):<9} {cat['category']}"
        )


def _run_failures_by_repo(args: argparse.Namespace) -> None:
    from foundry.memory.metrics import failures_by_repo

    since = _since_from_days(args.days)

    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = failures_by_repo(session, since=since)

    repos = report["repos"]
    if not repos:
        print(f"No runs failed in the last {args.days}d - nothing to triage.")
        return

    print(
        f"Failures by repo (last {args.days}d): {report['count']} total across "
        f"{report['distinct_repos']} repo(s), {report['blocked']} blocked, "
        f"{report['failed']} execution-failed (most frequent first):\n"
    )
    print(f"{'repo':<40} {'count':<7} {'blocked':<8} {'failed':<7} {'newest':<9} oldest")
    for repo in repos:
        print(
            f"{repo['repo']:<40} {repo['count']:<7} {repo['blocked']:<8} "
            f"{repo['failed']:<7} {_fmt_age(repo['newest_failure_seconds']):<9} "
            f"{_fmt_age(repo['oldest_failure_seconds'])}"
        )


def _run_failures_by_work_type(args: argparse.Namespace) -> None:
    from foundry.memory.metrics import failures_by_work_type

    since = _since_from_days(args.days)

    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = failures_by_work_type(session, since=since)

    work_types = report["work_types"]
    if not work_types:
        print(f"No runs failed in the last {args.days}d - nothing to triage.")
        return

    print(
        f"Failures by work type (last {args.days}d): {report['count']} total across "
        f"{report['distinct_work_types']} work type(s), {report['blocked']} blocked, "
        f"{report['failed']} execution-failed (most frequent first):\n"
    )
    print(
        f"{'work type':<20} {'count':<7} {'blocked':<8} {'failed':<7} "
        f"{'newest':<9} oldest"
    )
    for wt in work_types:
        print(
            f"{wt['work_type']:<20} {wt['count']:<7} {wt['blocked']:<8} "
            f"{wt['failed']:<7} {_fmt_age(wt['newest_failure_seconds']):<9} "
            f"{_fmt_age(wt['oldest_failure_seconds'])}"
        )


def _run_failures_trends(args: argparse.Namespace) -> None:
    from foundry.memory.metrics import failure_trends

    since = _since_from_days(args.days)

    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = failure_trends(session, since=since, bucket=args.bucket)

    periods = report["periods"]
    if not periods:
        print(f"No runs failed in the last {args.days}d - nothing to triage.")
        return

    label = "week of" if args.bucket == "week" else "day"
    print(
        f"Failures by {args.bucket} (last {args.days}d): {report['count']} total, "
        f"{report['blocked']} blocked, {report['failed']} execution-failed "
        f"(oldest period first):\n"
    )
    for period in periods:
        print(
            f"  {label} {period['period_start'][:10]}  "
            f"failures {period['count']:>3}  blocked {period['blocked']:>3}  "
            f"execution-failed {period['failed']:>3}"
        )


def _run_failures_by_category_trends(args: argparse.Namespace) -> None:
    from foundry.memory.metrics import failures_by_category_trends

    since = _since_from_days(args.days)

    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = failures_by_category_trends(session, since=since, bucket=args.bucket)

    categories = report["categories"]
    if not categories:
        print(f"No runs failed in the last {args.days}d - nothing to triage.")
        return

    periods = report["periods"]
    label = "week of" if args.bucket == "week" else "day"
    print(
        f"Failures by category by {args.bucket} (last {args.days}d): "
        f"{report['count']} total across {report['distinct_categories']} "
        f"reason(s), {report['blocked']} blocked, {report['failed']} "
        f"execution-failed (most frequent first):\n"
    )
    for cat in categories:
        print(
            f"{cat['category']}: {cat['count']} total "
            f"({cat['blocked']} blocked, {cat['failed']} execution-failed)"
        )
        for period_iso, cell in zip(periods, cat["series"]):
            print(
                f"    {label} {period_iso[:10]}  failures {cell['count']:>3}  "
                f"blocked {cell['blocked']:>3}  "
                f"execution-failed {cell['failed']:>3}"
            )
        print()


def _run_failures_by_repo_trends(args: argparse.Namespace) -> None:
    from foundry.memory.metrics import failures_by_repo_trends

    since = _since_from_days(args.days)

    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = failures_by_repo_trends(session, since=since, bucket=args.bucket)

    repos = report["repos"]
    if not repos:
        print(f"No runs failed in the last {args.days}d - nothing to triage.")
        return

    periods = report["periods"]
    label = "week of" if args.bucket == "week" else "day"
    print(
        f"Failures by repo by {args.bucket} (last {args.days}d): "
        f"{report['count']} total across {report['distinct_repos']} "
        f"repo(s), {report['blocked']} blocked, {report['failed']} "
        f"execution-failed (most frequent first):\n"
    )
    for repo in repos:
        print(
            f"{repo['repo']}: {repo['count']} total "
            f"({repo['blocked']} blocked, {repo['failed']} execution-failed)"
        )
        for period_iso, cell in zip(periods, repo["series"]):
            print(
                f"    {label} {period_iso[:10]}  failures {cell['count']:>3}  "
                f"blocked {cell['blocked']:>3}  "
                f"execution-failed {cell['failed']:>3}"
            )
        print()


def _render_inflight_queue(
    *, title: str, empty_msg: str, report: dict, age_key: str, age_header: str
) -> None:
    """Shared renderer for the single-age in-flight queues (approvals, executions).

    Both surface the same per-run shape - a parked run with one age and an
    ``sla_breached`` flag, the queue ordered oldest first - and differ only in the
    age field's name/header and the empty message, so the table layout lives here.
    """
    runs = report["runs"]
    if not runs:
        print(empty_msg)
        return
    print(
        f"{title}: {report['count']} total"
        f"{_sla_note(report['sla_breaches'], report['sla_seconds'])} (oldest first):\n"
    )
    print(f"{age_header:<9} {'status':<18} {'issue':<14} {'run':<14} step")
    for run in runs:
        breach = "  ! breaching SLA" if run["sla_breached"] else ""
        step = run["current_step"] or "-"
        print(
            f"{_fmt_age(run[age_key]):<9} {run['status']:<18} "
            f"{(run['linear_issue_key'] or '-'):<14} {run['run_id'][:12]:<14} "
            f"{step}{breach}"
        )


def _run_approvals() -> None:
    from foundry.memory.metrics import approval_queue

    settings, session_factory = _session_factory()
    with session_factory() as session:
        report = approval_queue(session, sla_seconds=settings.approval_sla_seconds)
    _render_inflight_queue(
        title="Approval queue (runs parked on a human)",
        empty_msg="No runs are awaiting human approval.",
        report=report,
        age_key="waiting_seconds",
        age_header="waited",
    )


def _run_executions() -> None:
    from foundry.memory.metrics import execution_queue

    settings, session_factory = _session_factory()
    with session_factory() as session:
        report = execution_queue(session, sla_seconds=settings.execution_sla_seconds)
    _render_inflight_queue(
        title="Execution queue (agents in flight)",
        empty_msg="No agents are currently running.",
        report=report,
        age_key="running_seconds",
        age_header="running",
    )


def _run_reviews() -> None:
    from foundry.memory.metrics import review_queue

    settings, session_factory = _session_factory()
    with session_factory() as session:
        report = review_queue(
            session,
            sla_seconds=settings.review_sla_seconds,
            stale_sla_seconds=settings.review_stale_sla_seconds,
        )

    runs = report["runs"]
    if not runs:
        print("No open PRs are awaiting review.")
        return

    print(
        f"Review queue (open PRs awaiting review): {report['count']} total"
        f"{_sla_note(report['sla_breaches'], report['sla_seconds'])} (oldest first):\n"
    )
    # Staleness ("inactive since last push") is a separate signal from the open-age
    # review SLA, so it gets its own knob and summary line - shown only when set.
    stale_note = _sla_note(report["stale_breaches"], report["stale_sla_seconds"])
    if stale_note:
        print(f"  staleness{stale_note}\n")

    print(f"{'unreviewed':<11} {'inactive':<10} {'status':<10} {'issue':<14} run")
    for run in runs:
        flags = []
        if run["sla_breached"]:
            flags.append("review")
        if run["stale_breached"]:
            flags.append("stale")
        breach = f"  ! {'+'.join(flags)} SLA" if flags else ""
        print(
            f"{_fmt_age(run['unreviewed_seconds']):<11} "
            f"{_fmt_age(run['inactive_seconds']):<10} "
            f"{run['status']:<10} {(run['linear_issue_key'] or '-'):<14} "
            f"{run['run_id'][:12]}{breach}"
        )


def _run_delivery(args: argparse.Namespace) -> None:
    from foundry.memory.metrics import delivery_metrics

    since = _since_from_days(args.days)
    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = delivery_metrics(session, since=since)

    print(
        f"Delivery metrics (last {args.days}d): "
        f"{report['runs_finished']} runs finished\n"
    )
    print(f"  PRs shipped       {report['prs_shipped']}")
    print(f"  blocked           {report['blocked']}")
    print(f"  rejected          {report['rejected']}")
    print(f"  failed            {report['failed']}")
    print(f"  needs clarif.     {report['needs_clarification']}")
    print(f"  retries consumed  {report['retries_consumed']}")
    print(f"  escalations       {report['escalations']}")
    print(f"  CI failures       {report['ci_failures']}")
    print(f"  spend             {_fmt_cost(report['total_cost_usd'])}")
    ttm = report["time_to_merge_seconds"]
    if ttm["count"]:
        print(
            f"  time-to-merge     median {_fmt_age(ttm['median'])}, "
            f"p90 {_fmt_age(ttm['p90'])} (n={ttm['count']})"
        )
    else:
        print("  time-to-merge     - (no merges)")

    if report["blocks_by_reason"]:
        print("\n  blocks by reason:")
        for reason, count in sorted(report["blocks_by_reason"].items()):
            print(f"    {reason:<28} {count}")
        # How many of those blocks a later merged rerun on the same issue
        # superseded (a human fixed the input) - the "was the block justified?"
        # signal the endpoint carries alongside the reason breakdown.
        print(
            f"    {'(superseded by later merge)':<28} "
            f"{report['blocked_superseded_by_merged_run']}"
        )

    if report["precision_by_confidence_band"]:
        print("\n  routing precision by confidence band:")
        for band in report["precision_by_confidence_band"]:
            print(
                f"    {band['band']:<8} {band['merged']}/{band['routed']} merged "
                f"(precision {band['precision']})"
            )


def _run_delivery_trends(args: argparse.Namespace) -> None:
    from foundry.memory.metrics import delivery_trends

    since = _since_from_days(args.days)
    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = delivery_trends(session, since=since, bucket=args.bucket)

    periods = report["periods"]
    if not periods:
        print(f"No runs finished in the last {args.days}d.")
        return

    label = "week of" if args.bucket == "week" else "day"
    print(f"Delivery by {args.bucket} (last {args.days}d):\n")
    for period in periods:
        print(
            f"  {label} {period['period_start'][:10]}  "
            f"shipped {period['prs_shipped']:>3}  blocked {period['blocked']:>3}  "
            f"runs {period['runs_finished']:>3}  retries {period['retries_consumed']:>3}  "
            f"spend {_fmt_cost(period['total_cost_usd'])}"
        )


def _run_delivery_by_repo(args: argparse.Namespace) -> None:
    from foundry.memory.metrics import delivery_by_repo

    since = _since_from_days(args.days)
    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = delivery_by_repo(session, since=since)

    repos = report["repos"]
    if not repos:
        print(f"No runs finished in the last {args.days}d.")
        return

    print(
        f"Delivery by repo (last {args.days}d): {report['runs_finished']} runs "
        f"finished across {len(repos)} repo(s) (most-shipping first):\n"
    )
    print(
        f"{'repo':<40} {'shipped':<8} {'blocked':<8} {'merge%':<7} "
        f"{'retries':<8} {'spend':<9} ttm median"
    )
    for repo in repos:
        ttm = repo["time_to_merge_seconds"]
        print(
            f"{repo['repo']:<40} {repo['prs_shipped']:<8} {repo['blocked']:<8} "
            f"{repo['merge_rate']:<7} {repo['retries_consumed']:<8} "
            f"{_fmt_cost(repo['total_cost_usd']):<9} {_fmt_age(ttm['median'])}"
        )


def _run_delivery_by_work_type(args: argparse.Namespace) -> None:
    from foundry.memory.metrics import delivery_by_work_type

    since = _since_from_days(args.days)
    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = delivery_by_work_type(session, since=since)

    work_types = report["work_types"]
    if not work_types:
        print(f"No runs finished in the last {args.days}d.")
        return

    print(
        f"Delivery by work type (last {args.days}d): {report['runs_finished']} "
        f"runs finished across {len(work_types)} type(s) (most-shipping first):\n"
    )
    print(
        f"{'work type':<16} {'shipped':<8} {'blocked':<8} {'merge%':<7} "
        f"{'retries':<8} {'spend':<9} ttm median"
    )
    for wt in work_types:
        ttm = wt["time_to_merge_seconds"]
        print(
            f"{wt['work_type']:<16} {wt['prs_shipped']:<8} {wt['blocked']:<8} "
            f"{wt['merge_rate']:<7} {wt['retries_consumed']:<8} "
            f"{_fmt_cost(wt['total_cost_usd']):<9} {_fmt_age(ttm['median'])}"
        )


def _run_delivery_by_repo_trends(args: argparse.Namespace) -> None:
    from foundry.memory.metrics import delivery_by_repo_trends

    since = _since_from_days(args.days)
    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = delivery_by_repo_trends(session, since=since, bucket=args.bucket)

    repos = report["repos"]
    if not repos:
        print(f"No runs finished in the last {args.days}d.")
        return

    periods = report["periods"]
    label = "week of" if args.bucket == "week" else "day"
    print(
        f"Per-repo delivery by {args.bucket} (last {args.days}d, "
        f"most-shipping first):\n"
    )
    for repo in repos:
        print(
            f"{repo['repo']}: {repo['prs_shipped']}/{repo['runs_finished']} merged "
            f"(rate {repo['merge_rate']}), {repo['retries_consumed']} retries, "
            f"{_fmt_cost(repo['total_cost_usd'])}"
        )
        for period_iso, cell in zip(periods, repo["series"]):
            print(
                f"    {label} {period_iso[:10]}  shipped {cell['prs_shipped']:>3}  "
                f"blocked {cell['blocked']:>3}  runs {cell['runs_finished']:>3}"
            )
        print()


def _run_delivery_by_work_type_trends(args: argparse.Namespace) -> None:
    from foundry.memory.metrics import delivery_by_work_type_trends

    since = _since_from_days(args.days)
    _settings, session_factory = _session_factory()
    with session_factory() as session:
        report = delivery_by_work_type_trends(session, since=since, bucket=args.bucket)

    work_types = report["work_types"]
    if not work_types:
        print(f"No runs finished in the last {args.days}d.")
        return

    periods = report["periods"]
    label = "week of" if args.bucket == "week" else "day"
    print(
        f"Per-work-type delivery by {args.bucket} (last {args.days}d, "
        f"most-shipping first):\n"
    )
    for wt in work_types:
        print(
            f"{wt['work_type']}: {wt['prs_shipped']}/{wt['runs_finished']} merged "
            f"(rate {wt['merge_rate']}), {wt['retries_consumed']} retries, "
            f"{_fmt_cost(wt['total_cost_usd'])}"
        )
        for period_iso, cell in zip(periods, wt["series"]):
            print(
                f"    {label} {period_iso[:10]}  shipped {cell['prs_shipped']:>3}  "
                f"blocked {cell['blocked']:>3}  runs {cell['runs_finished']:>3}"
            )
        print()
