# Project Foundry

**Foundry turns a Linear ticket into a reviewed pull request, safely, by letting an approved AI agent do the work under supervision.**

That's the whole pitch. It is not another coding AI. It is the thing that sits *above* your coding AI and decides whether a piece of work is actually ready, what context it needs, whether it's safe to hand to an agent, who has to approve it, and what happened afterwards. The agent (Cursor, Claude, OpenAI, whatever) is the muscle. Foundry is the brain and the seatbelt.

If you want the formal product statement, it lives in [`VISION.md`](./VISION.md). This README is the practical, slightly more caffeinated version.

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
   -> Linear gets updated with status, summary and next action
```

And it deliberately **stops at a reviewed PR**. No auto-merge, no auto-deploy to production, no autonomous database migrations, no touching auth or payments without a human saying yes. The brakes are the product, not a missing feature.

## Is this "the Kubernetes of agentic engineering"?

Sort of, and it's worth being honest about where the analogy helps and where it lies to you.

Where it fits: Kubernetes is a control plane that sits above interchangeable workloads (containers) and schedules and governs them. Foundry sits above interchangeable coding agents and schedules and governs them. Same shape. Swappable providers are basically the driver/CRD pattern, and we even use OPA, which is literally Kubernetes admission-control tech.

Where it breaks: Kubernetes is about autonomous reconciliation at scale with no human in the loop. Foundry is almost the opposite on purpose. It runs a bounded, per-ticket workflow with a human approval gate in the middle, and the two things that make it valuable have no Kubernetes equivalent at all: it reasons about *intent* (is this work any good?), and it treats human approval as a first-class state, not a failure.

So "Kubernetes of agentic engineering" is a great hook for the *shape*, but if you take it literally you'll miss the point. A more honest one-liner is "a control plane for agentic software delivery": it borrows the control-plane idea from Kubernetes, the durable-workflow idea from Temporal, and the admission-control idea from OPA, then adds the thing those don't have, which is judgement about the work itself. Use the k8s line in the pitch deck, just know what it's papering over.

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
| `foundry.agents` | The coding-agent abstraction. `manual`, a test fake, and **Cursor** two ways (see below). |
| `foundry.connectors` | Adapters for the tools Foundry talks to: Linear (read the issue, write back status and comments) and GitHub (watch the PR). |
| `foundry.api` | FastAPI app: signed Linear and GitHub webhooks, approval commands, run status. |
| `foundry.config` | The customisation story: a YAML file plus environment variables (see below). |

### The Cursor handoff (the nice bit)

You asked for the [Cursor Linear integration](https://cursor.com/blog/linear) and it's honestly the cleanest way to do this. Once a plan is approved, `CursorViaLinearProvider` drops an `@Cursor` comment with the governed instructions onto the Linear issue. Cursor's own integration runs the cloud agent, shows live status in Linear, and opens the PR. Foundry then watches that PR via the GitHub webhook and keeps Linear in sync. Foundry stays the control plane and never tries to be the agent. There's also `CursorCloudAgentProvider` that calls the Cursor API directly (`POST /v0/agents`) for triggers that don't come through Linear.

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
triggers:
  label: "foundry:candidate"      # runs only start on an explicit opt-in
approval:
  authorised_approvers: ["lead@example.com"]
```

Secrets via env:

| Env var | What it's for |
| --- | --- |
| `FOUNDRY_CONFIG` | Path to your YAML file. |
| `FOUNDRY_DATABASE_URL` | SQLAlchemy URL. SQLite by default, Postgres in prod. |
| `FOUNDRY_LINEAR_WEBHOOK_SECRET` | Verifies inbound Linear webhooks. |
| `FOUNDRY_GITHUB_WEBHOOK_SECRET` | Verifies inbound GitHub webhooks. |
| `FOUNDRY_LINEAR_API_TOKEN` | Turns on the live Linear connector (write-back). |
| `FOUNDRY_GITHUB_API_TOKEN` | Turns on the live GitHub connector (PR files). |
| `OPENAI_API_KEY` | Needed when the analyzer provider is `openai`. |
| `TEMPORAL_ADDRESS` | The Temporal server, for durable runs. |

The same code runs on a laptop (SQLite, heuristics, no keys) and in production (Postgres, GPT-5.5, live Linear and GitHub) with nothing changing but config. That's the point.

## Running it

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
- `.[otel]` OpenTelemetry tracing (without it, the spans are free no-ops)

There's also a real OPA bundle in `src/foundry/policy/foundry.rego`; run `opa test src/foundry/policy` if you have the OPA CLI. It's kept in lock-step with the Python engine and the two are tested against the same cases.

## The safety rules, in plain English

These are enforced, tested, and not negotiable by a prompt:

- No acceptance criteria, no build. Even if the model swears it's ready.
- If we can't confidently say which repo this belongs in, we stop and ask.
- Production deploys and database migrations cannot run autonomously. Full stop, for now.
- Auth, payments, PII and customer data need a human approval before an agent goes near them.
- Bigger-than-expected PRs and anything touching forbidden paths get bounced to a human.
- No auto-merge. Ever, in this version.
- Secrets never end up in an agent prompt; job inputs are scanned before dispatch.
- Every decision, every artifact, every approval is content-hashed and written down, so you can always answer "why did the agent do that?".

## How it's wired

```
Linear --webhook--> Foundry API
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
                  Human approval (in Linear)
                       |
              CodingAgentProvider  (Cursor via Linear, Cursor API, manual)
                       |
                  GitHub PR opens
                       |
   GitHub --webhook--> Foundry watches the PR, updates Linear
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
  agents/          provider abstraction: manual, fake, Cursor (two ways)
  connectors/      Linear, GitHub, comment/state rendering, live HTTP transports
  db/              SQLAlchemy models (runs, artifacts, audit, policy, jobs)
  audit/           content hashing + the verifiable trail
  api/             the FastAPI app, webhook security, payload mapping
tests/             one module per package, plus the gated Temporal tests
```

## Where it's going

The loop is complete and the seams are in place. What's left is mostly the stuff that needs real credentials or live infra to be worth doing: finishing the Temporal driver against a real server, Postgres migrations, container and deploy manifests, and a proper end-to-end smoke test against live Linear, GitHub, Cursor and OpenAI. The long game, per the vision, is to grow this from ticket-to-PR into a full Engineering OS: planning, build, test, deploy, observability and incidents, all under the same control plane. One honest loop first, though.
