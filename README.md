# Project Foundry

**Foundry turns a Linear ticket into a reviewed pull request, safely, by letting an approved AI agent do the work under supervision.**

*Raw tickets go in. Reviewed pull requests come out. Nothing unsafe makes it through the forge.*

That's the whole pitch. Foundry is not another coding AI. It is the thing that sits *above* your coding AI and decides whether a piece of work is actually ready, what context it needs, whether it's safe to hand to an agent, who has to approve it, and what happened afterwards. The agent (Cursor, Claude, OpenAI, whatever) is the muscle. Foundry is the brain and the seatbelt.

The quick version: it's a bit like Terraform, but for shipping code with AI agents. You describe the intent, Foundry produces a plan, a human approves it, and only then does anything actually happen. Plan, approve, apply.

The formal product statement lives in [`VISION.md`](./VISION.md). This README is the practical, slightly more caffeinated version.

## See it run (60 seconds, zero setup)

```bash
git clone https://github.com/JeremySNR/Project-foundry && cd Project-foundry
python -m venv .venv && source .venv/bin/activate && pip install -e .
python scripts/demo.py
```

No credentials, no network, no Docker. The demo drives the real production code path - orchestrator, policy engine, audit trail - through the whole story: a vague ticket gets bounced with drafted acceptance criteria, the improved ticket gets planned and gated, a human approves, the agent's PR fails CI and gets fixed by a governed retry, the PR merges, and a second run gets hard-blocked for touching `migrations/`. It ends with the receipts: the full audit trail and every policy decision. (`--slow` paces it for screen recording.)

## So what is it, really?

Picture the journey of a ticket today: someone writes "add customer favourites", an engineer reads it, fills in the gaps in their head, maybe asks an AI to write it, eyeballs the result, opens a PR. Foundry makes every one of those invisible steps explicit and governed:

```
Linear ticket
   -> Foundry reads it and asks: is this even clear enough to build?
   -> gathers context (which repo, which files, what tests)
   -> classifies risk (does this touch auth? payments? customer data?)
   -> writes a delivery plan a human can actually sign off on
   -> a human approves (this is a real step, not an afterthought)
   -> Foundry hands the approved plan to a coding agent
   -> the agent opens a PR
   -> CI, CodeRabbit and humans review it
   -> if CI fails or changes are requested, Foundry re-dispatches the agent
      with the failure context (policy-gated, capped, audited)
   -> Linear gets updated with status, summary and next action
```

And it deliberately **stops at a reviewed PR**. No auto-merge, no auto-deploy to production, no autonomous database migrations, no touching auth or payments without a human saying yes. The brakes are the product, not a missing feature.

The loop is also restartable where it should be: a ticket parked for clarification (or rejected, blocked, failed) can be re-triggered after it's improved - one *active* run per issue, not one run per issue forever.

## What actually exists today

The whole governed loop is built and tested, with swappable parts at every layer so no single vendor or piece of infra can hold you hostage. Nothing here needs the network or a paid API key to run the test suite, because every external thing hides behind a seam with a fake on the other side.

| Piece | What it does |
| --- | --- |
| `foundry.schemas` | The contracts for everything a run produces: the ticket snapshot, analysis, context, risk, plan, PR state, agent job. Pydantic, strict, validated. |
| `foundry.engines` | The intelligence. Deterministic heuristics by default; `OpenAITicketAnalyzer` (GPT-5.5) when you want a real brain at the gate. It judges readiness and missing info, it does not write code (that's the agent's job). |
| `foundry.policy` | The hard rules, as actual rules and not vibes. A pure-Python engine plus a matching OPA/Rego bundle. This is what blocks unsafe or unclear work. |
| `foundry.orchestrator` | The state machine that runs one ticket through the whole loop and writes down every decision. |
| `foundry.drivers` | One seam for *how* a run executes: inline in-process today, durable Temporal later, same interface. |
| `foundry.workflows` | The Temporal version of the loop: crash-proof, retries, and it'll happily wait days for an approval. |
| `foundry.agents` | The coding-agent abstraction. Foundry doesn't mind which blaster you bring: `manual`, a test fake, **Cursor** two ways, **Claude Code** via GitHub Actions, or *any* agent behind a signed webhook (see below). |
| `foundry.connectors` | Adapters for the tools Foundry talks to. Trackers: **Linear**, **GitHub Issues**, **Jira**. SCMs: **GitHub** and **GitLab** (watch the PR/MR, pull failing check summaries). |
| `foundry.api` | FastAPI app: signed Linear/GitHub/Jira/GitLab webhooks, approval commands, run status, the per-run decision timeline, and the dashboard. |
| `foundry.config` | The customisation story: a YAML file plus environment variables (see below). |

