# Project Foundry - C#/.NET port

A faithful C#/.NET 8 port of the Foundry core: the governance layer for AI
coding agents. Raw tickets in, reviewed pull requests out, every decision
policy-gated and audited. Same contracts, same hard rules, same wire format -
in the stack the team already lives in.

## See it run (60 seconds, zero setup)

```bash
cd dotnet
dotnet run --project src/Foundry.Demo          # add `-- --slow` for screen recording
```

No credentials, no network, no Docker. The demo drives the real production
code path - orchestrator, policy engine, audit trail - through the whole
story: a vague ticket gets bounced with drafted acceptance criteria, the
improved ticket gets planned and gated, a human approves, the agent's PR fails
CI and gets fixed by a governed retry, the PR merges, and a second run gets
hard-blocked for touching `migrations/`. It ends with the receipts.

## Run the tests

```bash
cd dotnet
dotnet test
```

The xUnit suite mirrors the Python tests case-for-case (policy gate, engines,
orchestrator lifecycle, governed remediation, audit sequencing, secret-leak
guard, configuration layering).

## Layout

| Project | What it is |
| --- | --- |
| `src/Foundry` | The core library (everything below). |
| `src/Foundry.Demo` | The offline end-to-end demo console. |
| `tests/Foundry.Tests` | xUnit suite mirroring the Python `tests/`. |

## What is ported (and from where)

| C# namespace | Python module | Notes |
| --- | --- | --- |
| `Foundry.Schemas` | `foundry.schemas` | Records with init-validation; snake_case JSON identical to the Pydantic wire format; unknown fields rejected on deserialise (Pydantic's `extra="forbid"`). |
| `Foundry.Policy` | `foundry.policy.engine` | `LocalPolicyEngine` - same default-deny rules, forbidden actions, retry cap and budget cap, same reason strings. An OPA-backed engine slots in behind `IPolicyEngine`. |
| `Foundry.Audit` | `foundry.audit.events` | Canonical-JSON SHA-256 content hashing, artifact/audit/policy row builders. |
| `Foundry.Db` | `foundry.db` | EF Core + SQLite (in-memory or file); same five tables and column names; monotonic per-run audit sequences assigned at `SaveChanges`, exactly like the SQLAlchemy flush hook. |
| `Foundry.Engines` | `foundry.engines` | `HeuristicAnalyzer`, `StaticContextEnricher`, `HeuristicRiskClassifier`, `TemplatePlanner`, Python-`fnmatch`-compatible globbing, plus the `IStructuredLlm` seam with `FakeStructuredLlm` and `LlmTicketAnalyzer`. |
| `Foundry.Agents` | `foundry.agents` | `CodingAgentProvider` base with the secret-leak guard, `ManualProvider`, `InMemoryFakeProvider`, `WebhookProvider` (HMAC-signed), `ClaudeCodeProvider`, `CursorViaLinearProvider`, `CursorCloudAgentProvider`, registry. |
| `Foundry.Connectors` | `foundry.connectors.base/comments` | `IIssueTracker` seam, in-memory fake, tracker comment/state rendering. |
| `Foundry.Orchestration` | `foundry.orchestrator`, `foundry.drivers` | The full run state machine: intake -> gate -> approve -> dispatch -> PR monitoring, diff-aware risk escalation, governed remediation loop with retry and budget caps, PR correlation; `InlineDriver`. |
| `Foundry.Configuration` | `foundry.config` | Same YAML shape and `FOUNDRY_*` environment overrides, same validation. |
| `Foundry.Observability` | `foundry.observability` | `ActivitySource` ("foundry") - the .NET-native OpenTelemetry seam; free when nothing listens. |

### Wire compatibility

Artifacts, policy decisions and audit rows serialise to the same snake_case
JSON with the same canonical form (sorted keys, compact separators), and the
database schema uses the same table/column names and enum strings as the
Python implementation - so the two implementations can read each other's
data and dashboards.

## Not ported (yet)

Deliberately left for a follow-up, in rough priority order:

- **`foundry.api`** (FastAPI app: signed Linear/GitHub/Jira/GitLab webhooks,
  approval commands, run timeline, dashboard) -> ASP.NET Core minimal APIs.
  The orchestrator/driver seam it sits on is fully ported.
- **`foundry.connectors`** live HTTP adapters (Linear/GitHub/Jira/GitLab
  transports) - the `IIssueTracker` seam and fakes are here; the thin HTTP
  clients are not.
- **`foundry.workflows`** (Temporal durable execution) - Temporal has an
  official .NET SDK; a `TemporalDriver` implements the existing `IRunDriver`.
- **`OpenAIStructuredLLM`** live transport - implement `IStructuredLlm` over
  the OpenAI .NET SDK; `LlmTicketAnalyzer` (validation, retry-with-feedback,
  identity overwrite) is already ported. Note: Pydantic derives the JSON
  schema from the model; here strict deserialisation enforces the contract
  instead.
- **OPA HTTP engine** - implement `IPolicyEngine` over the shared
  `foundry.rego` bundle (the bundle itself is language-agnostic and lives in
  the repository root).
