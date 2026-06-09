# Contributing to Project Foundry

Thanks for considering a contribution. Foundry is a governance layer for AI
coding agents, so the bar for changes is the same one the product enforces:
explicit, tested, auditable.

## Ground rules

1. **The safety rules are the product.** Changes that weaken a policy gate,
   an approval requirement, or an audit trail will not be merged, however
   convenient they are. If a rule is wrong, change the rule *explicitly* (with
   tests in both the Python engine and the Rego bundle) - don't route around it.
2. **No network in core tests.** Everything external (LLMs, Linear, GitHub,
   Temporal, OPA) lives behind a seam with a fake on the other side. `pytest`
   must pass offline with no API keys. If your change needs a new external
   call, add a transport seam and a fake.
3. **Both policy backends stay in lock-step.** Any change to
   `src/foundry/policy/engine.py` needs the mirror change in `foundry.rego`,
   and tests in both `tests/test_policy_engine.py` and `foundry_test.rego`.
4. **Artifacts are contracts.** Changes to `foundry.schemas` are API changes.
   Keep them strict (`extra="forbid"`), additive where possible, and update the
   consumers in the same PR.
5. **Webhook mappings are pinned by fixtures.** If a live Linear / GitHub /
   Jira / GitLab payload exposes a mapping bug, the fix starts with a redacted
   captured payload in `tests/fixtures/` and an assertion in the corresponding
   test module. This keeps mapping fixes contributor-friendly: no credentials
   needed to reproduce or verify.

## Getting started

```bash
python3.11 -m venv .venv && source .venv/bin/activate  # 3.11+ required
pip install -e ".[test]"
pytest
```

Optional checks:

```bash
opa test src/foundry/policy -v   # policy bundle (needs the OPA CLI)
ruff check src tests             # lint
```

## Pull requests

- One logical change per PR; small is reviewable.
- Add or update tests for every behaviour change - the suite is the spec.
- Explain *why* in the PR description, not just what.
- New configuration goes in `foundry.example.yaml` with a comment; secrets are
  environment variables, never YAML.

## Reporting security issues

Please do not open public issues for security vulnerabilities - see
[SECURITY.md](./SECURITY.md).