### The Cursor handoff (the nice bit)

The cleanest way to hand work off is the [Cursor Linear integration](https://cursor.com/blog/linear). Once a plan is approved, `CursorViaLinearProvider` drops an `@Cursor` comment with the governed instructions onto the Linear issue. Cursor's own integration runs the cloud agent, shows live status in Linear, and opens the PR. Foundry then watches that PR via the GitHub webhook and keeps Linear in sync. Delegated agents pick their own branch names, so PR-to-run correlation falls back to the Linear issue key embedded in the branch or PR title - the loop closes either way. Foundry stays the control plane and never tries to be the agent. For triggers that don't come through Linear, `CursorCloudAgentProvider` calls the Cursor API directly (`POST /v0/agents`).

### The other agents (vendor neutrality, for real)

Set `agent.provider` in the YAML and Foundry dispatches approved work elsewhere, no code changes:

- **`claude_code`** - fires a GitHub Actions `workflow_dispatch` in the target repo; the repo runs Claude Code headless with the governed instructions (reference workflow in [`examples/claude-code-runner.yml`](./examples/claude-code-runner.yml)). The Anthropic key lives in the repo's secrets - Foundry never holds it.
- **`webhook`** - POSTs the HMAC-signed job input to *your* endpoint. Wire up Codex CLI, Aider, an internal tool, anything: do the work on the branch named in the payload, open a PR, and Foundry's GitHub webhook takes it from there.
- **`cursor_cloud` / `cursor_via_linear` / `manual`** - as above, or record the job for a human.

Every provider goes through the same `create_job` path, so the secret-leak guard and the policy gate apply no matter whose agent does the typing.

### Bring your own tracker and SCM

The tracker and the SCM are seams too, not assumptions:

- **Linear** (default) - the original flow: signed webhook in, comments and state back.
- **GitHub Issues** (`tracker.provider: github_issues`) - the issue *is* the ticket. Trigger with the `foundry:candidate` label, approve with a `/foundry approve` comment, and Foundry writes its analysis back as comments and tracks pipeline position with `foundry:status:` labels. Approvers are keyed by GitHub login (the webhook signature plus GitHub's own identity authenticates the actor). Issue keys are synthesised (`CUSTOMERWE-42`) so PR correlation works unchanged.
- **Jira** (`tracker.provider: jira`) - same trigger/command semantics over Jira Cloud webhooks (`/webhooks/jira`). Jira keys (`ACME-42`) already match the correlation pattern. `set_state` fires the matching workflow transition when one exists and otherwise leaves your workflow alone.
- **GitLab** - point a project webhook at `/webhooks/gitlab` (merge request + pipeline events, `X-Gitlab-Token` auth) and merge requests close the loop exactly like GitHub PRs, including CI-failure remediation.

The webhook payload shapes are pinned by recorded fixtures in `tests/fixtures/` - if a live integration ever disagrees with the mapping, the fix is a redacted capture plus a test, no credentials needed.

### The feedback loop

A PR that opens and then fails CI used to be where automation stalled. Now: a failing check suite or a changes-requested review re-dispatches the agent onto the *same branch* with the failure context (failing check names and summaries pulled from GitHub). Every retry passes the policy gate as `retry_agent` - approvals are re-checked, attempts are counted against `remediation.max_agent_retries`, and optional spend is checked against `budget.max_cost_per_run`. Past the cap, the run parks at *review required* with a comment saying a human is needed. Forbidden-path blocks are never retried - blocked stays blocked.

### The dashboard

`GET /dashboard` serves a zero-build, read-only page over the audit data: every run with status badges, and per run the full decision timeline - artifacts, policy decisions with reasons, audit events, agent jobs and spend. It answers "why did the agent do that?" in one click. Token-gated by `FOUNDRY_API_TOKEN` and disabled when none is configured, same fail-closed posture as the API (the JSON equivalent is `GET /runs/{id}/timeline`).

## Customising it

Two kinds of config, kept deliberately separate:

- **Behaviour goes in a YAML file.** Which analyzer, the policy thresholds, the trigger label, who's allowed to approve. Commit this.
- **Secrets go in the environment.** Webhook signing secrets, API tokens, the database URL. Never commit these.

Copy [`foundry.example.yaml`](./foundry.example.yaml) to `foundry.yaml`, edit it, and point `FOUNDRY_CONFIG` at it. The layering is: built-in defaults, then your YAML, then environment variables on top, so each deployment can override the sensitive and operational bits without editing the file.

```yaml
analyzer:
  provider: openai          # or "heuristic" for the no-key default
  model: gpt-5.5
policy:
  repo_confidence_threshold: 70   # block work we can't confidently place in a repo
  max_files_changed: 12           # bigger PRs go to a human
  forbidden_globs: ["infra/**", "migrations/**", "**/.env*", "**/secrets/**"]
  sensitive_path_globs:           # diff-aware risk: PRs touching these escalate
    auth: ["**/auth/**", "**/login/**", "**/sso/**"]
    payments: ["**/billing/**", "**/stripe/**"]
remediation:
  max_agent_retries: 2            # CI-failure/review retries before a human takes over
  retry_on: ["ci_failed", "changes_requested"]
budget:
  max_cost_per_run: 25.0          # deny retries once provider-reported spend hits this
agent:
  provider: cursor_via_linear     # or cursor_cloud / claude_code / webhook / manual
tracker:
  provider: linear                # or github_issues / jira
triggers:
  label: "foundry:candidate"      # runs only start on an explicit opt-in
approval:
  approvers:                      # roles are config, never request payload
    - email: "lead@example.com"
      roles: ["engineering"]
    - email: "security@example.com"
      roles: ["security"]
```

Secrets via env:

| Env var | What it's for |
| --- | --- |
| `FOUNDRY_CONFIG` | Path to your YAML file. |
| `FOUNDRY_DATABASE_URL` | SQLAlchemy URL. SQLite by default, Postgres in prod. |
| `FOUNDRY_LINEAR_WEBHOOK_SECRET` | Verifies inbound Linear webhooks. |
| `FOUNDRY_GITHUB_WEBHOOK_SECRET` | Verifies inbound GitHub webhooks. |
| `FOUNDRY_LINEAR_API_TOKEN` | Turns on the live Linear connector (write-back). |
| `FOUNDRY_GITHUB_API_TOKEN` | Turns on the live GitHub connector (PR files; also the GitHub Issues tracker). |
| `FOUNDRY_JIRA_WEBHOOK_SECRET` | Enables `/webhooks/jira` (token-compared; endpoint disabled without it). |
| `FOUNDRY_JIRA_BASE_URL` / `..._EMAIL` / `..._API_TOKEN` | Jira Cloud credentials when the tracker is `jira`. |
| `FOUNDRY_GITLAB_WEBHOOK_SECRET` | Enables `/webhooks/gitlab` (`X-Gitlab-Token`; endpoint disabled without it). |
| `FOUNDRY_API_TOKEN` | Bearer token for the REST approval endpoint, the timeline API and the dashboard. **Unset = those are disabled** (fail closed); approvals still work via signed Linear comments. |
| `FOUNDRY_AGENT_PROVIDER` | Overrides `agent.provider` from the YAML. |
| `FOUNDRY_CURSOR_API_TOKEN` | Needed when the provider is `cursor_cloud`. |
| `FOUNDRY_AGENT_WEBHOOK_URL` / `..._SECRET` | Needed when the provider is `webhook`; the secret HMAC-signs the job payload. |
| `OPENAI_API_KEY` | Needed when the analyzer provider is `openai`. |
| `TEMPORAL_ADDRESS` | The Temporal server, for durable runs. |

The same code runs on a laptop (SQLite, heuristics, no keys) and in production (Postgres, GPT-5.5, live Linear and GitHub) with nothing changing but config. That's the point.

## Running it

The fastest path to a deployed instance is **[`docs/quickstart.md`](./docs/quickstart.md)** - zero to governed PR in ~30 minutes with `docker compose up` (API + Postgres, optional Temporal profile, dashboard included).

Tagged releases (`vX.Y.Z`) publish automatically: the `project-foundry` package to PyPI and a container image to GHCR (`ghcr.io/jeremysnr/project-foundry`), both gated on the full test suite.

For development:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
pytest
```

Serve the API:

```bash
pip install -e ".[server,http]"
export FOUNDRY_CONFIG=foundry.yaml
export FOUNDRY_LINEAR_WEBHOOK_SECRET=...   # and friends
uvicorn foundry.api.app:app_from_env --factory
```

Optional extras, install what you need:

- `.[llm]` GPT-5.5 analyzer
- `.[http]` live Linear and GitHub transports
- `.[workflow]` Temporal durable execution
- `.[postgres]` Postgres driver + Alembic migrations (`alembic upgrade head`)
- `.[otel]` OpenTelemetry tracing (without it, the spans are free no-ops)

There is also a live end-to-end smoke test (`scripts/smoke_e2e.py`) that drives a real Linear issue through approval, agent dispatch and PR observation. It is gated on `FOUNDRY_E2E=1` plus real credentials and never runs in CI.

There's also a real OPA bundle in `src/foundry/policy/foundry.rego`; run `opa test src/foundry/policy` if you have the OPA CLI. It's kept in lock-step with the Python engine and the two are tested against the same cases.

## The safety rules, in plain English

These are enforced, tested, and not negotiable by a prompt:

- No acceptance criteria, no build. Even if the model swears it's ready. (And when Foundry bounces a ticket, it drafts the acceptance criteria for you - clarification is a 30-second edit, not a rejection.)
- If we can't confidently say which repo this belongs in, we stop and ask.
- Production deploys and database migrations cannot run autonomously. Full stop, for now. `auto_merge` and `production_deploy` are modelled as policy actions that are **denied unconditionally** - "never" is an enforced, audited decision, not an absence of code.
- The policy gate is **default-deny**: an action it doesn't recognise is refused.
- Auth, payments, PII and customer data need a human approval before an agent goes near them - and the approval has to come from someone whose *configured role* covers it. Roles live in committed YAML; an API caller cannot claim "security" for themselves.
- Approval surfaces are authenticated, full stop. Linear comments arrive over a signed webhook with the actor identity from Linear; the REST endpoint needs a bearer token and is disabled outright when none is configured.
- Risk is checked twice: once from the ticket (before dispatch) and again from the **diff** (after the PR opens). A ticket that said "fix the button" whose PR touches `auth/` escalates to human review - and the guardrails re-run on *every push*, so an agent can't open a clean PR and sneak files in later.
- Bigger-than-expected PRs and anything touching forbidden paths get bounced to a human.
- The agent may retry its own failing PR, but every retry is a fresh policy decision: approvals re-checked, attempts capped, budget capped, all audited. Past the cap, a human takes over. A forbidden-path block is never retried.
- No auto-merge. Ever, in this version.
- Secrets never end up in an agent prompt; job inputs are scanned before dispatch.
- Every decision, every artifact, every approval is content-hashed and written down, so you can always answer "why did the agent do that?".

These aren't suggestions, they're the creed. This is the Way.

## How it's wired

```
Tracker --webhook--> Foundry API     (Linear, GitHub Issues, Jira)
                       |
                       v
                  RunDriver  (inline now, Temporal-backed later)
                       |
                       v
                FoundryOrchestrator
                       |
        analyse -> enrich -> classify risk -> plan
                       |
                  Policy gate (OPA-style hard rules)
                       |
                  Human approval (in the tracker)
                       |
              CodingAgentProvider  (Cursor x2, Claude Code, webhook, manual)
                       |
                  PR / MR opens
                       |
   SCM --webhook--> Foundry watches the PR, updates the tracker
                                     (GitHub, GitLab)
```

## Project layout

```
src/foundry/
  config.py        YAML + env settings
  observability.py OpenTelemetry spans (no-op without the extra)
  schemas/         the run artifact contracts (+ enums in common.py)
  engines/         analyzer / enrichment / risk / planner, plus the GPT-5.5 analyzer
  orchestrator.py  the state machine that runs a ticket end to end
  drivers.py       the RunDriver seam (inline today, Temporal attaches here)
  workflows/       decisions.py (pure) + the Temporal workflow, activities, worker
  policy/          the Python engine + foundry.rego (kept in sync)
  agents/          provider abstraction: manual, fake, Cursor (two ways), Claude Code, webhook
  connectors/      Linear, GitHub, GitHub Issues, Jira, GitLab, live HTTP transports
  db/              SQLAlchemy models (runs, artifacts, audit, policy, jobs)
  audit/           content hashing + the verifiable trail
  api/             the FastAPI app, webhook security, payload mapping, dashboard
tests/             one module per package, plus the gated Temporal/Postgres/E2E tests
tests/fixtures/    recorded webhook payloads pinning every payload mapping
migrations/        Alembic migrations (Postgres prod; SQLite dev uses create_all)
examples/          reference Claude Code runner workflow
scripts/           demo.py (offline narrated demo) + the live E2E smoke test
docs/              quickstart
```

## License & contributing

Apache-2.0. See [`LICENSE`](./LICENSE), [`CONTRIBUTING.md`](./CONTRIBUTING.md) and [`SECURITY.md`](./SECURITY.md). The short version of the contribution rules: the safety gates are the product - PRs that weaken a gate, an approval requirement or the audit trail don't merge, and any policy change lands in the Python engine and the Rego bundle together, with tests on both.

## A note on the name

Foundry takes its name from the Mandalorian forge, where the Armorer works raw beskar into something built to last and keeps to a strict creed the whole time. It fit a little too well. This thing takes raw tickets, forges them into solid reviewed work, and won't break its own rules to get there. The policy gate is the Armorer, the safety rules are the creed, and the coding agents are the ones swinging the hammer. Foundry just makes sure nobody melts something important. (If none of that means anything to you, no harm done, it still ships PRs.)

## Where it's going

The loop is complete, closed (the agent now fixes its own failing CI under governance), multi-vendor on every side (three trackers, two SCMs, five agent providers), visible (the dashboard), deployable (`docker compose up`, Alembic migrations, Postgres in CI) and released (PyPI + GHCR on tags). What's left is hardening against live traffic: finishing the Temporal driver against a real server and battle-testing the webhook payload mappings with the E2E smoke script. The long game, per the vision, is to grow this from ticket-to-PR into a full Engineering OS: planning, build, test, deploy, observability and incidents, all under the same control plane. One honest loop first, though.

---

*Forged in the covert. Raw ore in, beskar out.*
