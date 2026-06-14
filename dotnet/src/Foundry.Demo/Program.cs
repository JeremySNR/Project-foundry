// The Foundry demo: the whole governed loop, offline, in about a minute.
//
// No credentials, no network, no Docker - an in-memory database, the fake
// coding-agent provider and an in-memory issue tracker. Every stage you see is
// the real production code path (the same orchestrator, policy engine and
// audit trail the webhooks drive); only the external services are stand-ins.
//
//     dotnet run --project src/Foundry.Demo            # run it
//     dotnet run --project src/Foundry.Demo -- --slow  # dramatic pacing

using System.Text.Json;
using Foundry.Agents;
using Foundry.Connectors;
using Foundry.Db;
using Foundry.Orchestration;
using Foundry.Schemas;

// -- terminal dressing ---------------------------------------------------------

const string Bold = "\x1b[1m";
const string Dim = "\x1b[2m";
const string Reset = "\x1b[0m";
const string Green = "\x1b[32m";
const string Red = "\x1b[31m";
const string Amber = "\x1b[33m";
const string Blue = "\x1b[34m";
const string Purple = "\x1b[35m";

var delay = args.Contains("--slow") ? TimeSpan.FromMilliseconds(350) : TimeSpan.Zero;

var statusColour = new Dictionary<RunStatus, string>
{
    [RunStatus.NeedsClarification] = Amber,
    [RunStatus.WaitingApproval] = Amber,
    [RunStatus.AgentRunning] = Purple,
    [RunStatus.PrOpen] = Blue,
    [RunStatus.ReviewRequired] = Amber,
    [RunStatus.Blocked] = Red,
    [RunStatus.Complete] = Green,
};

void Say(string text = "")
{
    Console.WriteLine(text);
    if (delay > TimeSpan.Zero)
    {
        Thread.Sleep(delay);
    }
}

void Act(string title)
{
    Say();
    Say($"{Bold}{new string('=', 74)}{Reset}");
    Say($"{Bold}  {title}{Reset}");
    Say($"{Bold}{new string('=', 74)}{Reset}");
}

void ShowStatus(string label, RunStatus status)
{
    var colour = statusColour.GetValueOrDefault(status, "");
    Say($"{label}: {colour}{Bold}{status.ToWire()}{Reset}");
}

void ShowComment(InMemoryIssueTracker tracker, string issueId)
{
    var body = tracker.Comments[issueId][^1];
    Say($"{Dim}--- comment posted to the ticket {new string('-', 40)}{Reset}");
    foreach (var line in body.Split('\n'))
    {
        Say($"{Dim}| {line}{Reset}");
    }
    Say($"{Dim}{new string('-', 73)}{Reset}");
}

using var store = FoundryDataStore.InMemory();

void ShowLastDecision(string runId)
{
    using var session = store.CreateContext();
    var decision = session.PolicyDecisions
        .Where(d => d.RunId == runId)
        .OrderByDescending(d => d.CreatedAt)
        .FirstOrDefault();
    if (decision is null)
    {
        return;
    }
    var verdict = decision.Allowed ? $"{Green}ALLOWED{Reset}" : $"{Red}DENIED{Reset}";
    using var input = JsonDocument.Parse(decision.InputJson);
    var action = input.RootElement.TryGetProperty("action", out var actionProperty)
        ? actionProperty.GetString()
        : decision.PolicyName;
    Say($"policy gate [{action}] -> {verdict}");
    var colour = decision.Allowed ? Dim : Red;
    using var decisionJson = JsonDocument.Parse(decision.DecisionJson);
    if (decisionJson.RootElement.TryGetProperty("reasons", out var reasons))
    {
        foreach (var reason in reasons.EnumerateArray())
        {
            Say($"  {colour}- {reason.GetString()}{Reset}");
        }
    }
}

var provider = new InMemoryFakeProvider();
var tracker = new InMemoryIssueTracker();
var orchestrator = new FoundryOrchestrator(
    store.CreateContext, provider: provider, issueTracker: tracker, maxAgentRetries: 2);

Say($"{Bold}Project Foundry{Reset} - raw tickets in, reviewed PRs out. (C#/.NET port)");
Say($"{Dim}(everything below is the real code path; only the external services are fakes){Reset}");

// -- Act 1: a thin ticket gets bounced, helpfully --------------------------
Act("Act 1 - A vague ticket does not get built");
Say("PM files: \"Add customer favourites\" with no acceptance criteria.");
var thin = new RawTicket
{
    IssueId = "demo-1",
    IssueKey = "LIN-101",
    Title = "Add customer favourites",
    Description = "Customers want to favourite items.",
};
var run1 = orchestrator.IntakeAndPlan(thin, triggerType: "label");
ShowStatus("run", orchestrator.GetRun(run1)!.Status);
Say("Foundry does not reject it - it drafts the acceptance criteria for you:");
ShowComment(tracker, "demo-1");

// -- Act 2: the improved ticket reaches the human gate ---------------------
Act("Act 2 - The improved ticket gets analysed, planned and gated");
Say("The PM pastes the criteria in and adds the repo label. Re-trigger:");
var ready = new RawTicket
{
    IssueId = "demo-1",
    IssueKey = "LIN-101",
    Title = "Add customer favourites",
    Description = "Customers want to favourite items.\n\n"
        + "Acceptance Criteria:\n"
        + "- A favourites button exists on every item card\n"
        + "- Favourites persist across sessions\n",
    KnownRepositories = new[] { "customer-web" },
};
var runId = orchestrator.IntakeAndPlan(ready, triggerType: "label");
var run = orchestrator.GetRun(runId)!;
ShowStatus("run", run.Status);
Say($"risk: {Bold}{run.RiskLevel?.ToWire()}{Reset}  agent mode: {Bold}{run.AgentMode?.ToWire()}{Reset}");
Say("Nothing dispatches without a human. The plan is on the ticket:");
ShowComment(tracker, "demo-1");

