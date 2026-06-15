"""Console entry point for the starter policy library (issue #31).

Usage::

    foundry-policy presets                 # list the shipped presets
    foundry-policy show <name>             # print a preset's YAML (copy-to-adopt)
    foundry-policy explain <name>          # show the gate knobs a preset resolves to

This is decision-support and documentation only - it **never** changes a running
deployment's policy. ``presets`` lists what the library ships; ``show`` prints a
preset's raw YAML so you can copy it into your own ``foundry.yaml`` and adapt the
repo names / approvers; ``explain`` loads the preset through the same
``Settings`` validator your config uses and prints the effective gate knobs (the
confidence threshold, protected paths, per-repo overrides and the retry/budget
caps), so you can see a preset's effect without standing up a run.

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

    args = parser.parse_args(argv)
    if args.command == "presets":
        _run_presets()
    elif args.command == "show":
        _run_show(args.name)
    elif args.command == "explain":
        _run_explain(args.name)


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

    if summary["repo_forbidden_globs"]:
        print("\n  per-repo extra forbidden globs:")
        for repo, globs in summary["repo_forbidden_globs"].items():
            print(f"      {repo}: {', '.join(globs)}")
