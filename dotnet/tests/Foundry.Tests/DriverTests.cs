// Tests for the InlineDriver run-execution seam (mirrors test_drivers.py).

using Foundry.Agents;
using Foundry.Db;
using Foundry.Orchestration;
using Foundry.Schemas;
using Xunit;

namespace Foundry.Tests;

public class DriverTests : IDisposable
{
    private readonly FoundryDataStore _store = FoundryDataStore.InMemory();
    private readonly FoundryOrchestrator _orchestrator;
    private readonly InlineDriver _driver;

    public DriverTests()
    {
        _orchestrator = new FoundryOrchestrator(_store.CreateContext, provider: new InMemoryFakeProvider());
        _driver = new InlineDriver(_orchestrator);
    }

    public void Dispose() => _store.Dispose();

    private static RawTicket ReadyTicket(string? title = null, string? description = null) =>
        TestData.ReadyTicket(
            title: title ?? "Add customer favourites",
            description: description ?? "Acceptance Criteria:\n- A button exists\n- Favourites persist");

    [Fact]
    public void StartThenApproveDispatches()
    {
        var runId = _driver.Start(ReadyTicket(), triggerType: "label");
        Assert.Equal(RunStatus.WaitingApproval, _orchestrator.GetRun(runId)!.Status);

        _driver.SubmitDecision(runId, decision: "approve", user: "lead@example.com");
        Assert.Equal(RunStatus.AgentRunning, _orchestrator.GetRun(runId)!.Status);
    }

    [Fact]
    public void ApproveHumanOnlyWorkEndsBlockedNotRaised()
    {
        var ticket = ReadyTicket(
            title: "Rotate auth login session tokens",
            description: "Acceptance Criteria:\n- auth tokens rotate\n- login works");
        var runId = _driver.Start(ticket, triggerType: "label");
        // Auth work is human-only: the driver swallows the policy block and the
        // run ends blocked rather than raising.
        _driver.SubmitDecision(runId, decision: "approve", user: "lead@example.com",
            roles: new HashSet<ApprovalRole> { ApprovalRole.Engineering });
        Assert.Equal(RunStatus.Blocked, _orchestrator.GetRun(runId)!.Status);
    }

    [Fact]
    public void Reject()
    {
        var runId = _driver.Start(ReadyTicket(), triggerType: "label");
        _driver.SubmitDecision(runId, decision: "reject", user: "lead@example.com");
        Assert.Equal(RunStatus.Rejected, _orchestrator.GetRun(runId)!.Status);
    }

    [Fact]
    public void ObservePrRecords()
    {
        var runId = _driver.Start(ReadyTicket(), triggerType: "label");
        _driver.SubmitDecision(runId, decision: "approve", user: "lead@example.com");
        _driver.ObservePr(runId, new PullRequestState
        {
            Repo = "customer-web",
            PrNumber = 1,
            Url = "https://github.com/o/customer-web/pull/1",
            Branch = "foundry/lin-123",
            Status = PrStatus.Open,
            FilesChanged = new[] { "src/x.ts" },
        });
        Assert.Equal(RunStatus.PrOpen, _orchestrator.GetRun(runId)!.Status);
    }

    [Fact]
    public void UnsupportedDecisionRaises()
    {
        var runId = _driver.Start(ReadyTicket(), triggerType: "label");
        Assert.Throws<ArgumentException>(
            () => _driver.SubmitDecision(runId, decision: "frobnicate", user: "lead@example.com"));
    }
}
