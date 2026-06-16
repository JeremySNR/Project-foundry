"""Console entry point for the starter policy library (issue #31).

Usage::

    foundry-policy presets                 # list the shipped presets
    foundry-policy show <name>             # print a preset's YAML (copy-to-adopt)
    foundry-policy explain <name>          # show the gate knobs a preset resolves to
    foundry-policy explain --config PATH   # ...or the gate YOUR own config resolves to
    foundry-policy explain --config PATH --format json  # machine-readable
    foundry-policy check --against <name>  # verify YOUR config meets a baseline
    foundry-policy check --against <name> --format json   # machine-readable

This is decision-support and documentation only - it **never** changes a running
deployment's policy. ``presets`` lists what the library ships; ``show`` prints a
preset's raw YAML so you can copy it into your own ``foundry.yaml`` and adapt the
repo names / approvers; ``explain`` loads a config through the same ``Settings``
validator your deployment uses and prints the effective gate knobs (the
confidence threshold, protected paths, per-repo overrides and the retry/budget
caps), so you can see its effect without standing up a run. ``explain`` accepts
**either** a preset name (the reference baseline, loaded pure) **or** your own
deployment config (``--config PATH``, a path argument, or ``$FOUNDRY_CONFIG`` -
loaded with the process environment so ``FOUNDRY_*`` overrides are reflected),
so you can answer "what does *my* gate actually resolve to?", not just "what
does this preset resolve to?". It takes ``--format text`` (default) or
``--format json`` (the same effective knobs as a machine-readable object on
stdout, for a dashboard/CI step).

``check`` is the verification counterpart to ``explain``: it loads *your* config
(``--config`` or ``FOUNDRY_CONFIG``) and a baseline (a preset name or a path to
another config) and reports, control by control, whether your config is *at
least as strict* as the baseline - exiting non-zero when it is weaker, so it
drops straight into a compliance CI pipeline. The comparison is read-only and
uses only the already-gated knobs, so it adds no policy mechanism. ``check``
takes ``--format text`` (default, the human report) or ``--format json`` (the
same per-control verdicts as a machine-readable object on stdout, for a CI step
that wants to parse the result rather than scrape the text); the exit code is
identical in both modes (0 = meets/exceeds the baseline, 1 = weaker, 2 = a usage
or config error).

The presets are committed YAML built only from existing, already-gated config
knobs - they add no new policy mechanism and touch neither ``policy/engine.py``
nor ``foundry.rego``.
"""

from __future__ import annotations

import argparse
import sys
from typing import NoReturn


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
        "explain",
        help="Show the effective gate knobs a preset OR your own config resolves to.",
    )
    explain_p.add_argument(
        "target",
        nargs="?",
        help="A preset name (e.g. 'soc2') or a path to a config file. Omit to use "
        "--config or $FOUNDRY_CONFIG.",
    )
    explain_p.add_argument(
        "--config",
        help="Path to your own config to introspect (defaults to $FOUNDRY_CONFIG). "
        "Loaded with the process environment so FOUNDRY_* overrides are reflected.",
    )
    explain_p.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format: 'text' (default, human report) or 'json' "
        "(machine-readable effective knobs on stdout for a dashboard/CI step).",
    )

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
    check_p.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format: 'text' (default, human report) or 'json' "
        "(machine-readable per-control verdicts on stdout for CI). The exit "
        "code is the same either way (0 ok, 1 weaker, 2 usage/config error).",
    )

    args = parser.parse_args(argv)
    if args.command == "presets":
        _run_presets()
    elif args.command == "show":
        _run_show(args.name)
    elif args.command == "explain":
        _run_explain(args.target, args.config, args.format)
    elif args.command == "check":
        _run_check(args.config, args.against, args.format)


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


