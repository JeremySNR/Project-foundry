"""Console entry point for compliance evidence exports.

Usage::

    foundry-evidence run <run_id>     [--format json|html] [--output PATH]
    foundry-evidence epic <run_id>    [--format json|html] [--output PATH]
    foundry-evidence archive [--from ISO] [--to ISO] [--days N]
                             [--format json|html] [--output PATH]

This is the offline twin of the evidence endpoints (``GET /runs/{id}/evidence``,
``GET /runs/{id}/epic/evidence``, ``GET /evidence``): it reads the same
content-hashed audit trail straight from the database and produces the *same*
packs from the *same* builders/renderers, so an auditor can get a JSON or HTML
evidence pack without standing up the API or holding a bearer token. Control
mappings come from committed config (``compliance.control_mappings``), never from
input - exactly like the API.

``run`` exports one run's pack. ``epic`` exports an epic's whole cross-run chain,
resolving the epic root first so it works when pointed at a child run too
(mirrors ``GET /runs/{id}/epic/evidence``). ``archive`` exports every run created
in a date range, with the same ``from``-inclusive / ``to``-exclusive bound
semantics as ``GET /evidence`` (a date-only ``to`` covers the whole day), falling
back to the last ``--days`` (default 90) when no explicit window is given.

Settings come from ``FOUNDRY_CONFIG`` and the usual environment variable
overrides.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="foundry-evidence",
        description="Export Foundry compliance evidence packs (offline, no API).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Export one run's evidence pack.")
    run_p.add_argument("run_id", help="The run id to export.")
    _add_output_args(run_p)

    epic_p = sub.add_parser(
        "epic",
        help="Export an epic's cross-run evidence pack (root + children).",
    )
    epic_p.add_argument(
        "run_id",
        help="Any run in the epic; the epic root is resolved first.",
    )
    _add_output_args(epic_p)

    archive_p = sub.add_parser(
        "archive",
        help="Export every run in a date range as one org-wide archive.",
    )
    archive_p.add_argument(
        "--from",
        dest="from_",
        default=None,
        metavar="ISO",
        help="Start of the window (inclusive), ISO 8601 date or datetime.",
    )
    archive_p.add_argument(
        "--to",
        default=None,
        metavar="ISO",
        help=(
            "End of the window (exclusive), ISO 8601 date or datetime; a "
            "date-only value covers the whole day. Defaults to now."
        ),
    )
    archive_p.add_argument(
        "--days",
        type=int,
        default=None,
        help="Window length in days when --from is omitted (default: 90).",
    )
    _add_output_args(archive_p)

    args = parser.parse_args()
    if args.command == "run":
        _run_export(args)
    elif args.command == "epic":
        _epic_export(args)
    elif args.command == "archive":
        _archive_export(args)


def _add_output_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--format",
        default="json",
        choices=("json", "html"),
        help="Output format (default: json).",
    )
    p.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="Write to PATH instead of stdout.",
    )


def _session_factory():
    from foundry.config import Settings
    from foundry.db.base import init_schema, make_engine, make_session_factory

    settings = Settings.load(os.environ.get("FOUNDRY_CONFIG"), env=os.environ)
    engine = make_engine(settings.database_url)
    init_schema(engine)
    return settings, make_session_factory(engine)


def _emit(content: str, output: str | None) -> None:
    """Write ``content`` to ``output`` (a path) or to stdout."""
    if output is None:
        print(content)
        return
    with open(output, "w", encoding="utf-8") as fh:
        fh.write(content if content.endswith("\n") else content + "\n")
    print(f"Wrote {output}", file=sys.stderr)


def _dump_json(pack: dict[str, Any]) -> str:
    # Pack values are already JSON-serialisable (ISO strings, plain scalars), so
    # no custom encoder is needed. Insertion order is preserved (it mirrors the
    # API's JSON) - no sort_keys.
    return json.dumps(pack, indent=2)


def _run_export(args: argparse.Namespace) -> None:
    from foundry.compliance.evidence import build_evidence_pack, render_evidence_html
    from foundry.db.models import FoundryRun

    settings, session_factory = _session_factory()
    with session_factory() as session:
        run = session.get(FoundryRun, args.run_id)
        if run is None:
            print(f"error: run not found: {args.run_id}", file=sys.stderr)
            sys.exit(1)
        pack = build_evidence_pack(
            session,
            run,
            control_mappings=settings.compliance_control_mappings,
        )
        content = render_evidence_html(pack) if args.format == "html" else _dump_json(pack)
    _emit(content, args.output)


def _epic_export(args: argparse.Namespace) -> None:
    from foundry.compliance.evidence import (
        build_epic_evidence_pack,
        render_epic_evidence_html,
    )
    from foundry.db.models import FoundryRun

    settings, session_factory = _session_factory()
    with session_factory() as session:
        run = session.get(FoundryRun, args.run_id)
        if run is None:
            print(f"error: run not found: {args.run_id}", file=sys.stderr)
            sys.exit(1)
        # Resolve the epic root (a child resolves to its parent), then load its
        # children - mirroring GET /runs/{id}/epic/evidence.
        root_id = run.parent_run_id or run.id
        root = session.get(FoundryRun, root_id)
        children = (
            session.query(FoundryRun)
            .filter(FoundryRun.parent_run_id == root_id)
            .order_by(FoundryRun.created_at, FoundryRun.id)
            .all()
        )
        pack = build_epic_evidence_pack(
            session,
            root,
            children,
            control_mappings=settings.compliance_control_mappings,
        )
        content = (
            render_epic_evidence_html(pack)
            if args.format == "html"
            else _dump_json(pack)
        )
    _emit(content, args.output)


def _archive_export(args: argparse.Namespace) -> None:
    from foundry.compliance.evidence import (
        build_evidence_archive,
        render_archive_html,
    )

    # Same bound semantics as GET /evidence: from inclusive, to exclusive, a
    # date-only `to` covers the whole day, default window is the last 90 days.
    until = (
        _parse_iso_bound(args.to, inclusive_day_end=True)
        if args.to
        else datetime.now(timezone.utc)
    )
    if args.from_:
        since: datetime | None = _parse_iso_bound(args.from_, inclusive_day_end=False)
    else:
        window = 90 if args.days is None else args.days
        if window < 1:
            print("error: --days must be >= 1", file=sys.stderr)
            sys.exit(2)
        since = until - timedelta(days=window)
    if since >= until:
        print("error: --from must be before --to", file=sys.stderr)
        sys.exit(2)

    settings, session_factory = _session_factory()
    with session_factory() as session:
        archive = build_evidence_archive(
            session,
            since=since,
            until=until,
            control_mappings=settings.compliance_control_mappings,
        )
        content = (
            render_archive_html(archive)
            if args.format == "html"
            else _dump_json(archive)
        )
    _emit(content, args.output)


def _parse_iso_bound(value: str, *, inclusive_day_end: bool) -> datetime:
    """Parse an ISO 8601 date/datetime bound into an aware UTC datetime.

    A naive value is assumed UTC. A date-only value (``YYYY-MM-DD``) used as the
    *end* of a range is bumped to the next midnight so the named day is fully
    included (the underlying filter is half-open: ``since <= created_at < until``).
    Mirrors the API's ``_parse_iso_bound`` but exits 2 on a bad value instead of
    raising an HTTP error.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        print(f"error: invalid ISO 8601 date/datetime: {value!r}", file=sys.stderr)
        sys.exit(2)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if inclusive_day_end and len(value) == 10 and "T" not in value:
        parsed = parsed + timedelta(days=1)
    return parsed
