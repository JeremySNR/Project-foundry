# Project Foundry

Project Foundry is an **AI-native engineering control plane** that turns product
intent into governed software delivery. It is not the AI that writes code — it is
the system that decides what work is requested, whether there is enough
information, what context is needed, which system is affected, whether the work is
safe for an agent, which agent to use, what approvals are required, and what
happened afterwards.

> **Read [`VISION.md`](./VISION.md) first** — it is the canonical product
> description and the north star that keeps this build honest.

## Module 1 — Ticket-to-PR

> **Promise:** Connect Linear and GitHub. Let approved AI agents turn well-formed
> tickets into reviewed PRs, safely.

The first Foundry loop:

```
Linear ticket
  → Foundry analyses & enriches it
  → Foundry creates a delivery plan
  → Human approves
  → Foundry launches a coding agent
  → Agent creates branch / PR
  → PR reviewed by CI + CodeRabbit + human as needed
  → Linear updated with status, summary, next action
```

The MVP **stops at a draft / ready-for-review PR**. Explicit non-goals: auto-merge,
auto production deploy, incident-to-fix automation, a custom developer portal,
multi-team marketplace, org-wide agent memory, and autonomous
migration/auth/payment/customer-data changes.

## What's in this repository (the testable core foundation)

This first increment is the governed, fully unit-tested core — **no live external
calls**. Live adapters (Temporal, LangGraph, OPA server, Linear/GitHub MCP, coding
agents) plug into these contracts later.

| Package | Responsibility |
| --- | --- |
| `foundry.schemas` | Pydantic contracts for every run artifact: `RawTicket`, `TicketAnalysis`, `ContextBundle`, `RiskAssessment`, `DeliveryPlan`, `PullRequestState`, coding-agent job I/O. |
| `foundry.engines` | The intelligence stages as `Protocol`s + deterministic reference implementations: `HeuristicAnalyzer`, `StaticContextEnricher`, `HeuristicRiskClassifier`, `TemplatePlanner`. Real LLM/LangGraph backends implement the same protocols. |
| `foundry.orchestrator` | `FoundryOrchestrator` — drives a run through analyse → enrich → risk → plan → policy gate → human approval → agent dispatch → PR monitoring, persisting every artifact, audit event and policy decision. |
| `foundry.policy`  | The policy gate — **hard rules, not prompts**. `LocalPolicyEngine` (pure-Python, default) mirrors `foundry.rego` for an OPA server. |
| `foundry.agents`  | The `CodingAgentProvider` abstraction + registry. Backends: `ManualProvider`, `InMemoryFakeProvider`, and **Cursor** — `CursorViaLinearProvider` (delegates approved work to Cursor by `@Cursor`-commenting the Linear issue) and `CursorCloudAgentProvider` (direct `POST /v0/agents`). Foundry is never built around one coding tool. |
| `foundry.connectors` | Adapters for the tools Foundry coordinates. `IssueTracker` protocol; `LinearConnector` (GraphQL via an injected transport — no network in tests); comment/state rendering that writes Foundry's analysis and `Foundry: …` workflow states back to the issue. |
| `foundry.db`      | SQLAlchemy data model: runs, versioned content-hashed artifacts, append-only audit events, policy decisions, agent jobs. |
| `foundry.audit`   | Content hashing + helpers to persist the run's verifiable trail. |
| `foundry.api`     | FastAPI app **wired to the orchestrator**: signed + idempotent Linear webhook intake runs `intake_and_plan` and persists a real run; authorised `/foundry approve\|reject\|stop` commands drive approval + dispatch; run-status endpoints read from the DB. |

### The run loop (today, no live calls)

```python
from foundry.db import create_all, make_engine, make_session_factory
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.ticket import RawTicket

engine = make_engine()                 # SQLite by default
create_all(engine)
orch = FoundryOrchestrator(make_session_factory(engine))

ticket = RawTicket(
    issue_id="i-1", issue_key="LIN-123", title="Add customer favourites",
    description="Acceptance Criteria:\n- A favourites button exists",
    known_repositories=["customer-web"],
)
run_id = orch.intake_and_plan(ticket, trigger_type="label")   # -> WAITING_APPROVAL
orch.approve(run_id, user="lead@example.com")                  # -> APPROVED
job = orch.dispatch_agent(run_id)                              # -> AGENT_RUNNING (policy re-checked)
# ...later, when a PR is observed:
# orch.record_pr(run_id, pr_state)                             # -> PR_OPEN | REVIEW_REQUIRED | BLOCKED
```

