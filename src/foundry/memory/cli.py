"""Console entry point for delivery memory.

Usage::

    foundry-memory backfill [--recompute]
    foundry-memory show-priors
    foundry-memory show-scorecards

``backfill`` derives outcome rows for terminal runs that finished before the
``foundry_run_outcomes`` table existed (or that a fail-soft hook missed);
``--recompute`` re-derives every terminal run from the audit trail.
``show-priors`` prints the mined routing priors; ``show-scorecards`` prints
per-provider agent performance. Settings come from ``FOUNDRY_CONFIG`` and the
usual environment variable overrides.
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

    args = parser.parse_args()
    if args.command == "backfill":
        _run_backfill(args)
    elif args.command == "show-priors":
        _run_show_priors()
    elif args.command == "show-scorecards":
        _run_show_scorecards()


def _session_factory():
    from foundry.config import Settings
    from foundry.db.base import create_all, make_engine, make_session_factory

    settings = Settings.load(os.environ.get("FOUNDRY_CONFIG"), env=os.environ)
    engine = make_engine(settings.database_url)
    create_all(engine)
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
