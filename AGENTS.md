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
| Risk classification | Hardcoded keyword lists on ticket text + `fnmatch` globs on PR diff paths (default) **or** an LLM pass with cited evidence (`risk.provider: llm`). The heuristics are a hard floor: the LLM may only escalate (add areas, raise the level), never downgrade — enforced in the classifier before the policy gate, so the policy engine/Rego are untouched. Evidence lands in the `RiskAssessment` artifact and `risk.escalated` event metadata. LLM failures degrade to the floor, recorded in the artifact. | `engines/risk.py`, `engines/llm_risk.py` |
| Repo routing | Tiered: explicit ticket association (conf 90) > delivery-memory priors (capped 89) > catalog IDF-token / keyword scoring (lexical-capped 89, stale-capped 65) > legacy YAML keywords. Lexical, not semantic. With `context.provider: code`, the synced file tree becomes a scored field and the bundle carries `RepoCodeFacts` (test layout, CODEOWNERS, manifests) + candidate files + inferred test commands; reasons cite concrete paths. Same confidence tiers/caps — code evidence never adds a tier. | `engines/enrichment.py`, `engines/code_context.py`, `catalog/`, `memory/priors.py` |
| Planning | Template rendering — steps are "Satisfy acceptance criterion: X" (default) **or** an LLM pass (`planner.provider: llm`) that turns the code-aware context (candidate files, `RepoCodeFacts`) into file-level steps, test locations, verify commands and `expected_files_or_areas`. The LLM is only consulted for runs the template planner would dispatch (ready + confident repo); identity/scope stay deterministic and the `agent_instructions` guardrail block is rendered by Foundry, never the model. LLM failures degrade to the template plan, recorded in the plan's `open_questions`. | `engines/planner.py`, `engines/llm_planner.py` |
| Delivery memory | Outcomes derived purely from the audit trail; Beta-smoothed routing priors (min 3 samples, >50% success); per-provider agent scorecards (which agent ships, by work type/repo); a scorecard-backed `recommend_provider` that turns the cards into one explainable provider pick for a work-type/repo (same priors guard rails: min-sample floor, >50%-merged gate, candidate allow-list) — both **reporting/decision-support only, auto-dispatch (`agent.provider: auto`) is a future gated change**; ROI metrics API | `memory/` |

The deterministic heuristics are the no-key/offline reference implementations and
the test baseline; they are intentionally conservative, not unfinished LLM calls.

## Module map (`src/foundry/`)

