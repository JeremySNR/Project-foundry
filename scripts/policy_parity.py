#!/usr/bin/env python3
"""Assert the Rego policy bundle matches the shared decision vectors.

This is the OPA half of the policy lock-step check (AGENTS.md invariant #2). It
runs every vector in ``tests/data/policy_vectors.json`` through the Rego bundle
with ``opa eval`` and asserts the decision equals the vector's ``expected``.
The Python half - ``tests/test_policy_parity.py`` - runs the *same* vectors
through ``LocalPolicyEngine`` against the *same* ``expected``, so both backends
are anchored to one source of truth and cannot drift silently.

Run from the OPA CI job (it has the ``opa`` binary). Uses only the standard
library, so it needs no editable install of the package.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGO_FILE = ROOT / "src" / "foundry" / "policy" / "foundry.rego"
VECTORS_FILE = ROOT / "tests" / "data" / "policy_vectors.json"
QUERY = "data.foundry.ticket_to_pr.decision"


def opa_decision(opa_input: dict) -> dict:
    """Evaluate the decision document for one input via ``opa eval``."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(opa_input, handle)
        input_path = handle.name
    try:
        proc = subprocess.run(
            ["opa", "eval", "--format", "json", "--data", str(REGO_FILE),
             "--input", input_path, QUERY],
            capture_output=True,
            text=True,
        )
    finally:
        Path(input_path).unlink(missing_ok=True)

    if proc.returncode != 0:
        raise SystemExit(f"opa eval failed (exit {proc.returncode}):\n{proc.stderr}")

    payload = json.loads(proc.stdout)
    try:
        return payload["result"][0]["expressions"][0]["value"]
    except (KeyError, IndexError) as exc:  # pragma: no cover - opa output shape
        raise SystemExit(f"unexpected opa output: {proc.stdout}") from exc


def _mismatches(decision: dict, expected: dict) -> list[str]:
    problems: list[str] = []
    if decision.get("allow") != expected["allow"]:
        problems.append(
            f"allow: rego={decision.get('allow')!r} expected={expected['allow']!r}"
        )
    if decision.get("allowed_agent_mode") != expected["allowed_agent_mode"]:
        problems.append(
            f"allowed_agent_mode: rego={decision.get('allowed_agent_mode')!r} "
            f"expected={expected['allowed_agent_mode']!r}"
        )
    # Sets: Rego materialises sorted, Python preserves insertion order.
    if set(decision.get("reasons", [])) != set(expected["reasons"]):
        problems.append(
            f"reasons: rego={sorted(decision.get('reasons', []))} "
            f"expected={sorted(expected['reasons'])}"
        )
    if set(decision.get("required_approvals", [])) != set(expected["required_approvals"]):
        problems.append(
            f"required_approvals: rego={sorted(decision.get('required_approvals', []))} "
            f"expected={sorted(expected['required_approvals'])}"
        )
    return problems


def main() -> int:
    vectors = json.loads(VECTORS_FILE.read_text())["vectors"]
    failures: list[str] = []
    for vector in vectors:
        opa_input = dict(vector["input"])
        threshold = vector.get("config", {}).get("repo_confidence_threshold")
        if threshold is not None:
            opa_input["repo_confidence_threshold"] = threshold
        decision = opa_decision(opa_input)
        problems = _mismatches(decision, vector["expected"])
        if problems:
            failures.append(
                f"MISMATCH {vector['name']}:\n  " + "\n  ".join(problems)
            )

    if failures:
        print("\n".join(failures))
        print(f"\n{len(failures)} of {len(vectors)} policy vectors diverged "
              "between Rego and the shared expected decisions.")
        return 1

    print(f"policy parity OK: {len(vectors)} vectors match between the Rego "
          "bundle and the shared expected decisions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