def _run_explain(
    target: str | None, config_path: str | None, fmt: str = "text"
) -> None:
    import json
    import os
    from pathlib import Path

    from foundry.config import Settings
    from foundry.policy.library import (
        available_preset_names,
        effective_policy_summary,
        load_preset_settings,
    )

    def _fail(message: str) -> NoReturn:
        # Usage / config errors exit 2, mirroring `check`. In json mode the error
        # is a structured object on stderr so a json consumer never parses prose.
        if fmt == "json":
            json.dump({"error": message}, sys.stderr)
            sys.stderr.write("\n")
        else:
            print(f"error: {message}", file=sys.stderr)
        sys.exit(2)

    if target is not None and config_path is not None:
        _fail("pass a preset/config positional argument OR --config, not both")

    # Resolve what to introspect, distinguishing a *reference preset* (loaded pure,
    # like `show`/the old `explain`) from the operator's *own config* (loaded with
    # the process environment so FOUNDRY_* overrides resolve as they do at runtime).
    kind: str
    source: str
    try:
        if config_path is not None:
            source, kind = config_path, "config"
            if not Path(config_path).exists():
                _fail(f"config file not found: {config_path}")
            settings = Settings.load(config_path, env=os.environ)
        elif target is not None and target in available_preset_names():
            source, kind = target, "preset"
            settings = load_preset_settings(target)
        elif target is not None:
            # Not a known preset, so treat it as a path to the operator's config.
            source, kind = target, "config"
            if not Path(target).exists():
                _fail(
                    f"{target!r} is neither a known preset nor an existing config "
                    f"file; presets: {', '.join(available_preset_names())}"
                )
            settings = Settings.load(target, env=os.environ)
        else:
            env_config = os.environ.get("FOUNDRY_CONFIG")
            if not env_config:
                _fail(
                    "nothing to explain; pass a preset name, a config path, "
                    "--config PATH, or set FOUNDRY_CONFIG"
                )
            source, kind = env_config, "config"
            if not Path(env_config).exists():
                _fail(f"config file not found: {env_config}")
            settings = Settings.load(env_config, env=os.environ)
    except ValueError as exc:
        _fail(str(exc))

    summary = effective_policy_summary(settings)

    if fmt == "json":
        json.dump(
            {"source": source, "kind": kind, "policy": summary},
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return

    label = f"preset '{source}'" if kind == "preset" else f"config '{source}'"
    print(f"Effective policy for {label}:\n")
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

    if summary["change_freeze_windows"]:
        print(
            "\n  change-freeze windows "
            "(autonomous re-dispatch held for a human while active):"
        )
        for window in summary["change_freeze_windows"]:
            print(f"      {window}")


def _run_check(config_path: str | None, against: str, fmt: str = "text") -> None:
    import json
    import os
    from pathlib import Path

    from foundry.config import Settings
    from foundry.policy.library import (
        compare_policy_strictness,
        comparison_to_dict,
        resolve_settings,
    )

    def _fail(message: str) -> NoReturn:
        # Usage / config errors exit 2 - distinct from a clean "weaker" verdict
        # (exit 1) - so CI can tell "you misconfigured the check" from "the
        # config failed the baseline". In json mode the error is emitted as a
        # structured object on stderr, so a json consumer never parses free text.
        if fmt == "json":
            json.dump({"error": message}, sys.stderr)
            sys.stderr.write("\n")
        else:
            print(f"error: {message}", file=sys.stderr)
        sys.exit(2)

    source = config_path or os.environ.get("FOUNDRY_CONFIG")
    if not source:
        _fail("no config to check; pass --config PATH or set FOUNDRY_CONFIG")

    if not Path(source).exists():
        _fail(f"config file not found: {source}")

    try:
        subject = Settings.load(source, env=os.environ)
        baseline = resolve_settings(against)
    except ValueError as exc:
        _fail(str(exc))

    comparison = compare_policy_strictness(subject, baseline)

    if fmt == "json":
        # config/baseline label the run; the verdict body is the shared serialiser
        # the in-app GET /metrics/policy/check uses, so the two can't drift.
        payload = {"config": source, "baseline": against, **comparison_to_dict(comparison)}
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        if not comparison.ok:
            sys.exit(1)
        return

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
