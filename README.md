# Project Foundry

[![CI](https://github.com/JeremySNR/Project-foundry/actions/workflows/ci.yml/badge.svg)](https://github.com/JeremySNR/Project-foundry/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/JeremySNR/Project-foundry?color=blue&label=release&sort=semver)](https://github.com/JeremySNR/Project-foundry/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://github.com/JeremySNR/Project-foundry/blob/main/pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](./LICENSE)

**Foundry turns a ticket into a reviewed pull request, safely, by letting an approved AI agent do the work under supervision.**

*Raw tickets go in. Reviewed pull requests come out. Nothing unsafe makes it through the forge.*

That's the whole pitch. Foundry is not another coding AI. It is the thing that sits *above* your coding AI and decides whether a piece of work is actually ready, what context it needs, whether it's safe to hand to an agent, who has to approve it, and what happened afterwards. The agent (Cursor, Claude, OpenAI, whatever) is the muscle. Foundry is the brain and the seatbelt.

The quick version: it's a bit like Terraform, but for shipping code with AI agents. You describe the intent, Foundry produces a plan, a human approves it, and only then does anything actually happen. Plan, approve, apply.

The formal product statement lives in [`VISION.md`](./VISION.md). This README is the practical, slightly more caffeinated version.

## Why now: the Fable 5 era

Anthropic's [Claude Fable 5 and Mythos 5](https://www.anthropic.com/news/claude-fable-5-mythos-5) can work autonomously for longer than any model before them - Stripe reported a codebase-wide migration that "would have taken a whole team over two months" done in a day. GitHub's early-access verdict points at the same future: *developers handing increasingly ambitious work to agents and trusting the results across the software lifecycle.*

Which means raw capability is no longer the bottleneck. **The bottleneck is everything around the agent**: was the ticket actually ready, which repo does this belong in, is this change safe to delegate, who signed off, what did it cost, and why did the agent do that? An agent that can run for days unsupervised is exactly the kind of agent you should not run unsupervised.

Foundry is designed for precisely this class of model. The more autonomous the agent, the more the gates matter:

- A Mythos-class model will happily take on the migration ticket; Foundry is what makes `migrations/**` a hard policy block instead of a hope.
- Long-horizon autonomy means more decisions made out of sight; Foundry writes down every one of them - content-hashed artifacts, policy decisions with reasons, a full audit timeline.
- Frontier models retry and self-correct; Foundry makes every retry a fresh, capped, budgeted policy decision instead of an unbounded loop.

Point the `claude_code` provider at Fable 5 (or `cursor_*` at Cursor's agents, or the signed webhook at anything else) and you get full agentic engineering with the seatbelt on: the model does the work, the humans keep the keys, and the audit trail keeps the receipts. True end-to-end agentic engineering is a governance problem, and that problem is the product.

## See it run (60 seconds, zero setup)

```bash
git clone https://github.com/JeremySNR/Project-foundry && cd Project-foundry
python -m venv .venv
source .venv/bin/activate     # macOS / Linux
# .venv\Scripts\activate      # Windows (PowerShell or cmd)
pip install -e .
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
| `foundry.api` | FastAPI app: signed Linear/GitHub/Jira/GitLab webhooks, Slack interactivity approvals, approval commands, run status, the per-run decision timeline, the epic view for parent/child runs (`GET /runs/{id}/epic`, plus the epic board `GET /epics`), compliance evidence packs (`GET /runs/{id}/evidence`, plus the org-wide date-range archive `GET /evidence` and the cross-run epic export `GET /runs/{id}/epic/evidence`), and the dashboard. |
| `foundry.config` | The customisation story: a YAML file plus environment variables (see below). |

### The Cursor handoff (the nice bit)

The cleanest way to hand work off is the [Cursor Linear integration](https://cursor.com/blog/linear). Once a plan is approved, `CursorViaLinearProvider` drops an `@Cursor` comment with the governed instructions onto the Linear issue. Cursor's own integration runs the cloud agent, shows live status in Linear, and opens the PR. Foundry then watches that PR via the GitHub webhook and keeps Linear in sync. Delegated agents pick their own branch names, so PR-to-run correlation falls back to the Linear issue key embedded in the branch or PR title - the loop closes either way. Foundry stays the control plane and never tries to be the agent. For triggers that don't come through Linear, `CursorCloudAgentProvider` calls the Cursor API directly (`POST /v0/agents`).

### The other agents (vendor neutrality, for real)

Set `agent.provider` in the YAML and Foundry dispatches approved work elsewhere, no code changes:

- **`claude_code`** - fires a GitHub Actions `workflow_dispatch` in the target repo; the repo runs Claude Code headless with the governed instructions (reference workflow in [`examples/claude-code-runner.yml`](./examples/claude-code-runner.yml)). The Anthropic key lives in the repo's secrets - Foundry never holds it.
- **`webhook`** - POSTs the HMAC-signed job input to *your* endpoint. Wire up Codex CLI, Aider, an internal tool, anything: do the work on the branch named in the payload, open a PR, and Foundry's GitHub webhook takes it from there.
- **`cursor_cloud` / `cursor_via_linear` / `manual`** - as above, or record the job for a human.
- **`auto`** - *learned dispatch* (issue #33): don't pin one agent, let the delivery-memory scorecards pick. Each run routes to the candidate with the best majority-merged history for its work-type/repo, falling back to a configured default until an agent has earned the pick (the same min-sample floor + >50%-merged gate the recommendation enforces). The choice is recorded as an explainable `selection` reason on the run's audit trail, and a retry always reuses the agent that opened the PR. See [Delivery memory](#delivery-memory) below.

Every provider goes through the same `create_job` path, so the secret-leak guard and the policy gate apply no matter whose agent does the typing. (The gate is provider-agnostic - it decides *whether* and in *which mode* to dispatch, never *which* agent - so learned dispatch needed no policy/Rego change.)

### Bring your own tracker and SCM

The tracker and the SCM are seams too, not assumptions:

- **Linear** (default) - the original flow: signed webhook in, comments and state back.
- **GitHub Issues** (`tracker.provider: github_issues`) - the issue *is* the ticket. Trigger with the `foundry:candidate` label, approve with a `/foundry approve` comment, and Foundry writes its analysis back as comments and tracks pipeline position with `foundry:status:` labels. Approvers are keyed by GitHub login (the webhook signature plus GitHub's own identity authenticates the actor). Issue keys are synthesised from the repo name plus a short hash of the full `owner/repo` path (`CUSTOMEREB-42`) so PR correlation works unchanged and similarly-named repos never collide.
- **Jira** (`tracker.provider: jira`) - same trigger/command semantics over Jira Cloud webhooks (`/webhooks/jira`). Jira keys (`ACME-42`) already match the correlation pattern. `set_state` fires the matching workflow transition when one exists and otherwise leaves your workflow alone.
- **GitLab** - point a project webhook at `/webhooks/gitlab` (merge request + pipeline events, `X-Gitlab-Token` auth) and merge requests close the loop exactly like GitHub PRs, including CI-failure remediation. Set `FOUNDRY_GITLAB_API_TOKEN` so MR diffs are fetched and the same file-based gates (forbidden paths, oversize, sensitive areas) apply; without it GitLab MRs are diff-blind, just as GitHub PRs are without `FOUNDRY_GITHUB_API_TOKEN`.

Approvals don't have to happen in the tracker, either:

- **Slack** - approvers who live in chat can approve/reject/stop from an interactive message. Point Slack interactivity at `/webhooks/slack` and set `FOUNDRY_SLACK_SIGNING_SECRET`; each button click is verified against Slack's v0 request signature (with replay-age protection) and then driven through the *same* policy gate, role checks, and audit writes as every other surface. The actor is the Slack-signed `user.id`, so key approvers by Slack user id (as GitHub Issues keys them by login). Fail-closed: no signing secret, no endpoint. (Posting the interactive message and pushing run-status notifications back into Slack is the next slice — see issue #32.)

The webhook payload shapes are pinned by fixtures in `tests/fixtures/` - spec-derived from the providers' webhook docs today, and meant to be replaced by redacted live captures over time. If a live integration ever disagrees with the mapping, the fix is a redacted capture plus a test, no credentials needed.

### The feedback loop

A PR that opens and then fails CI used to be where automation stalled. Now: a failing check suite or a changes-requested review re-dispatches the agent onto the *same branch* with the failure context (failing check names and summaries pulled from GitHub). Every retry passes the policy gate as `retry_agent` - approvals are re-checked, attempts are counted against `remediation.max_agent_retries`, and projected spend is checked against `budget.max_cost_per_run`. The cap binds at first dispatch too, not just on retries; providers that don't report `cost_usd` count `budget.estimated_cost_per_dispatch` per attempt as a proxy. Past the cap, the run parks at *review required* with a comment saying a human is needed. Forbidden-path blocks are never retried - blocked stays blocked.

### The dashboard

`GET /dashboard` serves a zero-build, read-only page over the audit data: a **live fleet strip** at the top (runs in flight, the approval-queue depth *and how long the oldest has waited* — plus how many have breached the SLA when one is configured, agents running *and how long the oldest has been running* — plus how many have breached the execution SLA, PRs open *and how long the oldest has been awaiting review* — plus how many have breached the review SLA, and spend committed by runs still in flight - the "what is every agent doing right now" view, backed by `GET /metrics/fleet`), an **approval-queue panel** (every run parked on a human, oldest first, each with its wait age and SLA breaches highlighted, backed by `GET /metrics/approvals` - issue #37), an **execution-queue panel** (every in-flight agent run — `AGENT_RUNNING`, agent working but no PR yet — oldest first, each with its run-time age and execution-SLA breaches highlighted, the hung/runaway-agent signal, backed by `GET /metrics/executions` - issue #37), a **review-queue panel** (every open PR — `PR_OPEN`, agent shipped a PR and now awaiting review/CI — oldest first, each with its review-latency age and review-SLA breaches highlighted, the "PRs sitting unreviewed for N hours" signal, backed by `GET /metrics/reviews` - issue #37), every run with status badges (filterable down to the approval queue - just what is waiting on a human right now), a delivery-metrics strip, a **delivery-trend table** (PRs shipped vs blocked, by week), the **agent scorecards** and a **per-agent merge-confidence trend** sparkline strip (backed by `GET /metrics/agents/trends` - is each agent improving or sliding?), an **epic board** (multi-repo runs rolled up to one status with their child runs, backed by `GET /epics` - issue #35), and per run the full decision timeline - artifacts, policy decisions with reasons, audit events, agent jobs and spend. It answers "why did the agent do that?" in one click. Token-gated by `FOUNDRY_API_TOKEN` and disabled when none is configured, same fail-closed posture as the API (the JSON equivalent is `GET /runs/{id}/timeline`); when OIDC browser login is configured an operator can **sign in via SSO** at `/dashboard/login` instead of pasting a token (issue #34), and the resulting read-only session cookie is rejected on the approval endpoint so it can't be CSRF'd into an approval. `GET /metrics/fleet` is a snapshot of the runs' *current* state (no time window), distinct from the historical delivery metrics below, which aggregate finished runs over a window.

### Epics and multi-repo runs

One run targets one repo - but the migration ticket above spans several. `orchestrator.intake_epic(ticket, ...)` is the **producer** (#35): it splits an epic ticket into one child run per repo, each opened through the ordinary intake path so it is analysed, risk-classified, planned, **independently policy-gated and approved**, and rolled up under one parent run. The split is deterministic (`engines/decomposition.py`, the same no-model philosophy as the heuristic analyzer): it reads an explicit `Repositories:` section (`- billing-api: migrate the ledger writes` bullets, checkbox markers tolerated) or, failing that, the ticket's `known_repositories` (≥2). The epic's acceptance criteria are carried into every child, and each child is scoped to exactly one repo so it routes confidently. A ticket that names fewer than two repos is not an epic and runs as a single ordinary run. The parent/child runs surface in the [epic board](#the-dashboard) and the cross-run evidence export below. (Follow-up tracked on the issue: wiring epic detection into a webhook/trigger auto-path - `intake_epic` is an explicit entrypoint for now - and LLM-assisted decomposition.)

### Compliance evidence packs

`GET /runs/{id}/evidence` (token-gated, `?format=html` for a rendered page or `?format=pdf` for a downloadable PDF — the optional `[pdf]` extra) exports a single run's full chain - ticket, plan, risk assessment, approvals *with identities and granted roles*, every policy decision, agent jobs and the PR - as a one-click procurement artifact. It bundles an **integrity check** with three parts: each artifact's content hash is recomputed from its stored payload; the append-only audit sequence is checked for gaps; and a **cross-row linked hash chain** over the audit events is recomputed, where each event commits to the previous event's hash, so a dropped, reordered, edited, or inserted row is detectable - not just a missing sequence number. (Events written before the chain existed read back as un-chained rather than failing the check, so it's safe to enable on an existing database. It's tamper-*evidence*: a wholesale rewrite that re-hashes every downstream event would still verify, since the chain has no external anchor - we don't oversell it.) The run's evidence is mapped onto named controls (SOC 2 CC8.1, ISO/IEC 27001:2022 A.8.32, EU AI Act Article 14 by default), each marked satisfied or showing exactly which evidence section is missing. The mappings are **config, not code** - override them under `compliance.control_mappings` in `foundry.yaml`.

`GET /evidence` (token-gated, `?format=html` or `?format=pdf`) is the **org-wide** version: every run created in a date range, each as the same per-run pack, plus a rollup over the whole window - aggregate integrity (which runs, if any, fail their hash/sequence check), a status breakdown, and per-control coverage (how many runs in the range satisfy each configured control). Bound the window with ISO 8601 `from`/`to` (`from` inclusive, `to` exclusive; a date-only `to` like `2026-06-14` covers the whole day) or with `days` (the last N days); with nothing supplied it defaults to the last 90 days. This is the "hand the auditor one file for the quarter" export.

All three evidence exports — per-run, org-wide, and the cross-run epic export below — also render to **PDF** via `?format=pdf` (or `--format pdf` on the CLI), for the auditor who wants a fileable/signable document rather than a web page. PDF rendering is the optional, lazily-imported `[pdf]` extra (pure-Python `fpdf2`, no system binary or network — installed alongside the offline core only when you want it; a PDF request without it returns a clear 503/error rather than a crash). The PDF is built from the same evidence-pack data as the JSON and HTML, so the three formats agree by construction.

`GET /runs/{id}/epic/evidence` (token-gated, `?format=html` or `?format=pdf`) is the **cross-run** cut for an epic (#35): the parent run plus every child run it decomposed into - one per repo - bundled into a single document, so a codebase-wide migration's full chain is one auditable artifact rather than one file per repo. It resolves the epic root first (so it works called on a child too), carries the `compute_epic_rollup` status and explicit root/child linkage, and rolls the same aggregate summary - integrity, status breakdown, per-control coverage - across the whole epic. A run with no children exports cleanly as a one-run epic.

The **`foundry-evidence` CLI** is the offline twin of those three endpoints: it reads the same content-hashed trail straight from the database and produces the same packs from the same builders/renderers, so an auditor or security team can get a JSON or HTML evidence pack without standing up the API or holding a bearer token. `foundry-evidence run <run_id>` exports one run; `foundry-evidence epic <run_id>` exports an epic's whole cross-run chain (resolving the root first, so it works on a child id too); `foundry-evidence archive [--from ISO] [--to ISO] [--days N]` exports the org-wide date-range archive (same `from`-inclusive / `to`-exclusive / date-only-`to`-covers-the-day bounds as `GET /evidence`). Each takes `--format json|html|pdf` (default `json`; `pdf` needs the `[pdf]` extra) and `--output PATH` (default stdout); control mappings come from committed config (`compliance.control_mappings`), never from input - exactly like the API.

### Delivery memory

A run used to end at "PR merged" and the data died there. Now every finished run is distilled into an outcome row - time to merge, retries consumed, escalations, spend, and a block-reason taxonomy - derived entirely from the audit trail (so `foundry-memory backfill` rebuilds it for runs that finished before the table existed). That history feeds back in two ways:

- **Routing priors.** With the catalog enricher, "14 of 16 of this team's feature tickets merged in billing-service" becomes a routing signal with an audit-friendly reason string. It is bounded on every side: a minimum sample size before history speaks at all, a smoothed (never triumphalist) success rate that must clear 50%, and a confidence cap of 89 so an explicit repo association on the ticket (90) always wins. `memory.priors_enabled: false` switches it off.
- **ROI evidence.** `GET /metrics/delivery?days=90` (token-gated) answers the question a buyer actually asks - PRs shipped, blocked and why, median/p90 time to merge, retries, escalations, total agent spend - plus observed routing precision by confidence band, which is the data you need before moving `policy.repo_confidence_threshold` off its default. The dashboard shows the same numbers in a strip above the run list, and `foundry-memory show-priors` prints the mined history. `GET /metrics/delivery/trends?days=90&bucket=week` (or `bucket=day`) returns the same throughput/blocks/spend bucketed over time - the "is delivery trending up or down?" view - which the dashboard renders as a trend table.
- **Agent scorecards.** `GET /metrics/agents?days=90` (token-gated) turns the same outcome rows into *which agent* to trust: per provider, broken down by work type and repo, the smoothed merge rate, retries consumed, and spend. GitHub will never tell you Cursor outperforms Copilot on your billing service; Foundry can, with receipts, and it compounds with every run. `foundry-memory show-scorecards` prints it and the dashboard carries it alongside the metrics strip. `GET /metrics/agents/trends?days=90&bucket=week` (or `bucket=day`, or `foundry-memory show-scorecard-trends`) buckets that same per-provider merge rate over time on one shared axis - the snapshot says "Cursor merges 70%", the trend says whether that is 50%→90% (route more to it) or 90%→50% (pull back); the dashboard renders it as a per-agent confidence sparkline strip. `GET /metrics/agents/recommendation?work_type=feature&repo=billing-service` (or `foundry-memory recommend-agent --work-type feature --repo billing-service`) goes one step further and turns those scorecards into a single, explainable provider pick - same delivery-memory guard rails as the routing priors (min-sample floor, a >50%-merged history gate, an optional candidate allow-list so it only suggests agents you can actually dispatch). Setting **`agent.provider: auto`** (issue #33) turns that recommendation into the actual routing decision: the orchestrator calls `recommend_provider` at first dispatch, routes to its pick over the configured `auto_candidates`, and falls back to `auto_fallback` until an agent earns the pick - the same guard rails, now acting, not just reporting. The pick is recorded as an explainable `selection` block on the `AGENT_STARTED` audit event, a retry never re-routes (it reuses the agent that opened the PR), and the kill switch is simply not setting `auto`. Because the policy gate is provider-agnostic, this needed no gate/Rego change.

Blocks are never auto-judged: a blocked run whose issue later merges in a fresh run is reported as *superseded*, which is the honest proxy for "the gate held and a human fixed the input".

## Customising it

Two kinds of config, kept deliberately separate:

- **Behaviour goes in a YAML file.** Which analyzer, the policy thresholds, the trigger label, who's allowed to approve. Commit this.
- **Secrets go in the environment.** Webhook signing secrets, API tokens, the database URL. Never commit these.

Copy [`foundry.example.yaml`](./foundry.example.yaml) to `foundry.yaml`, edit it, and point `FOUNDRY_CONFIG` at it. The layering is: built-in defaults, then your YAML, then environment variables on top, so each deployment can override the sensitive and operational bits without editing the file.

```yaml
analyzer:
  provider: openai          # or "heuristic" for the no-key default
  model: gpt-5.5
risk:
  provider: llm             # or "heuristic" (default): keywords + globs, no key.
                            # "llm" adds cited evidence to the audit trail and can
                            # only ESCALATE over the deterministic floor, never lower it.
policy:
  repo_confidence_threshold: 70   # block work we can't confidently place in a repo
  max_files_changed: 12           # bigger PRs go to a human
  forbidden_globs: ["infra/**", "**/infra/**", "migrations/**", "**/migrations/**", "**/.env*", "**/secrets/**"]
  repo_forbidden_globs:           # per-repo extras for monorepos; additive on top
    payments-service: ["**/ledger/**"]   # of forbidden_globs, scoped to the routed repo
  repo_required_roles:            # per-repo approval roles (#31), additive on top of
    payments-service: ["security"]       # risk-derived roles; can only make approval stricter
  min_approvals: 1                # N-of-M "two-person rule" (#31): distinct human sign-offs
  repo_min_approvals:             # per-repo override; effective minimum is max(global, this)
    payments-service: 2                  # so a repo can only ever demand *more*, never fewer
  path_required_roles:            # per-PATH approval roles (#31/#35): diff-aware, monorepo
    "**/billing/**": ["security"]        # subtree -> role; a PR touching it that no approver
                                         # signed for escalates to REVIEW_REQUIRED (additive)
  sensitive_path_globs:           # diff-aware risk: PRs touching these escalate
    auth: ["**/auth/**", "**/login/**", "**/sso/**"]
    payments: ["**/billing/**", "**/stripe/**"]
remediation:
  max_agent_retries: 2            # CI-failure/review retries before a human takes over
  retry_on: ["ci_failed", "changes_requested"]
budget:
  max_cost_per_run: 25.0          # deny dispatch (first + retries) once projected spend hits this
  estimated_cost_per_dispatch: 0.0 # proxy cost for providers that don't report spend (0 = off)
agent:
  provider: cursor_via_linear     # or cursor_cloud / claude_code / webhook / manual / auto
  # provider: auto                # learned dispatch (#33): pick per run by scorecard
  # auto_candidates: [claude_code, cursor_cloud]  # the agents auto may route between
  # auto_fallback: manual         # used until a candidate earns the pick
  # auto_min_samples: 3           # min runs before a candidate is eligible
tracker:
  provider: linear                # or github_issues / jira
triggers:
  label: "foundry:candidate"      # runs only start on an explicit opt-in
decomposition:
  provider: heuristic             # or llm - infer prose-described epic splits (grounded, degrade-to-floor, #35)
epics:
  auto_decompose: false           # split a multi-repo ticket into per-repo child runs at intake (#35)
approval:
  approvers:                      # roles are config, never request payload
    - email: "lead@example.com"
      roles: ["engineering"]
    - email: "security@example.com"
      roles: ["security"]
```

Rather than start from a blank policy block, copy a vetted preset: Foundry ships a **starter policy library** (`baseline`, `soc2`, `change-management`) built only from the knobs above. `foundry-policy presets` lists them, `foundry-policy show soc2` prints one to paste into your config, and `foundry-policy explain soc2` shows the gate knobs it resolves to (threshold, protected paths, per-repo sign-offs, caps). The presets are copy-to-adopt — browsing the library never changes a running deployment — and each is strict-or-stricter than the built-in defaults, so adopting one only ever tightens the gate.

Secrets via env:

| Env var | What it's for |
| --- | --- |
| `FOUNDRY_CONFIG` | Path to your YAML file. |
| `FOUNDRY_DATABASE_URL` | SQLAlchemy URL. SQLite by default, Postgres in prod. |
| `FOUNDRY_LINEAR_WEBHOOK_SECRET` | Verifies inbound Linear webhooks. |
| `FOUNDRY_GITHUB_WEBHOOK_SECRET` | Verifies inbound GitHub webhooks. |
| `FOUNDRY_LINEAR_API_TOKEN` | Turns on the live Linear connector (write-back). |
| `FOUNDRY_GITHUB_API_TOKEN` | Turns on the live GitHub connector (PR files; also the GitHub Issues tracker). |
| `FOUNDRY_JIRA_WEBHOOK_SECRET` | Enables `/webhooks/jira` (token-compared; endpoint disabled without it). Jira has no body signature, so this is an **approver-level credential** (the actor identity comes from the payload) — header-only by default; `?token=` query delivery needs `tracker.jira_allow_query_token: true`. See SECURITY.md. |
| `FOUNDRY_JIRA_BASE_URL` / `..._EMAIL` / `..._API_TOKEN` | Jira Cloud credentials when the tracker is `jira`. |
| `FOUNDRY_GITLAB_WEBHOOK_SECRET` | Enables `/webhooks/gitlab` (`X-Gitlab-Token`; endpoint disabled without it). |
| `FOUNDRY_GITLAB_API_TOKEN` / `..._BASE` | Fetches MR diffs so GitLab MRs run the same file-based gates as GitHub PRs (without it, MRs are diff-blind). `..._BASE` overrides the API root for self-managed GitLab. |
| `FOUNDRY_SLACK_SIGNING_SECRET` | Enables `/webhooks/slack` (Slack v0 request-signing + replay-age; endpoint disabled without it). |
| `FOUNDRY_SLACK_BOT_TOKEN` / `FOUNDRY_SLACK_CHANNEL` | Enables outbound Slack: posts the interactive approval message + status updates (parked/blocked/PR open/merged). Fail-closed — both (token + channel, the latter also settable via `notifications.slack_channel`) required, else no notifier. |
| `FOUNDRY_API_TOKEN` | Bearer token for the REST approval endpoint, the timeline API, the delivery-metrics API, the compliance evidence-pack endpoint and the dashboard. **Unset = those are disabled** (fail closed) unless OIDC is configured; approvals still work via signed Linear comments. |
| `FOUNDRY_OIDC_ISSUER` / `..._AUDIENCE` / `..._JWKS_URI` | Enables **OIDC** bearer auth on the token-gated API as an alternative/addition to `FOUNDRY_API_TOKEN` (issue #34): a valid JWT from your IdP is accepted alongside the static token. All three are required together (also settable under `auth.oidc` in YAML). Optionally `FOUNDRY_OIDC_ALGORITHMS` (comma-separated allow-list, default `RS256`) and `FOUNDRY_OIDC_LEEWAY_SECONDS` (clock-skew tolerance, default 60). Needs the `oidc` extra (`pip install 'project-foundry[oidc]'`; bundled in the Docker image). |
| `FOUNDRY_OIDC_SUBJECT_CLAIM` / `..._GROUP_CLAIM` | For OIDC-authenticated REST approvals (issue #34): which **verified** claim names identify the approver (default `email`, falling back to `sub`) and carry IdP group membership (default `groups`). The approver identity is then bound to the verified token, not the request body. The IdP-group → approver-role map itself is YAML-only — `auth.oidc.group_role_map` ({group → roles}); a verified member of a mapped group is granted those roles on top of any committed `approval.approvers` grant. |
| `FOUNDRY_OIDC_CLIENT_SECRET` / `FOUNDRY_SESSION_SECRET` | Enable **browser SSO login** for the dashboard (issue #34): an operator signs in via your IdP (OAuth2 authorization-code + PKCE) at `/dashboard/login` instead of pasting a token. The non-secret parts go under `auth.oidc` (`client_id` / `authorization_endpoint` / `token_endpoint` / `redirect_uri`, all-or-nothing; optional `scopes` / `session_ttl_seconds` / `cookie_secure`, also env-settable as `FOUNDRY_OIDC_CLIENT_ID` etc.); these two **secrets** are env-only and required to wire login (the OAuth client secret, and the HMAC key signing the session cookie). The session cookie authenticates the dashboard's **read** calls only — it is rejected on the approval endpoint (CSRF-safe). Needs the `oidc` + `http` extras. |
| `FOUNDRY_AGENT_PROVIDER` | Overrides `agent.provider` from the YAML (`manual` / `cursor_*` / `claude_code` / `webhook` / `auto`). |
| `FOUNDRY_AGENT_AUTO_CANDIDATES` / `..._FALLBACK` / `..._MIN_SAMPLES` | Learned dispatch (issue #33), used when the provider is `auto`: a comma-separated candidate allow-list the scorecard routes between, the fallback agent used until a candidate earns the pick, and the min-sample floor. Also settable under `agent.auto_*` in YAML. |
| `FOUNDRY_CURSOR_API_TOKEN` | Needed when the provider is `cursor_cloud`. |
| `FOUNDRY_AGENT_WEBHOOK_URL` / `..._SECRET` | Needed when the provider is `webhook`; the secret HMAC-signs the job payload. |
| `OPENAI_API_KEY` | Needed when the analyzer provider is `openai`. |
| `TEMPORAL_ADDRESS` | The Temporal server, for durable runs. |
| `FOUNDRY_CONTEXT_PROVIDER` | Overrides `context.provider` (`static`, `catalog` or `code`). |
| `FOUNDRY_CONTEXT_ORG` | GitHub org for `foundry-catalog sync`; overrides `context.org`. |

The same code runs on a laptop (SQLite, heuristics, no keys) and in production (Postgres, GPT-5.5, live Linear and GitHub) with nothing changing but config. That's the point.

## Running it

The fastest path to a deployed instance is **[`docs/quickstart.md`](./docs/quickstart.md)** - zero to governed PR in ~30 minutes with `docker compose up` (API + Postgres, optional Temporal profile, dashboard included). A bare `docker compose up` boots on a fresh clone with no copy step (it mounts the committed `foundry.example.yaml`); the API container applies Alembic migrations on startup, so Postgres gets its schema — and its `alembic_version` stamp — without a manual step.

Tagged releases (`vX.Y.Z`) publish a container image to GHCR (`ghcr.io/jeremysnr/project-foundry`) automatically, gated on the full test suite.

For development:

```bash
python -m venv .venv
source .venv/bin/activate     # macOS / Linux; on Windows: .venv\Scripts\activate
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

To use the catalog-backed context enricher (`context.provider: catalog`), populate the repo
catalog first and then keep it fresh with a periodic sweep:

```bash
export FOUNDRY_GITHUB_API_TOKEN=...
foundry-catalog sync --org <your-github-org> --bootstrap
```

Run this on a schedule (e.g. daily cron or a Temporal workflow) so the catalog stays current.
The sync is stateful and budget-aware: interrupted sweeps resume automatically on the next run.

The code-aware enricher (`context.provider: code`) goes further: the sync also records each
repo's file tree (one Git Trees API call), test layout, CODEOWNERS rules and root dependency
manifests — `foundry-catalog sync --code-facts`, implied when the provider is `code`. Routing
then matches tickets against actual code paths, reason strings cite concrete files and owners
("Code evidence: src/billing/invoice.py; owners: @org/payments"), and the context bundle carries
candidate files, the test layout and inferred test commands for the plan. Worst case the sync
spends 9 API calls per repo instead of 3; the same budget and resume semantics apply.

With `planner.provider: llm` the planner consumes that code-aware context and produces a
**file-level** plan: named files to touch, where the tests live, the commands to verify, and a
populated `expected_files_or_areas`. It's only consulted for a buildable, confidently-routed run;
the goal/scope/branch and the guardrail block (forbidden paths, no migrations, stop conditions)
stay deterministic — the model enriches the plan but can't relax a constraint — and an LLM failure
degrades to the deterministic template plan. The template planner remains the no-key default.

Epic decomposition has the same shape. The deterministic producer splits a multi-repo ticket via
an explicit `Repositories:` section or `≥2` associated repos; with `decomposition.provider: llm`,
an epic described only in *prose* — "migrate the ledger in `billing-api` and the checkout in
`customer-web`" — is recovered by inference. The deterministic decomposer stays a hard floor: the
model is consulted only when the floor declines, every repo it proposes must already appear in the
ticket text (no invented repos), fewer than two grounded repos degrades to the floor, and each
child still runs the full policy gate and its own approval — so the LLM can only *add* a split,
never weaken one.

Optional extras, install what you need:

- `.[llm]` GPT-5.5 analyzer
- `.[http]` live Linear and GitHub transports
- `.[workflow]` Temporal durable execution
- `.[postgres]` Postgres driver + Alembic migrations. On Postgres, Alembic is the **single** schema owner: run `alembic upgrade head` (the Docker image does this automatically on startup; `make migrate` does it by hand). SQLite dev/test databases have no migration step and are bootstrapped in-process.
- `.[pdf]` PDF rendering for the compliance evidence exports (pure-Python `fpdf2`; JSON/HTML never need it)
- `.[otel]` OpenTelemetry tracing (without it, the spans are free no-ops)

There is also a live end-to-end smoke test (`scripts/smoke_e2e.py`) that drives a real Linear issue through approval, agent dispatch and PR observation. It is gated on `FOUNDRY_E2E=1` plus real credentials and never runs in CI.

There's also a real OPA bundle in `src/foundry/policy/foundry.rego`; run `opa test src/foundry/policy` if you have the OPA CLI. It's kept in lock-step with the Python engine and the two are tested against the same cases.

## The safety rules, in plain English

These are enforced, tested, and not negotiable by a prompt:

- No acceptance criteria, no build. Even if the model swears it's ready. (And when Foundry bounces a ticket, it drafts the acceptance criteria for you - clarification is a 30-second edit, not a rejection.)
- If we can't confidently say which repo this belongs in, we stop and ask.
- Production deploys and database migrations cannot run autonomously. Full stop, for now. `auto_merge` and `production_deploy` are modelled as policy actions that are **denied unconditionally** - "never" is an enforced, audited decision, not an absence of code.
- The policy gate is **default-deny**: an action it doesn't recognise is refused.
- No agent runs without a human's sign-off, full stop. The gate itself requires **at least one recorded approval** before any autonomous action - the human-in-the-loop promise is a policy rule, not just an orchestration detail, so a code path that reached the gate without an approval would still be refused. (Sensitive areas need *specific* roles on top of that; see the next point.) For a stricter **two-person rule**, set `policy.min_approvals` (or a per-repo `policy.repo_min_approvals`): the run accumulates *distinct* sign-offs — each its own audited approval, a duplicate from the same person refused — and only proceeds once enough humans have approved. The minimum is a one-way ratchet (a per-repo value can only raise it), so it can only ever make approval stricter. When more than one sign-off is required, the approval prompt (the tracker comment and the Slack message) says so up front ("Approvers required: N distinct sign-offs"), so the first approver isn't surprised by a run that stays parked after they approve. And once a partial sign-off lands, the next approver is re-pinged on the same surfaces ("N of M distinct sign-offs collected — K more required"), so a two-person-rule run doesn't go silent between approvals.
- Auth, payments, PII and customer data need a human approval before an agent goes near them - and the approval has to come from someone whose *configured role* covers it. Roles live in committed YAML; an API caller cannot claim "security" for themselves. A sign-off from someone whose role doesn't cover the work is **refused up front** - never recorded and then quietly blocked at dispatch, so the audit trail never shows an approval for work that was actually denied. Roles can be scoped to a **repo** (`policy.repo_required_roles`, resolved at intake) or, for monorepos, to a **path** (`policy.path_required_roles`): a PR whose diff touches a configured subtree (e.g. `**/billing/**`) that no approver signed for escalates to human review when the PR opens. Both are strictly additive — they only ever *add* a required sign-off, never drop one.
- Approval surfaces are authenticated, full stop. Linear comments arrive over a signed webhook with the actor identity from Linear; the REST endpoint needs a bearer token (the static `FOUNDRY_API_TOKEN` or, when configured, an **OIDC JWT** from your IdP - signature, issuer, audience, expiry and an RS256-only algorithm allow-list all verified) and is disabled outright when no credential is configured. On the OIDC path the approver *identity* is bound to the **verified token**, not the request body, and **IdP groups map to approver roles** via committed config (`auth.oidc.group_role_map`) - so SSO group membership, not a hand-maintained email list, can grant approval authority, while the role a run *requires* is unchanged.
- A captured webhook can't be replayed into action. Every delivery is deduped against a durable, bounded table (`(provider, delivery_id)`, shared across workers, pruned on a TTL), so a redelivered approval or CI-failure event is dropped instead of re-driving state. Set `webhook.replay_max_age_seconds` to additionally reject deliveries older than a window for providers that carry a timestamp (Linear).
- The network surfaces are rate limited. Signatures stop *unauthorised* callers; a coarse per-client cap (on by default, configurable under `rate_limit:`) stops a flood of authorised-looking ones - a replayed webhook in a loop, a runaway integration, a token brute-force - from exhausting the process. Webhooks and the API get independent budgets so a burst on one can't starve the other.
- Risk is checked twice: once from the ticket (before dispatch) and again from the **diff** (after the PR opens). A ticket that said "fix the button" whose PR touches `auth/` escalates to human review - and the guardrails re-run on *every push*, so an agent can't open a clean PR and sneak files in later. With `risk.provider: llm`, a model pass writes its cited reasoning into the audit trail ("touches session issuance in `auth/tokens.py`") - and it may only *escalate* over the deterministic keyword/glob floor, never downgrade it.
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
  engines/         analyzer / enrichment / risk / planner, plus the GPT-5.5 analyzer,
                   the escalate-only LLM risk classifier (llm_risk.py), and the
                   file-level LLM planner (llm_planner.py)
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
tests/fixtures/    spec-derived webhook payloads pinning every payload mapping
migrations/        Alembic migrations — the sole schema owner on Postgres; SQLite dev uses init_schema/create_all
examples/          reference Claude Code runner workflow
scripts/           demo.py (offline narrated demo) + the live E2E smoke test
docs/              quickstart
```

## License & contributing

Apache-2.0. See [`LICENSE`](./LICENSE), [`CONTRIBUTING.md`](./CONTRIBUTING.md) and [`SECURITY.md`](./SECURITY.md). The short version of the contribution rules: the safety gates are the product - PRs that weaken a gate, an approval requirement or the audit trail don't merge, and any policy change lands in the Python engine and the Rego bundle together, with tests on both.

## A note on the name

Foundry takes its name from the Mandalorian forge, where the Armorer works raw beskar into something built to last and keeps to a strict creed the whole time. It fit a little too well. This thing takes raw tickets, forges them into solid reviewed work, and won't break its own rules to get there. The policy gate is the Armorer, the safety rules are the creed, and the coding agents are the ones swinging the hammer. Foundry just makes sure nobody melts something important. (If none of that means anything to you, no harm done, it still ships PRs.)

## Where it's going

The loop is complete, closed (the agent now fixes its own failing CI under governance), multi-vendor on every side (three trackers, two SCMs, five agent providers), visible (the dashboard), deployable (`docker compose up`, Alembic migrations, Postgres in CI) and released (GHCR image on tags). What's left is hardening against live traffic: finishing the Temporal driver against a real server and battle-testing the webhook payload mappings with the E2E smoke script. The long game, per the vision, is to grow this from ticket-to-PR into a full Engineering OS: planning, build, test, deploy, observability and incidents, all under the same control plane. One honest loop first, though.

---

*Forged in the covert. Raw ore in, beskar out.*
