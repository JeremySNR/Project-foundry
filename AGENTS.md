# AGENTS.md — fast orientation for AI agents working on this repo

> **Maintenance rule (non-negotiable):** any PR that adds, removes, or changes a
> feature MUST update this file in the same PR (and the README where user-facing).
> This document is the fast path for the next agent; if it drifts from the code,
> it is worse than useless. Treat it like a test: stale doc = failing build.

## What this project is

Foundry is a **governance control plane for AI coding agents**: a ticket goes in
(Linear / GitHub Issues / Jira), Foundry analyzes readiness, routes it to a repo,
classifies risk, produces a plan, gets a **human approval**, dispatches a coding
agent (Cursor, Claude Code, signed webhook, manual), then watches the PR/MR
(GitHub / GitLab) and re-dispatches the agent on CI failures or review requests —
policy-gated, capped, budgeted, and fully audited. It deliberately **stops at a
reviewed PR**: no auto-merge, no deploys, no migrations. The safety gates are the
product, not overhead. Canonical product statement: `VISION.md`.

**Looking for work, or about to start some?** `ROADMAP.md` is the prioritized
backlog with per-item status tracking — claim an item there and keep its status
current in the same PR as the work.

## The run lifecycle (what the orchestrator does)

```
webhook intake → analyze (ready? AC present?) → enrich (which repo?) →
classify risk → plan → POLICY GATE → human approval → dispatch agent →
PR opens → diff-aware risk re-check on every push → CI fail / changes
requested → policy-gated retry (capped, budgeted) → merged/closed →
outcome recorded into delivery memory
```

Terminal states: merged, blocked (never retried), rejected, failed,
needs-clarification (re-triggerable — one *active* run per issue, not one ever).

## Honest map of the intelligence layers

Know what is smart and what is deliberately dumb before you "improve" anything:

| Layer | Mechanism | Where |
| --- | --- | --- |
| Ticket readiness / AC extraction | Regex + structure heuristics (default) **or** OpenAI structured output (`analyzer.provider: openai`) | `engines/analyzer.py`, `engines/openai_analyzer.py` |
| Work-type classification | Keyword-hit counting | `engines/analyzer.py` |
| Risk classification | Hardcoded keyword lists on ticket text + `fnmatch` globs on PR diff paths (default) **or** an LLM pass with cited evidence (`risk.provider: llm`). The heuristics are a hard floor: the LLM may only escalate (add areas, raise the level), never downgrade — enforced in the classifier before the policy gate, so the policy engine/Rego are untouched. Evidence lands in the `RiskAssessment` artifact and `risk.escalated` event metadata. LLM failures degrade to the floor, recorded in the artifact. | `engines/risk.py`, `engines/llm_risk.py` |
| Repo routing | Tiered: explicit ticket association (conf 90) > delivery-memory priors (capped 89) > catalog IDF-token scoring (stale-capped 65) > legacy YAML keywords. Lexical, not semantic. With `context.provider: code`, the synced file tree becomes a scored field and the bundle carries `RepoCodeFacts` (test layout, CODEOWNERS, manifests) + candidate files + inferred test commands; reasons cite concrete paths. Same confidence tiers/caps — code evidence never adds a tier. | `engines/enrichment.py`, `engines/code_context.py`, `catalog/`, `memory/priors.py` |
| Planning | Template rendering — steps are "Satisfy acceptance criterion: X". No file-level analysis. | `engines/planner.py` |
| Delivery memory | Outcomes derived purely from the audit trail; Beta-smoothed routing priors (min 3 samples, >50% success); ROI metrics API | `memory/` |

The deterministic heuristics are the no-key/offline reference implementations and
the test baseline; they are intentionally conservative, not unfinished LLM calls.

## Module map (`src/foundry/`)