// -- Act 3: approval + governed dispatch -----------------------------------
Act("Act 3 - A human approves; the policy gate decides; the agent runs");
Say("lead@example.com comments \"/foundry approve\" ...");
orchestrator.Approve(runId, user: "lead@example.com");
var job = orchestrator.DispatchAgent(runId);
ShowLastDecision(runId);
ShowStatus("run", orchestrator.GetRun(runId)!.Status);
Say($"agent job {Dim}{job.JobId}{Reset} dispatched (instructions passed the secret-leak guard).");
provider.Run(job.JobId);

PullRequestState Pr(
    PrStatus status = PrStatus.Open,
    CiStatus ciStatus = CiStatus.Pending,
    ReviewStatus reviewStatus = ReviewStatus.None,
    IReadOnlyList<string>? filesChanged = null,
    string summary = "") => new()
{
    Repo = "customer-web",
    PrNumber = 42,
    Url = "https://github.com/example/customer-web/pull/42",
    Branch = "foundry/lin-101-add-customer-favourites",
    Status = status,
    CiStatus = ciStatus,
    ReviewStatus = reviewStatus,
    FilesChanged = filesChanged
        ?? new[] { "src/components/Favourites.tsx", "src/api/favourites.ts" },
    Summary = summary,
};

Say("The agent opens PR #42. Foundry verifies the diff against the guardrails:");
ShowStatus("run", orchestrator.RecordPr(runId, Pr()));

// -- Act 4: CI fails; the agent fixes its own PR, under governance ---------
Act("Act 4 - CI fails; Foundry re-dispatches the agent with the failure");
Say("The check suite comes back red:");
var failing = Pr(
    filesChanged: Array.Empty<string>(),
    ciStatus: CiStatus.Failing,
    summary: "- unit tests: FavouritesButton renders twice on item cards");
var statusAfterFailure = orchestrator.RecordPr(runId, failing);
ShowLastDecision(runId);
ShowStatus("run", statusAfterFailure);
Say("Same branch, original plan + failure excerpt, attempt counted against the retry cap.");

// -- Act 5: green, reviewed, merged -----------------------------------------
Act("Act 5 - Green CI, human review, merge, done");
orchestrator.RecordPr(runId, Pr()); // agent pushed the fix; PR re-opens clean
orchestrator.RecordPr(runId, Pr(
    filesChanged: Array.Empty<string>(),
    ciStatus: CiStatus.Passing,
    reviewStatus: ReviewStatus.Approved));
var final = orchestrator.RecordPr(runId, Pr(status: PrStatus.Merged));
ShowStatus("run", final);
Say("Linear was kept in sync the whole way. No auto-merge happened: a human clicked the button.");

// -- Act 6: what getting stopped looks like ---------------------------------
Act("Act 6 - And this is what the brakes feel like");
Say("A second ticket; this time the agent's PR sneaks in a file under migrations/ :");
var risky = new RawTicket
{
    IssueId = "demo-2",
    IssueKey = "LIN-102",
    Title = "Tidy up the favourites schema",
    Description = "Small cleanup.\n\nAcceptance Criteria:\n- Schema fields renamed\n",
    KnownRepositories = new[] { "customer-web" },
};
var run2 = orchestrator.IntakeAndPlan(risky, triggerType: "label");
orchestrator.Approve(run2, user: "lead@example.com");
var job2 = orchestrator.DispatchAgent(run2);
provider.Run(job2.JobId);
var blocked = orchestrator.RecordPr(run2, new PullRequestState
{
    Repo = "customer-web",
    PrNumber = 43,
    Url = "https://github.com/example/customer-web/pull/43",
    Branch = "foundry/lin-102-tidy-up-the-favourites-schema",
    Status = PrStatus.Open,
    FilesChanged = new[] { "src/models.py", "migrations/0007_rename.sql" },
});
ShowStatus("run", blocked);
Say($"{Red}Forbidden path touched -> blocked, audited, human required. No retry resurrects it.{Reset}");

// -- epilogue: the receipts --------------------------------------------------
Act("The receipts - every decision is written down");
using (var session = store.CreateContext())
{
    var events = session.AuditEvents
        .Where(e => e.RunId == runId)
        .OrderBy(e => e.Sequence)
        .ToList();
    Say($"audit trail for {ready.IssueKey} ({events.Count} events):");
    foreach (var auditEvent in events)
    {
        Say($"  #{auditEvent.Sequence,-3} {auditEvent.EventType.ToWire(),-32} {Dim}{auditEvent.ActorType}{Reset}");
    }
    var decisions = session.PolicyDecisions.Count(d => d.RunId == runId);
    Say($"plus {decisions} policy decisions with full inputs and reasons, "
        + "content-hashed artifacts, and the same audit model as the Python implementation.");
}
Say();
Say($"{Bold}{Green}Raw ore in, beskar out.{Reset} {Dim}Same loop, now in C#.{Reset}");
return 0;
