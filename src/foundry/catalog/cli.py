"""Console entry point for the repo catalog sync.

Usage::

    foundry-catalog sync [--org ORG] [--bootstrap] [--budget N]

Settings come from ``FOUNDRY_CONFIG`` (path to a YAML file) and the usual
environment variable overrides.  ``--org`` falls back to ``settings.context_org``
if not provided.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="foundry-catalog",
        description="Manage the Foundry repo catalog.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync_p = sub.add_parser("sync", help="Sync GitHub org metadata into the catalog.")
    sync_p.add_argument("--org", default=None, help="GitHub org to sync (overrides config).")
    sync_p.add_argument(
        "--bootstrap",
        action="store_true",
        default=False,
        help="Force a deep fetch of every repo, even if unchanged.",
    )
    sync_p.add_argument(
        "--budget",
        type=int,
        default=None,
        help="Maximum number of GitHub API calls (overrides config default).",
    )
    sync_p.add_argument(
        "--code-facts",
        action="store_true",
        default=False,
        help=(
            "Also fetch code facts per repo (file tree, CODEOWNERS, manifests). "
            "Implied by context.provider=code or context.sync_code_facts in config."
        ),
    )

    args = parser.parse_args()

    if args.command == "sync":
        _run_sync(args)


def _run_sync(args: argparse.Namespace) -> None:
    from foundry.config import Settings
    from foundry.connectors.transport import github_rest_transport
    from foundry.db.base import init_schema, make_engine, make_session_factory
    from foundry.catalog.sync import CatalogSync, CatalogSyncError

    settings = Settings.load(os.environ.get("FOUNDRY_CONFIG"), env=os.environ)

    org = args.org or settings.context_org
    if not org:
        print(
            "Error: no org specified. Pass --org ORG or set 'context.org' in the config.",
            file=sys.stderr,
        )
        sys.exit(2)

    token = settings.github_api_token
    if not token:
        print(
            "Error: FOUNDRY_GITHUB_API_TOKEN is required for catalog sync.",
            file=sys.stderr,
        )
        sys.exit(2)

    call_budget = args.budget if args.budget is not None else settings.context_sync_call_budget
    fetch_code_facts = (
        args.code_facts
        or settings.context_sync_code_facts
        or settings.context_provider == "code"
    )

    engine = make_engine(settings.database_url)
    init_schema(engine)
    session_factory = make_session_factory(engine)
    transport = github_rest_transport(token)

    sync = CatalogSync(
        session_factory,
        transport,
        call_budget=call_budget,
        fetch_code_facts=fetch_code_facts,
        tree_max_paths=settings.context_tree_max_paths,
    )
    try:
        report = sync.sync(org, bootstrap=args.bootstrap)
    except CatalogSyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    status = " (budget exhausted — run again to continue)" if report.budget_exhausted else ""
    print(
        f"Sync complete{status}: "
        f"{report.repos_listed} repos listed, "
        f"{report.deep_fetched} deep-fetched, "
        f"{report.deleted} deleted, "
        f"{report.calls_used} API calls used."
    )
    sys.exit(0)
