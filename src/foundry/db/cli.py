"""``foundry-db`` - offline maintenance over Foundry's database.

Today this is a single command:

    foundry-db reencrypt-artifacts [--dry-run]

It re-wraps every ``foundry_artifacts`` payload that is not already encrypted
under the *current* primary key (legacy plaintext, or ciphertext under a
rotated-away key), so artifact encryption at rest finally covers historical
rows and a retired key can be dropped from ``FOUNDRY_ARTIFACT_ENCRYPTION_KEY``.
See ``db/maintenance.py`` for the why and the guarantees (plaintext-preserving,
all-orgs, idempotent). A privileged operator task: it spans every tenant.
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="foundry-db",
        description="Offline maintenance over Foundry's database.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    reenc_p = sub.add_parser(
        "reencrypt-artifacts",
        help="Re-wrap artifact payloads under the current primary encryption key.",
    )
    reenc_p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report what would be re-wrapped without writing any changes.",
    )

    args = parser.parse_args(argv)

    if args.command == "reencrypt-artifacts":
        _run_reencrypt(args)


def _run_reencrypt(args: argparse.Namespace) -> None:
    from foundry.config import Settings
    from foundry.db.base import init_schema, make_engine, make_session_factory
    from foundry.db.encryption import build_cipher
    from foundry.db.maintenance import reencrypt_artifacts

    settings = Settings.load(os.environ.get("FOUNDRY_CONFIG"), env=os.environ)
    cipher = build_cipher(settings.artifact_encryption_key)
    if not cipher.enabled:
        print(
            "No artifact encryption key configured "
            "(FOUNDRY_ARTIFACT_ENCRYPTION_KEY): nothing to re-encrypt.",
            file=sys.stderr,
        )
        sys.exit(1)

    engine = make_engine(settings.database_url)
    init_schema(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        report = reencrypt_artifacts(session, cipher, dry_run=args.dry_run)

    verb = "would be re-wrapped" if args.dry_run else "re-wrapped"
    print(
        f"Re-encrypt complete: {report.scanned} scanned, "
        f"{report.rewrapped} {verb}, {report.skipped} already current, "
        f"{report.failed} failed."
    )
    if report.failed and not report.rewrapped:
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
