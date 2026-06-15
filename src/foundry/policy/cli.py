"""Console entry point for the starter policy library (issue #31).

Usage::

    foundry-policy presets                 # list the shipped presets
    foundry-policy show <name>             # print a preset's YAML (copy-to-adopt)
    foundry-policy explain <name>          # show the gate knobs a preset resolves to
    foundry-policy check --against <name>  # verify YOUR config meets a baseline

This is decision-support and documentation only - it **never** changes a running
deployment's policy. ``presets`` lists what the library ships; ``show`` prints a
preset's raw YAML so you can copy it into your own ``foundry.yaml`` and adapt the
repo names / approvers; ``explain`` loads the preset through the same
``Settings`` validator your config uses and prints the effective gate knobs (the
confidence threshold, protected paths, per-repo overrides and the retry/budget
caps), so you can see a preset's effect without standing up a run.

``check`` is the verification counterpart to ``explain``: it loads *your* config
(``--config`` or ``FOUNDRY_CONFIG``) and a baseline (a preset name or a path to
another config) and reports, control by control, whether your config is *at
least as strict* as the baseline - exiting non-zero when it is weaker, so it
drops straight into a compliance CI pipeline. The comparison is read-only and
uses only the already-gated knobs, so it adds no policy mechanism.

The presets are committed YAML built only from existing, already-gated config
knobs - they add no new policy mechanism and touch neither ``policy/engine.py``
nor ``foundry.rego``.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="foundry-policy",
        description="Browse and adopt Foundry's starter policy library.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("presets", help="List the starter policy presets.")

    show_p = sub.add_parser(
        "show", help="Print a preset's YAML so you can copy it into foundry.yaml."
    )
    show_p.add_argument("name", help="Preset name (see 'foundry-policy presets').")

    explain_p = sub.add_parser(
        "explain", help="Show the effective gate knobs a preset resolves to."
    )
    explain_p.add_argument("name", help="Preset name (see 'foundry-policy presets').")

    check_p = sub.add_parser(
        "check",
        help="Verify your config is at least as strict as a baseline (exits "
        "non-zero if weaker).",
    )
    check_p.add_argument(
        "--config",
        help="Path to the config to check (defaults to $FOUNDRY_CONFIG).",
    )
    check_p.add_argument(
        "--against",
        required=True,
        metavar="PRESET_OR_PATH",
        help="Baseline to check against: a preset name (e.g. 'soc2') or a path "
        "to another config file.",
    )

    args = parser.parse_args(argv)
    if args.command == "presets":
        _run_presets()
    elif args.command == "show":
        _run_show(args.name)
    elif args.command == "explain":
        _run_explain(args.name)
    elif args.command == "check":
        _run_check(args.config, args.against)


def _run_presets() -> None:
    from foundry.policy.library import list_presets

    presets = list_presets()
    print("Starter policy presets (copy-to-adopt; nothing is applied automatically):\n")
    for preset in presets:
        print(f"  {preset.name}")
        print(f"      {preset.summary}")
        print()
    print("Use 'foundry-policy show <name>' to print one, or 'explain <name>' for "
          "its effective gate knobs.")


def _run_show(name: str) -> None:
    from foundry.policy.library import load_preset_yaml

    try:
        sys.stdout.write(load_preset_yaml(name))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


def _run_explain(name: str) -> None:
    from foundry.policy.library import effective_policy_summary, load_preset_settings

    try:
        settings = load_preset_settings(name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    summary = effective_policy_summary(settings)
    print(f"Effective policy for preset '{name}':\n")
    print(f"  repo_confidence_threshold : {summary['repo_confidence_threshold']}")
    print(f"  max_files_changed         : {summary['max_files_changed']}")
    print(f"  min_approvals             : {summary['min_approvals']}")
    print(f"  max_agent_retries         : {summary['max_agent_retries']}")
    print(f"  retry_on                  : {', '.join(summary['retry_on']) or '-'}")
    cap = summary["max_cost_per_run"]
    print(f"  max_cost_per_run          : {('$' + str(cap)) if cap is not None else 'none'}")
    print(f"  estimated_cost_per_dispatch: ${summary['estimated_cost_per_dispatch']}")
    print(f"  approvers configured      : {summary['approver_count']}")

    print("\n  forbidden_globs (never modified, never retried):")
    for glob in summary["forbidden_globs"]:
        print(f"      {glob}")

    if summary["repo_required_roles"]:
        print("\n  per-repo required approval roles (additive - only ever stricter):")
        for repo, roles in summary["repo_required_roles"].items():
            print(f"      {repo}: {', '.join(roles)}")

    if summary["repo_min_approvals"]:
        print("\n  per-repo minimum distinct approvers (additive - max with global):")
        for repo, count in summary["repo_min_approvals"].items():
            print(f"      {repo}: {count}")

    if summary["path_required_roles"]:
        print("\n  per-path required approval roles (diff-aware - only ever stricter):")
        for glob, roles in summary["path_required_roles"].items():
            print(f"      {glob}: {', '.join(roles)}")

    if summary["repo_forbidden_globs"]:
        print("\n  per-repo extra forbidden globs:")
        for repo, globs in summary["repo_forbidden_globs"].items():
            print(f"      {repo}: {', '.join(globs)}")


def _run_check(config_path: str | None, against: str) -> None:
    import os

    from foundry.config import Settings
    from foundry.policy.library import compare_policy_strictness, resolve_settings

    source = config_path or os.environ.get("FOUNDRY_CONFIG")
    if not source:
        print(
            "error: no config to check; pass --config PATH or set FOUNDRY_CONFIG",
            file=sys.stderr,
        )
        sys.exit(2)

    from pathlib import Path

    if not Path(source).exists():
        print(f"error: config file not found: {source}", file=sys.stderr)
        sys.exit(2)

    try:
        subject = Settings.load(source, env=os.environ)
        baseline = resolve_settings(against)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    comparison = compare_policy_strictness(subject, baseline)
    print(f"Checking '{source}' against baseline '{against}':\n")
    for finding in comparison.findings:
        marker = "PASS" if finding.ok else "FAIL"
        print(f"  {marker}  {finding.knob:<26}: {finding.detail}")

    print()
    if comparison.ok:
        print(f"RESULT: PASS - config meets or exceeds baseline '{against}'.")
    else:
        weak = len(comparison.weaknesses)
        print(
            f"RESULT: FAIL - config is weaker than baseline '{against}' on "
            f"{weak} control(s)."
        )
        sys.exit(1)