| Path | What it is |
| --- | --- |
| `schemas/` | Pydantic contracts for every artifact a run produces (`extra="forbid"` — changes here are API changes; update consumers in the same PR) |
| `engines/` | analyzer / enrichment / risk / planner (see table above) + `llm.py` structured-LLM seam + `llm_risk.py` escalate-only LLM risk classifiers + `llm_planner.py` file-level LLM planner (degrades to the template planner) + `decomposition.py` — the deterministic epic **producer** (`decompose_epic`, #35): splits an epic ticket into one per-repo child ticket via an explicit *Repositories* section (`repo: scope` bullets, checkbox-tolerant) or, failing that, ≥2 `known_repositories`; carries the epic's acceptance criteria into each child and scopes each to one repo. <2 distinct repos ⇒ not an epic. The orchestrator depends on the `EpicDecomposer` protocol (`HeuristicDecomposer` wraps `decompose_epic`, the default) + `llm_decomposition.py` — the LLM-assisted decomposer (`decomposition.provider: llm`) that recovers epics described only in *prose* (neither heuristic shape present): it keeps the deterministic decomposer as a **non-overridable floor** (consulted only when the floor declines, so it can only *add* a split the floor missed, never remove/re-scope one), **grounds** every model-proposed repo against the ticket text/`known_repositories` (no invented repos), needs ≥2 grounded repos or it degrades to the floor, and degrades on any LLM failure — each child still independently gated, so no gate weakened |
| `policy/engine.py` | Default-deny policy gate, ~10 hard rules (readiness, repo confidence, sensitive areas, retry caps, budget, role-checked approvals; **every autonomous action also requires ≥1 recorded human approval** via the role-agnostic `approval_present` input — the human-in-the-loop promise as a gate rule, not just an orchestration check, issue #18; `auto_merge`/`production_deploy` denied unconditionally). The budget cap binds on **every** spending action (first `start_agent` + each `retry_agent`), comparing *projected* spend — recorded cost plus the next dispatch's `estimated_cost_per_dispatch` proxy for providers that don't report `cost_usd`. **Forbidden-path blocking is *not* here** — it lives in `orchestrator._forbidden_violations()` (diff-aware, sticky `BLOCKED`) and has no Rego mirror, so invariant #2 doesn't apply to it. Forbidden globs are global (`policy.forbidden_globs`) **plus** optional per-repo extras (`policy.repo_forbidden_globs`, keyed on the run's routed repo, issue #35) merged by `_forbidden_globs_for(repo)` — strictly additive, so per-repo scoping only ever makes the block stricter, never weakens the global floor (invariant #1). The same merged list seeds the agent's `do_not_modify` constraints at dispatch |
| `policy/foundry.rego` | OPA backend, selectable via `policy.provider: opa` (`OpaPolicyEngine`); the Python `LocalPolicyEngine` is the default. **Must change in lock-step** (machine-verified over shared vectors — see invariant #2). The confidence threshold is read from input, not hardcoded, so both backends honour the configured value |
| `orchestrator.py` | The state machine; writes every decision/artifact/approval as content-hashed audit rows. `approve()` pre-validates the approver's roles against the run's required approvals (derived from risk) and refuses *before* recording, so a void approval can't land on the trail and be blocked only at dispatch |
| `drivers.py` | RunDriver seam: inline in-process (the real one) vs Temporal. The `InlineDriver` owns the optional epic auto-decomposition (`auto_decompose_epics`, config `epics.auto_decompose`, **default off**, issue #35): when on, `start()` routes intake through `orchestrator.intake_epic` instead of `intake_and_plan`, so a multi-repo ticket arriving on the webhook/trigger path fans out into independently-gated child runs; it returns the parent (epic-root) run id, so the webhook callers' one-active-run-per-issue bookkeeping is unchanged. A ticket that doesn't decompose degrades to a single ordinary run, so the path is always safe to take |
| `workflows/` | Temporal version (durable waits for approval/PR). Signal-driven; the sequencing decisions are pure functions in `decisions.py` (offline-tested) and the workflow is a thin shell. Wait timeouts terminate cleanly (approval window → `BLOCKED`, PR window → `EXECUTION_FAILED`, both audited via `expire_pending_approval` / `expire_pending_pr`); unknown decision verbs are dropped in the signal handler (never a silent stop); `intake_and_plan` is idempotent under activity retries; PR observation loops on every push until the run leaves a PR-observable state. Each activity carries an **explicit per-activity retry/timeout policy** (`activity_options.py`, pure stdlib so it unit-tests offline): deterministic failures (`ValueError`/`ValidationError`/`OrchestratorError`) are classified non-retryable (fail fast), idempotent steps (intake, `record_pr`) retry patiently, the heaviest step (intake) gets the longest budget — `workflow.py` turns each spec into the Temporal `RetryPolicy`/timeout. An activity that **exhausts its retry budget** (raising `ActivityError`) is caught by the workflow, which runs the `fail_run` **compensation activity** (`orchestrator.fail_run` → any still-active run to `EXECUTION_FAILED`, audited `agent.failed`/`workflow_irrecoverable_error`, in-flight job cancelled, idempotent and terminal-safe per invariant #7) and then re-raises so the workflow still surfaces as Failed — the run never strands active forever. **Still not battle-tested against a real server** (the E2E `test_temporal_workflow.py` runs only where the time-skipping test-server binary is fetchable — CI, not offline sandboxes) |
| `agents/` | Provider abstraction: `manual`, fake, `cursor_cloud`, `cursor_via_linear`, `claude_code` (GitHub Actions `workflow_dispatch`), `webhook` (HMAC-signed). All go through one `create_job` path with a secret-leak scan, and expose `cancel_job` — a human `stop`/`reject` best-effort cancels the in-flight job (Cursor cancel API; no-op for `manual`/`webhook`/`claude_code`) so a stopped run stops spending, recorded as an `AGENT_CANCELLED` audit event |
| `connectors/` | Trackers: Linear, GitHub Issues, Jira. SCMs: GitHub, GitLab. Both fetch the changed-file list via an injected transport (`github_transport` / `gitlab_transport`, GitLab paging `/diffs`) so file-based gates see the full diff; **no token ⇒ diff-blind, gates skipped**. Chat notifications: `RunNotifier` seam (`connectors/notify.py`) with a `SlackNotifier` (`connectors/slack.py`, `slack_transport` → `chat.postMessage`) that posts the interactive approval message (buttons whose `action_id`/`value` the inbound `api/slack.py` parser consumes — the wire contract `SLACK_ACTION_PREFIX`/`SLACK_DECISIONS` lives in `connectors/slack.py`, re-exported by `api/slack.py`) and status updates (parked/blocked/PR open/merged). Best-effort like the tracker write-back; **fail-closed: wired only when both `FOUNDRY_SLACK_BOT_TOKEN` and a channel are set**. `transport.py` is the HTTP seam (fakes in tests). Jira webhooks have no body signature, so the shared `FOUNDRY_JIRA_WEBHOOK_SECRET` is an approver-level credential (the actor identity comes from the payload): it is read from the `X-Foundry-Webhook-Token` header only, with `?token=` query delivery off unless `tracker.jira_allow_query_token: true` (see SECURITY.md) |
| `catalog/` | `foundry-catalog sync` — GitHub org metadata sweep feeding the catalog enricher (stateful, budget-aware, resumable). `--code-facts` (implied by `context.provider: code`) adds per-repo code facts: file tree via the Git Trees API, CODEOWNERS, root manifests; derivation logic is pure functions in `catalog/code_facts.py` |
| `memory/` | Run outcomes, routing priors, agent scorecards, scorecard-backed provider recommendation (`scorecards.recommend_provider`), delivery metrics; `foundry-memory backfill / show-priors / show-scorecards / recommend-agent` |
| `api/` | FastAPI: signed webhooks (Linear/GitHub/Jira/GitLab), Slack interactivity approvals (`POST /webhooks/slack` — `api/slack.py`; Slack v0 request-signing + replay-age in `api/security.py`; approve/reject/stop buttons drive the same `_apply_decision` path; actor = Slack-signed `user.id`, approvers keyed by Slack user id; fail-closed on `FOUNDRY_SLACK_SIGNING_SECRET`), REST approvals, run timeline, `GET /runs/{id}/epic` (epic view for the parent/child run model #35 — resolves the epic root so it works on a child too, then returns the root run, its children, and the `compute_epic_rollup` summary; token-gated like the timeline), `GET /runs/{id}/epic/evidence` (the cross-run compliance export for that epic — the parent + every child as one evidence pack with the rollup and aggregate integrity/coverage; resolves the root like the epic view, JSON or `?format=html`), `GET /metrics/delivery`, `GET /metrics/delivery/trends` (the same aggregates bucketed by `day`/`week` over time), `GET /metrics/agents` (per-provider scorecards), `GET /metrics/agents/recommendation` (the scorecards turned into one explainable provider pick for a `work_type`/`repo` — decision-support only, nothing dispatches), `GET /metrics/fleet` (a live, no-window snapshot of every run's *current* state — runs in flight, approval-queue depth, agents running, PRs open, spend committed by in-flight runs — read from `FoundryRun`, distinct from the finished-run-only delivery aggregates; in `memory/metrics.py`), `GET /epics` (every epic root — a run other runs point at via `parent_run_id` — with the `compute_epic_rollup` status and its child runs; the dashboard epic board reads it, ordinary single-repo runs are omitted; same `run`/`rollup`/`children` shape as `GET /runs/{id}/epic`), `GET /runs/{id}/evidence` (single-run compliance evidence pack, JSON or `?format=html`), `GET /evidence` (org-wide evidence archive over a date range — every run in the window as the same per-run pack, plus a rollup of aggregate integrity, status breakdown, and per-control coverage; bound with ISO `from`/`to` — `from` inclusive, `to` exclusive, a date-only `to` covers the whole day — or `days`, default last 90; JSON or `?format=html`), and a zero-build HTML dashboard at `/dashboard` (a live fleet strip, run board with an approval-queue filter, the metrics strip, the delivery-trend table, an epic board rolling up multi-repo runs from `GET /epics`, and the per-run timeline). Everything token-gated, **fail-closed** (no `FOUNDRY_API_TOKEN` = endpoints disabled). Replayed/redelivered webhooks are dropped by a **durable, bounded dedup** table (`api/dedup.py`, keyed on `(provider, delivery_id)`, TTL-pruned); an optional fail-closed replay-age check rejects deliveries older than `webhook.replay_max_age_seconds` for providers that carry a timestamp (Linear's `webhookTimestamp`). Coarse per-client rate limiting (`api/ratelimit.py`) fronts the webhook and API surfaces via an ASGI middleware — fixed-window, two independent buckets (`webhook`/`api`), on by default, configurable under `rate_limit:`; **per-process** (in-memory), keyed by the direct peer address |
| `compliance/` | Compliance evidence packs: read-only packaging of a run's chain. `evidence.py` `build_evidence_pack` assembles one run's pack + `verify_integrity` (recomputed artifact content-hashes + audit-sequence continuity — *not* a cross-row linked chain); `build_evidence_archive` rolls every run in a `created_at` date range (`since` inclusive, `until` exclusive, either bound optional) into one org-wide export with aggregate integrity / status / per-control coverage; `build_epic_evidence_pack` is the cross-run cut of the same rollup (#35) — an epic's full chain as one export: the parent run plus every child pack, the `compute_epic_rollup` status, explicit root/child linkage, and the same aggregate summary (shared `_summarise_packs` helper); `render_evidence_html` / `render_archive_html` / `render_epic_evidence_html` are the zero-build HTML renderers. `controls.py` holds the control→evidence-section mappings (config, overridable via `compliance.control_mappings`; section names validated at load) |
| `db/models.py` | SQLAlchemy: runs, artifacts, audit events, policy decisions, agent jobs, repo catalog, run outcomes, webhook deliveries (dedup). **Single-tenant** — no org/tenant columns. `foundry_runs.parent_run_id` is a nullable self-FK for epic decomposition (#35): a child run points at its parent epic, single-level (the orchestrator refuses to nest) |
| `epics.py` | Read side of the parent/child run model (#35): `compute_epic_rollup` — a pure function deriving an epic's status (`empty` / `in_progress` / `complete` / `partial` / `failed`) and per-bucket counts from its children's `RunStatus` values, mirroring the `ACTIVE_RUN_STATUSES` / terminal sets so it can't drift from the lifecycle. `FoundryOrchestrator.list_epics()` is the matching query side — the epic roots (runs with ≥1 child) that feed the `GET /epics` dashboard board. `EpicIntakeResult` is the producer's return shape. The producer itself is `orchestrator.intake_epic(ticket, ...)` (#35): it `decompose_epic`s the ticket, opens the parent (epic root) run for the epic ticket, then one **independently-gated** child run per repo via `intake_and_plan(..., parent_run_id=...)` — each child analysed/risk-classified/planned/gated and parked for its own approval, no gate weakened. A non-epic ticket degrades to a single ordinary run with no children. The producer is now also reachable from the webhook/trigger auto-path behind a default-off flag (`epics.auto_decompose`, wired through `InlineDriver.auto_decompose_epics`); LLM-assisted decomposition is the remaining future slice |
| `audit/` | SHA-256 content hashing for artifacts and events |
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
- Plans are templated by default; `planner.provider: llm` adds a file-level
  planner that consumes the code-aware context (candidate files, code facts).
  It populates `expected_files_or_areas`, but the plan-vs-diff gate that would
  consume it is not built yet.
- Risk-from-ticket is keyword matching by default (risk-from-diff globs are
  precise); `risk.provider: llm` adds judgment + cited evidence, escalate-only.
- Single-tenant DB; approvers are static config (no SSO/SCIM); no rate limiting.
- Temporal driver unproven against a live server; inline driver is production path.
  The known workflow holes (wait-timeout handling, decision-verb validation,
  intake idempotency, single-shot PR observation) are fixed and CI-tested under
  the time-skipping environment, activity retry/timeout policies are now
  explicit per-activity (`activity_options.py`), and a compensation activity now
  auto-fails a run (`fail_run`) when an activity exhausts its retries so it can't
  strand active; durability against a real Temporal server (a CI job on the
  docker-compose Temporal profile) is still the open work in #37.
- One run targets one repo. The parent/child run model + epic rollup (#35) has
  landed (`epics.py`, `foundry_runs.parent_run_id`, `GET /runs/{id}/epic`), and
  the epic's full chain now exports as one cross-run evidence pack
  (`build_epic_evidence_pack`, `GET /runs/{id}/epic/evidence`), and the dashboard
  surfaces epics as a rolled-up board (`GET /epics`), and the *producer* —
  decomposing an epic ticket into independently-gated child plans — has landed
  (`engines/decomposition.py`, `orchestrator.intake_epic`), and the webhook/trigger
  intake path can now opt into running it automatically (`epics.auto_decompose`,
  default off, wired through `InlineDriver`), and LLM-assisted decomposition has
  landed (`engines/llm_decomposition.py`, `decomposition.provider: llm`) — it
  recovers prose-described epics over the deterministic floor (additive,
  grounded, degrade-to-floor), so the producer is no longer deterministic-only.
  Path-scoped policy has a first slice: per-repo forbidden-path
  globs (`policy.repo_forbidden_globs`) make the sticky forbidden-path block
  monorepo-aware (additive, no Rego); per-path *approval* rules remain open.
- No Slack/Teams surface; approvals are tracker comments or the REST endpoint.

A C#/.NET port of the core lives in unmerged PR #1 (`dotnet/`); the Python
implementation is canonical — genuine defects get fixed in both.
