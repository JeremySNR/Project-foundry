# AGENTS.md — fast orientation for AI agents working on this repo

> **Maintenance rule (non-negotiable):** any PR that adds, removes, or changes a
> feature MUST update this file in the same PR (and the README where user-facing).
> This document is the fast path for the next agent; if it drifts from the code,
> it is worse than useless. Treat it like a test: stale doc = failing build.
> Keep entries concise — describe each module's responsibilities, seams, and the
> invariants it touches. The code is the source of truth for specifics; do not
> enumerate every endpoint, metric cut, or helper here.

## What this project is

Foundry is a **governance control plane for AI coding agents**: a ticket goes in
(Linear / GitHub Issues / Jira), Foundry analyzes readiness, routes it to a repo,
classifies risk, produces a plan, gets a **human approval**, dispatches a coding
agent (Cursor, Claude Code, signed webhook, manual), then watches the PR/MR
(GitHub / GitLab) and re-dispatches the agent on CI failures or review requests —
policy-gated, capped, budgeted, and fully audited. It deliberately **stops at a
reviewed PR**: no auto-merge, no deploys, no migrations. The safety gates are the
product, not overhead. Canonical product statement: `VISION.md`.

**Looking for work, or about to start some?** The backlog lives in
[GitHub Issues](https://github.com/JeremySNR/Project-foundry/issues): bugs and
hardening are labelled `bug`, the strategic roadmap items `enhancement`. Assign
yourself (or comment) before starting, work one issue — or one clearly-scoped
slice of one — per PR, and split slices into sub-issues rather than new
top-level items. New top-level roadmap items need a human decision, not an
agent's.

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
| Ticket readiness / AC extraction | Regex + structure heuristics (default) **or** OpenAI structured output (`analyzer.provider: openai`). LLM failures degrade to the heuristic analyzer, recorded in the analysis `assumptions` — an OpenAI outage never fails intake. | `engines/analyzer.py`, `engines/openai_analyzer.py` |
| Work-type classification | Keyword-hit counting | `engines/analyzer.py` |
| Risk classification | Built-in keyword lists on ticket text + `fnmatch` globs on PR diff paths (default) **or** an LLM pass with cited evidence (`risk.provider: llm`). Both halves are operator-tunable without forking: the diff-stage path globs via `policy.sensitive_path_globs`, and now the ticket-text keywords via `risk.extra_sensitive_keywords` (issue #31, area name → extra keywords merged **on top of** the built-in floor via `merge_sensitive_keywords`; area names validated against `SENSITIVE_AREA_KEYS` at load). Strictly additive — extras can only flag *more* areas, never fewer, so they only escalate risk (invariant #1); no `SensitiveAreas` schema / `ApprovalRole` / Rego change (the keyword→area→level/roles mapping stays Python-side). The heuristics are a hard floor: the LLM may only escalate (add areas, raise the level), never downgrade — enforced in the classifier before the policy gate, so the policy engine/Rego are untouched. Evidence lands in the `RiskAssessment` artifact and `risk.escalated` event metadata. LLM failures degrade to the floor, recorded in the artifact. | `engines/risk.py`, `engines/llm_risk.py` |
| Repo routing | Tiered: explicit ticket association (conf 90) > delivery-memory priors (capped 89) > catalog IDF-token / keyword scoring (lexical-capped 89, stale-capped 65) > legacy YAML keywords. Lexical, not semantic. With `context.provider: code`, the synced file tree becomes a scored field and the bundle carries `RepoCodeFacts` (test layout, CODEOWNERS, manifests) + candidate files + inferred test commands; reasons cite concrete paths. Same confidence tiers/caps — code evidence never adds a tier. | `engines/enrichment.py`, `engines/code_context.py`, `catalog/`, `memory/priors.py` |
| Planning | Template rendering — steps are "Satisfy acceptance criterion: X" (default) **or** an LLM pass (`planner.provider: llm`) that turns the code-aware context (candidate files, `RepoCodeFacts`) into file-level steps, test locations, verify commands and `expected_files_or_areas`. The LLM is only consulted for runs the template planner would dispatch (ready + confident repo); identity/scope stay deterministic and the `agent_instructions` guardrail block is rendered by Foundry, never the model. LLM failures degrade to the template plan, recorded in the plan's `open_questions`. | `engines/planner.py`, `engines/llm_planner.py` |
| Delivery memory | Outcomes derived purely from the audit trail; Beta-smoothed routing priors (min 3 samples, >50% success); per-provider agent scorecards + scorecard *trends*; a scorecard-backed `recommend_provider` that turns the cards into one explainable provider pick (same guard rails: min-sample floor, >50%-merged gate, candidate allow-list); **learned dispatch has landed** (`agent.provider: auto`, issue #33) — the orchestrator now *acts* on `recommend_provider` at first dispatch (see the `agents/` and `orchestrator.py` rows), so scorecards are no longer reporting-only. | `memory/` |

The deterministic heuristics are the no-key/offline reference implementations and
the test baseline; they are intentionally conservative, not unfinished LLM calls.

## Module map (`src/foundry/`)

| Path | What it is |
| --- | --- |
| `schemas/` | Pydantic contracts for every artifact a run produces (`extra="forbid"` — changes here are API changes; update consumers in the same PR) |
| `engines/` | analyzer / enrichment / risk / planner (see table above) + `llm.py` structured-LLM seam + `llm_risk.py` (escalate-only LLM risk) + `llm_planner.py` (file-level LLM planner, degrades to template) + `decomposition.py` / `llm_decomposition.py` — the epic **producer** (`decompose_epic`, #35): splits an epic ticket into one child per repo (explicit *Repositories* section or ≥2 `known_repositories`; <2 repos ⇒ not an epic). The LLM decomposer recovers prose-only epics but keeps the heuristic as a non-overridable floor (only *adds* a split, grounds every repo against the ticket, degrades on failure) |
| `policy/engine.py` | Default-deny policy gate, ~10 hard rules (readiness, repo confidence, sensitive areas, retry caps, budget, role-checked approvals; **every autonomous action also requires ≥1 recorded human approval** via `approval_present` — the human-in-the-loop promise as a gate rule, #18; `auto_merge` / `production_deploy` denied unconditionally). Required approval roles = the **risk-derived** roles ∪ any **per-repo** roles (`policy.repo_required_roles`, #31), resolved by the orchestrator and stamped on `PolicyInput` so both backends read one field. Strictly additive (invariant #1). **Forbidden-path blocking and the escalate-only path/freeze/plan-scope rules live in `orchestrator.py`, not here, and have no Rego mirror** (so invariant #2 doesn't apply to them — see that row) |
| `policy/foundry.rego` | OPA backend, selectable via `policy.provider: opa` (`OpaPolicyEngine`); the Python `LocalPolicyEngine` is the default. **Must change in lock-step** (machine-verified over shared vectors — see invariant #2). The confidence threshold is read from input, not hardcoded, so both backends honour the configured value |
| `policy/library/` | Starter policy library (#31): committed, tested presets (`baseline` / `soc2` / `change-management` / `pci-dss`) a buyer copies into `foundry.yaml`. Built **only** from existing gated knobs — no new policy mechanism, no `engine.py` / `foundry.rego` change, no lock-step concern. Copy-to-adopt, never auto-applied (can't weaken a running gate — invariant #1); each preset is strict-or-stricter than the defaults. `library/__init__.py` is the pure offline loader plus `effective_policy_summary` (the read-only `explain` data — the whole gate a config resolves to) and `compare_policy_strictness` (the verification counterpart: a per-knob strictness diff answering "does my config meet this baseline's floor?", with typed machine fields, shared by `foundry-policy check --format json` and `GET /metrics/policy/check` so the two verdicts can't drift). `policy/cli.py` is the `foundry-policy` console entry (`presets` / `show` / `explain` / `check`) — decision-support only, never mutates a deployment's policy |
| `orchestrator.py` | The state machine; writes every decision/artifact/approval as content-hashed audit rows. `approve()` pre-validates approver roles against the run's required approvals and refuses *before* recording. **N-of-M approval matrix (#31):** accumulates *distinct* approvals, flipping the run to `APPROVED` only once `policy.min_approvals` (raised by per-repo `repo_min_approvals` via `max`) distinct humans sign off; a duplicate from the same person is refused; dispatch reads the union of granted roles. **Learned dispatch (#33):** under `agent.provider: auto` the first dispatch picks the provider via `recommend_provider`; a retry never re-routes. Both are lifecycle-only — no `PolicyInput` / `foundry.rego` / gate change (invariant #2 N/A); default 1 / single-provider = the historical path. Also home to the orchestrator-only, escalate-only, Rego-less rules: diff-aware forbidden-path blocking (sticky `BLOCKED`, with per-repo extras), per-path approval roles, change-freeze windows, and plan-scope drift — all strictly additive (invariant #1) |
| `drivers.py` | RunDriver seam: inline in-process (the real, production one) vs Temporal. `InlineDriver` owns optional epic auto-decomposition (`epics.auto_decompose`, **default off**, #35): when on, a multi-repo ticket fans out into independently-gated child runs and `start()` returns the parent (epic-root) run id, so callers' one-active-run-per-issue bookkeeping is unchanged. A ticket that doesn't decompose degrades to a single ordinary run, so the path is always safe to take |
| `workflows/` | Temporal driver (durable waits for approval/PR). Signal-driven; sequencing decisions are pure functions in `decisions.py` (offline-tested) and the workflow is a thin shell. Wait timeouts terminate cleanly; unknown decision verbs are dropped (never a silent stop); `intake_and_plan` is idempotent under retries; PR observation loops until the run leaves a PR-observable state. Each activity carries an explicit retry/timeout policy (`activity_options.py`); an activity that exhausts its budget triggers the `fail_run` **compensation activity** so a run never strands active (invariant #7). **CI-proven against a real `temporalio/auto-setup` server** (the `temporal` CI job, #37) as well as the in-memory time-skipping harness; the **inline driver remains the production path** |
| `agents/` | Provider abstraction: `manual`, fake, `cursor_cloud`, `cursor_via_linear`, `claude_code` (GitHub Actions `workflow_dispatch`), `webhook` (HMAC-signed). All go through one `create_job` path with a secret-leak scan (invariant #6) and expose `cancel_job` — a human `stop` / `reject` best-effort cancels the in-flight job so a stopped run stops spending (audited `AGENT_CANCELLED`). **Learned dispatch (`agent.provider: auto`, #33):** `build_provider_registry` (in `api/app.py`) builds every `auto_candidates` provider + the fallback into a name→provider registry; the orchestrator resolves each run's provider by the recorded `job.provider`, not a singleton. A single configured agent is byte-for-byte the old path |
| `connectors/` | Trackers: Linear, GitHub Issues, Jira. SCMs: GitHub, GitLab. Both fetch the changed-file list via an injected transport (`github_transport` / `gitlab_transport`) so file-based gates see the full diff; **no token ⇒ diff-blind, gates skipped**. Chat: a `RunNotifier` seam (`notify.py`) + `SlackNotifier` (`slack.py`) posts the interactive approval message (button wire-contract `SLACK_ACTION_PREFIX` / `SLACK_DECISIONS` shared with `api/slack.py`), an N-of-M partial-approval progress nudge (#31), and status updates — best-effort, **fail-closed: wired only when both `FOUNDRY_SLACK_BOT_TOKEN` and a channel are set**. `transport.py` is the HTTP seam (fakes in tests). Jira webhooks are unsigned, so `FOUNDRY_JIRA_WEBHOOK_SECRET` is an approver-level header credential (see SECURITY.md) |
| `catalog/` | `foundry-catalog sync` — GitHub org metadata sweep feeding the catalog enricher (stateful, budget-aware, resumable). `--code-facts` (implied by `context.provider: code`) adds per-repo code facts: file tree via the Git Trees API, CODEOWNERS, root manifests; derivation logic is pure functions in `catalog/code_facts.py` |
| `memory/` | Delivery memory derived from the audit trail: run outcomes, Beta-smoothed routing priors, per-provider agent scorecards (+ trends) and `recommend_provider`, plus the read-time **metrics** the API and `foundry-memory` CLI both serve. Two families — **delivery** (`delivery_metrics` and its `by-repo` / `by-work-type` / `*_trends` cuts: PRs shipped, merge rate, spend, `time_to_approval_seconds` / `time_to_merge_seconds` distributions, bucketed over time) and **failures** (`failure_queue` + `by-category` / `by-repo` / `by-work-type` / `*_trends`: recently blocked/failed runs, why, grouped and trended) — plus the live `fleet_status` snapshot and its three in-flight queues (`approval_queue`; `execution_queue` with per-run spend + cost SLA; `review_queue` with open-age + stale-since-push ages). Every metric has an offline `foundry-memory` twin that reads the DB and calls the **same** derivation as the API (honouring the same `dashboard.*_sla_seconds` knobs) so CLI and API verdicts can't drift — for an on-call/auditor with DB access but no running API |
| `api/` | FastAPI surface (routes in `app.py`, helpers in `api/*.py`). **Webhooks:** signed Linear/GitHub/GitLab/Jira intake + Slack interactivity (`api/slack.py`; Slack v0 signing + replay-age in `api/security.py`; approve/reject/stop buttons drive the same `_apply_decision` path, actor = Slack-signed user id; fail-closed on `FOUNDRY_SLACK_SIGNING_SECRET`). **Reads** (token- or OIDC-gated): runs, timeline, evidence, epics (`/runs/{id}/epic` + its cross-run `/epic/evidence` pack, #35), and the `/metrics/*` family (delivery, failures, fleet/approvals/executions/reviews, agents, policy, policy/check, integrity — all twinned by `foundry-memory`, see the `memory/` row). **Approvals:** `POST /runs/{id}/approval` (the human gate). **Auth** (`api/auth.py`, `oidc_login.py`, `sessions.py`, #34): static bearer token *or* OIDC JWT for the API; browser-side OIDC SSO login (authz-code + PKCE, signed session cookie) for the dashboard — the cookie authenticates reads but is **rejected on the approval endpoint** (CSRF-safe), so it never widens the human gate. Dashboard HTML is `api/dashboard.py` |
| `compliance/` | Compliance evidence packs: read-only packaging of a run's chain. `evidence.py` `build_evidence_pack` assembles one run's pack + `verify_integrity` (recomputed artifact content-hashes + audit-sequence continuity + the cross-event hash chain). `build_epic_evidence_pack` rolls a parent + all children into one cross-run export (#35). JSON, `?format=html`, or `?format=pdf` (optional `[pdf]` extra). Read-only — no audit write |
| `db/models.py` | SQLAlchemy: runs, artifacts, audit events, policy decisions, agent jobs, repo catalog, run outcomes, webhook deliveries (dedup). **Single-tenant** — no org/tenant columns. `foundry_runs.parent_run_id` is a nullable self-FK for epic decomposition (#35): a child run points at its parent epic, single-level (the orchestrator refuses to nest). `foundry_audit_events.content_hash` is a nullable cross-row chain hash (#36): each event commits to the previous event's hash for its run (assigned by the `db/base.py` flush hook alongside `sequence`); nullable so pre-chain rows read back cleanly |
| `epics.py` | Read side of the parent/child run model (#35): `compute_epic_rollup` — a pure function deriving an epic's status (`empty` / `in_progress` / `complete` / `partial` / `failed`) + per-bucket counts from its children's `RunStatus`, mirroring the lifecycle sets so it can't drift. `FoundryOrchestrator.list_epics()` is the query side feeding the `GET /epics` board. The producer is `orchestrator.intake_epic` — decompose the ticket, open the parent run, then one **independently-gated** child run per repo; a non-epic ticket degrades to a single run |
| `audit/` | SHA-256 content hashing for artifacts and events, plus `audit_event_chain_hash` — the linked-hash-chain step that ties each audit event to the previous one in its run's trail (assigned at flush time in `db/base.py`, recomputed for verification in `compliance/evidence.py` — one definition, two call sites, #36) |
| `config.py` | Layering: built-in defaults → YAML (`FOUNDRY_CONFIG`) → env vars. Behaviour in YAML (committed), secrets in env (never committed). Example: `foundry.example.yaml` |

Other locations: `tests/fixtures/` (webhook payloads — spec-derived from the
providers' webhook docs, replaceable by redacted live captures — pinning every
payload mapping), `migrations/` (Alembic — the **single** schema owner on Postgres, run by the
Docker entrypoint on startup; SQLite dev/test is bootstrapped in-process by
`db.base.init_schema`, which create_alls only on SQLite so it never strands a
later `alembic upgrade head`),
`examples/claude-code-runner.yml` (reference agent workflow), `scripts/demo.py`
(offline narrated end-to-end demo — the fastest way to see the whole loop).

## Hard invariants — do not route around these

1. **Never weaken a gate.** PRs that loosen a policy rule, approval requirement,
   or audit write don't merge. Change rules explicitly, with tests, or not at all.
2. **Python policy engine and `foundry.rego` change together**, with tests in
   `tests/test_policy_engine.py` and `foundry_test.rego`. Lock-step is
   machine-verified: shared vectors in `tests/data/policy_vectors.json` run
   through `LocalPolicyEngine` (`tests/test_policy_parity.py`) **and** through
   `opa eval` (`scripts/policy_parity.py`, in the OPA CI job) against the same
   expected decisions — add a vector when you add a rule, on both sides.
3. **No network in core tests.** `pytest` must pass offline with zero API keys.
   New external calls need a transport seam + a fake.
4. **The policy gate is default-deny**; an unrecognised action is refused.
5. **Approver roles live in committed YAML / config**, never in request payloads.
6. **Secrets never reach agent prompts** — job inputs pass the leak scan in
   `agents/provider.py`.
7. **Forbidden-path blocks are never retried.** Blocked stays blocked — and that
   must hold under concurrency too: every run state-transition reads the run row
   with `_require_run(..., lock=True)` (`SELECT … FOR UPDATE`, no-op on SQLite,
   real on Postgres) and re-verifies status under that lock before writing. A
   (re)dispatch that finds the run already terminal (e.g. a human `stop` won the
   race) records the launched job, cancels it, and never reverts the status. The
   `(run_id, sequence)` audit index is unique so a duplicate sequence fails loud.
8. New config goes in `foundry.example.yaml` with a comment; secrets are env vars.

## Dev commands

```bash
pip install -e ".[test]" && pytest          # full offline suite (~500 tests)
ruff check src tests                        # lint
opa test src/foundry/policy -v --ignore '*.yaml'  # Rego tests (needs opa CLI; ignore preset configs)
python scripts/demo.py                      # offline end-to-end demo
foundry-policy presets                      # browse the starter policy library
foundry-policy check --config foundry.yaml --against soc2  # verify your config meets a baseline (exits non-zero if weaker; add --format json for CI)
make dev                                    # uvicorn API on :8000, SQLite
docker compose up --build                   # API + Postgres (+ Temporal profile)
alembic upgrade head                        # migrations (Postgres)
```

Gated suites (skipped by default): Temporal (the time-skipping E2E needs the
fetchable test-server binary; the **real-server** E2E runs in the `temporal` CI
job — `docker compose --profile temporal up -d temporal`, then
`FOUNDRY_TEMPORAL_TEST_ADDRESS=localhost:7233 pytest tests/test_temporal_workflow.py`),
Postgres smoke, live E2E (`FOUNDRY_E2E=1` + real credentials — never in CI).

## Known limitations (so you don't rediscover them)

State what is *still* dumb or unfinished here — not what has landed (that's the
module map and git history). Keep these to genuine gaps the next agent would
otherwise re-discover or re-implement.

- **Context/routing is lexical** (token overlap), not semantic — no AST or
  embeddings. The `code` provider reads file trees / CODEOWNERS / manifests, but
  only after a `--code-facts` sync; `static` / `catalog` still read no code.
- **Plans are templated by default** (`planner.provider: llm` adds a file-level
  planner). The plan-vs-diff drift check consumes the planner's
  `expected_files_or_areas` (`policy.enforce_plan_scope`, escalate-only), but a
  *plan-aware gate* that reasons about whether the change actually satisfies the
  plan (beyond file scope) remains future work.
- **Risk-from-ticket is keyword matching** by default (risk-from-diff globs are
  precise); `risk.provider: llm` adds escalate-only judgment. Both heuristics are
  operator-extensible (`policy.sensitive_path_globs`, `risk.extra_sensitive_keywords`,
  #31). Dynamically-named risk *categories* beyond the seven fixed `SensitiveAreas`,
  and live user-loadable OPA bundles, remain open on #31.
- **Single-tenant DB — no `org_id` / row-level isolation, and no SCIM yet** (#34).
  (API auth accepts OIDC JWTs alongside the static bearer token; dashboard SSO
  login, federated logout, sliding sessions, and coarse per-client rate limiting
  on the webhook + API surfaces have landed — so the remaining #34 gaps are
  tenancy/`org_id` and SCIM.)
- **The Temporal driver is CI-proven against a real server (#37) but the inline
  driver remains the production path.**
- **One run targets one repo** by default. The parent/child epic model, rollup,
  cross-run evidence export, and the decomposition producer (incl. LLM-assisted)
  have landed (#35), but broader multi-repo orchestration maturity is still
  settling.
- **No Teams surface**; approvals are tracker comments, the Slack interactive
  message, or the REST endpoint.

A C#/.NET port of the core lives in unmerged PR #1 (`dotnet/`); the Python
implementation is canonical — genuine defects get fixed in both.