The same loop is exposed over HTTP: a signed `POST /webhooks/linear` runs
`intake_and_plan`, and `POST /runs/{id}/approval` with `/foundry approve` drives
approval and agent dispatch. Signal the affected repo with a Linear label
`repo:<name>` so a run can reach a confident repository without a live GitHub
lookup.

### The Cursor handoff (preferred dispatch)

Foundry's job is to *govern* the work, then delegate the actual coding. The
recommended path uses Cursor's [Linear integration](https://cursor.com/blog/linear):
once a plan is approved, `CursorViaLinearProvider` posts an `@Cursor` comment with
the **governed, approved** instructions onto the Linear issue. Cursor's cloud agent
then runs, reports status back in Linear, and auto-opens the PR — Foundry observes
that PR (`record_pr`) and keeps the `Foundry: …` state in sync. This keeps Foundry
the control plane above the tools rather than re-implementing agent plumbing.
`CursorCloudAgentProvider` is the alternative for non-Linear triggers, launching an
agent directly via `POST https://api.cursor.com/v0/agents`.

> Going live needs the Cursor⨉Linear integration enabled (a Cursor admin
> connection + GitHub), a Linear API token for the connector transport, and the
> webhook signing secret — none of which are required for the test suite.

A Temporal workflow will later wrap these steps as durable activities; the
deterministic engines are swapped for LLM/LangGraph and the connectors for the
Linear/GitHub MCP servers — none of which changes the contracts above.

### Governance guarantees encoded here

- **Idempotent intake** — a redelivered webhook never creates a second run.
- **Authenticated webhooks** — bad signatures are rejected; no workflow starts.
- **Acceptance criteria required** — a ticket is not "buildable" without them, even
  if the LLM claims it is ready.
- **Repo confidence threshold** — work is blocked when no single repo clears 70.
- **MVP hard blocks** — production deploy and DB migrations cannot run autonomously.
- **Sensitive areas need approval** — auth/infra need engineering; customer-data/PII/
  payments need security. Draft-PR mode only for low/medium risk; never auto-merge.
- **No secrets to agents** — job inputs are scanned before dispatch.

## Architecture (target)

```
Linear ──webhook/MCP──▶ Foundry API
                          │
                          ▼
                    Temporal workflow         (crash-proof, long-running, waits)
                          │
                          ▼
              LangGraph / Agents SDK flow      (analysis, context, planning)
                          │
                          ▼
                 Ticket Intelligence Engine
                          │
                          ▼
                      Policy Gate (OPA)         ← foundry.policy mirrors this
                          │
                          ▼
                    Human approval (Linear)
                          │
                          ▼
              CodingAgentProvider adapter        ← foundry.agents
            (Cursor / Claude Code / OpenAI / manual)
                          │
                          ▼
                  GitHub branch + PR
                          │
                          ▼
              CI + CodeRabbit + human review
                          │
                          ▼
                  Foundry PR monitor ─▶ Linear update
```

## Getting started

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
pytest
```

Optional extras:

- `pip install -e ".[server]"` then `uvicorn foundry.api.app:create_app --factory`
  (provide a webhook secret).
- `pip install -e ".[workflow]"` for the Temporal / LangGraph adapters (added later).

### Policy

The Python `LocalPolicyEngine` is the default and runs with no external service.
The equivalent OPA bundle lives at `src/foundry/policy/foundry.rego`; run its tests
with the OPA CLI:

```bash
opa test src/foundry/policy
```

The two backends are held to the same behaviour (`tests/test_policy_engine.py`
mirrors `foundry_test.rego`).

## Project layout

```
src/foundry/
  schemas/        artifact contracts (+ shared enums in common.py)
  engines/        analyzer.py, enrichment.py, risk.py, planner.py
  orchestrator.py the run state machine wiring it all together
  policy/         engine.py (Local + OPA), foundry.rego, foundry_test.rego
  agents/         provider.py, manual.py, cursor.py, registry.py
  connectors/     base.py (IssueTracker), linear.py, comments.py
  db/             base.py, models.py
  audit/          events.py
  api/            app.py, security.py, mapping.py
tests/            one module per package
```