| Path | What it is |
| --- | --- |
| `schemas/` | Pydantic contracts for every artifact a run produces (`extra="forbid"` — changes here are API changes; update consumers in the same PR) |
| `engines/` | analyzer / enrichment / risk / planner (see table above) + `llm.py` structured-LLM seam + `llm_risk.py` escalate-only LLM risk classifiers |
| `policy/engine.py` | Default-deny policy gate, ~10 hard rules (readiness, repo confidence, forbidden globs, sensitive areas, retry caps, budget, role-checked approvals; `auto_merge`/`production_deploy` denied unconditionally) |
| `policy/foundry.rego` | OPA mirror of the Python engine — **must change in lock-step**, tests on both sides |
| `orchestrator.py` | The state machine; writes every decision/artifact/approval as content-hashed audit rows |
| `drivers.py` | RunDriver seam: inline in-process (the real one) vs Temporal |
| `workflows/` | Temporal version (durable waits for approval/PR). Exists, signal-driven, **not yet battle-tested against a real server** |
| `agents/` | Provider abstraction: `manual`, fake, `cursor_cloud`, `cursor_via_linear`, `claude_code` (GitHub Actions `workflow_dispatch`), `webhook` (HMAC-signed). All go through one `create_job` path with a secret-leak scan |
| `connectors/` | Trackers: Linear, GitHub Issues, Jira. SCMs: GitHub, GitLab. `transport.py` is the HTTP seam (fakes in tests) |
| `catalog/` | `foundry-catalog sync` — GitHub org metadata sweep feeding the catalog enricher (stateful, budget-aware, resumable). `--code-facts` (implied by `context.provider: code`) adds per-repo code facts: file tree via the Git Trees API, CODEOWNERS, root manifests; derivation logic is pure functions in `catalog/code_facts.py` |
| `memory/` | Run outcomes, routing priors, delivery metrics; `foundry-memory backfill / show-priors` |
| `api/` | FastAPI: signed webhooks (Linear/GitHub/Jira/GitLab), REST approvals, run timeline, `GET /metrics/delivery`, and a zero-build HTML dashboard at `/dashboard`. Everything token-gated, **fail-closed** (no `FOUNDRY_API_TOKEN` = endpoints disabled) |
| `db/models.py` | SQLAlchemy: runs, artifacts, audit events, policy decisions, agent jobs, repo catalog, run outcomes. **Single-tenant** — no org/tenant columns |
| `audit/` | SHA-256 content hashing for artifacts and events |
| `config.py` | Layering: built-in defaults → YAML (`FOUNDRY_CONFIG`) → env vars. Behaviour in YAML (committed), secrets in env (never committed). Example: `foundry.example.yaml` |

Other locations: `tests/fixtures/` (recorded webhook payloads pinning every payload
mapping), `migrations/` (Alembic, Postgres prod; SQLite dev uses `create_all`),
`examples/claude-code-runner.yml` (reference agent workflow), `scripts/demo.py`
(offline narrated end-to-end demo — the fastest way to see the whole loop).

## Hard invariants — do not route around these

1. **Never weaken a gate.** PRs that loosen a policy rule, approval requirement,
   or audit write don't merge. Change rules explicitly, with tests, or not at all.
2. **Python policy engine and `foundry.rego` change together**, with tests in
   `tests/test_policy_engine.py` and `foundry_test.rego`.
3. **No network in core tests.** `pytest` must pass offline with zero API keys.
   New external calls need a transport seam + a fake.
4. **The policy gate is default-deny**; an unrecognised action is refused.
5. **Approver roles live in committed YAML / config**, never in request payloads.
6. **Secrets never reach agent prompts** — job inputs pass the leak scan in
   `agents/provider.py`.
7. **Forbidden-path blocks are never retried.** Blocked stays blocked.
8. New config goes in `foundry.example.yaml` with a comment; secrets are env vars.

## Dev commands

```bash
pip install -e ".[test]" && pytest          # full offline suite (~320 tests)
ruff check src tests                        # lint
opa test src/foundry/policy -v              # Rego tests (needs opa CLI)
python scripts/demo.py                      # offline end-to-end demo
make dev                                    # uvicorn API on :8000, SQLite
docker compose up --build                   # API + Postgres (+ Temporal profile)
alembic upgrade head                        # migrations (Postgres)
```

Gated suites (skipped by default): Temporal (needs a server), Postgres smoke,
live E2E (`FOUNDRY_E2E=1` + real credentials — never in CI).

## Known limitations (so you don't rediscover them)

- Context/routing is lexical (token overlap), not semantic — no AST or embeddings.
  The `code` provider reads file trees/CODEOWNERS/manifests, but `static`/`catalog`
  still read no code, and code facts only exist after a `--code-facts` sync.
- Plans are templated; the bundle now carries candidate files and code facts, but
  the planner does not yet use them for file-level steps (roadmap #3).
- Risk-from-ticket is keyword matching by default (risk-from-diff globs are
  precise); `risk.provider: llm` adds judgment + cited evidence, escalate-only.
- Single-tenant DB; approvers are static config (no SSO/SCIM); no rate limiting.
- No DB-level idempotency on intake — simultaneous webhooks for one issue can race.
- Temporal driver unproven against a live server; inline driver is production path.
- One run targets one repo; no cross-repo or epic decomposition.
- No Slack/Teams surface; approvals are tracker comments or the REST endpoint.

A C#/.NET port of the core lives in unmerged PR #1 (`dotnet/`); the Python
implementation is canonical — genuine defects get fixed in both.
