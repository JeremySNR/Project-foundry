// End-to-end orchestrator tests using deterministic engines + a fake provider
// (mirrors test_orchestrator.py).

using Foundry.Agents;
using Foundry.Connectors;
using Foundry.Db;
using Foundry.Orchestration;
using Foundry.Schemas;
using Xunit;

namespace Foundry.Tests;

public class OrchestratorTests : IDisposable
{
    private readonly FoundryDataStore _store = FoundryDataStore.InMemory();

    public void Dispose() => _store.Dispose();

    private FoundryOrchestrator Orchestrator(
        CodingAgentProvider? provider = null,
        IIssueTracker? tracker = null,
        int maxFilesChanged = 12,
        IReadOnlyDictionary<string, IReadOnlyList<string>>? sensitivePathGlobs = null,
        int maxAgentRetries = 2,
        IReadOnlyList<string>? retryOn = null,
        double? maxCostPerRun = null) =>
        new(_store.CreateContext,
            provider: provider,
            issueTracker: tracker,
            maxFilesChanged: maxFilesChanged,
            sensitivePathGlobs: sensitivePathGlobs,
            maxAgentRetries: maxAgentRetries,
            retryOn: retryOn,
            maxCostPerRun: maxCostPerRun);

    private RunStatus Status(string runId)
    {
        using var session = _store.CreateContext();
        return session.Runs.Find(runId)!.Status;
    }

    private int JobCount(string runId)
    {
        using var session = _store.CreateContext();
        return session.AgentJobs.Count(j => j.RunId == runId);
    }

    private static PullRequestState Pr(
        string branch = "foundry/lin-123-add-customer-favourites",
        string title = "",
        PrStatus status = PrStatus.Open,
        CiStatus ciStatus = CiStatus.Unknown,
        ReviewStatus reviewStatus = ReviewStatus.None,
        IReadOnlyList<string>? filesChanged = null,
        string summary = "") => new()
    {
        Repo = "customer-web",
        PrNumber = 7,
        Url = "https://github.com/example/customer-web/pull/7",
        Branch = branch,
        Title = title,
        Status = status,
        CiStatus = ciStatus,
        ReviewStatus = reviewStatus,
        FilesChanged = filesChanged ?? new[] { "src/features/favourites/index.ts" },
        Summary = summary,
    };

    private (FoundryOrchestrator Orchestrator, string RunId, InMemoryFakeProvider Provider)
        DispatchedRun(
            IIssueTracker? tracker = null,
            int maxFilesChanged = 12,
            IReadOnlyDictionary<string, IReadOnlyList<string>>? sensitivePathGlobs = null,
            int maxAgentRetries = 2,
            IReadOnlyList<string>? retryOn = null,
            double? maxCostPerRun = null)
    {
        var provider = new InMemoryFakeProvider();
        var orchestrator = Orchestrator(
            provider: provider,
            tracker: tracker,
            maxFilesChanged: maxFilesChanged,
            sensitivePathGlobs: sensitivePathGlobs,
            maxAgentRetries: maxAgentRetries,
            retryOn: retryOn,
            maxCostPerRun: maxCostPerRun);
        var runId = orchestrator.IntakeAndPlan(TestData.ReadyTicket(), triggerType: "label");
        orchestrator.Approve(runId, user: "lead@example.com");
        var job = orchestrator.DispatchAgent(runId);
        provider.Run(job.JobId);
        return (orchestrator, runId, provider);
    }

    [Fact]
    public void ReadyTicketReachesWaitingApproval()
    {
        var runId = Orchestrator().IntakeAndPlan(TestData.ReadyTicket(), triggerType: "label");
        Assert.Equal(RunStatus.WaitingApproval, Status(runId));
    }

    [Fact]
    public void IntakePersistsAllArtifacts()
    {
        var runId = Orchestrator().IntakeAndPlan(TestData.ReadyTicket(), triggerType: "label");
        using var session = _store.CreateContext();
        var types = session.Artifacts
            .Where(a => a.RunId == runId)
            .Select(a => a.ArtifactType)
            .ToHashSet();
        Assert.Contains(ArtifactType.TicketSnapshot, types);
        Assert.Contains(ArtifactType.TicketAnalysis, types);
        Assert.Contains(ArtifactType.ContextBundle, types);
        Assert.Contains(ArtifactType.RiskAssessment, types);
        Assert.Contains(ArtifactType.DeliveryPlan, types);
        // A policy decision was recorded during intake.
        Assert.True(session.PolicyDecisions.Count(d => d.RunId == runId) >= 1);
    }

    [Fact]
    public void FullHappyPathToPrOpen()
    {
        var provider = new InMemoryFakeProvider();
        var orchestrator = Orchestrator(provider: provider);

        var runId = orchestrator.IntakeAndPlan(TestData.ReadyTicket(), triggerType: "label");
        orchestrator.Approve(runId, user: "lead@example.com");
        var job = orchestrator.DispatchAgent(runId);
        Assert.Equal(RunStatus.AgentRunning, Status(runId));

        // Simulate the agent finishing and opening a PR.
        var final = provider.Run(job.JobId);
        var pr = new PullRequestState
        {
            Repo = "customer-web",
            PrNumber = 1,
            Url = final.PrUrl!,
            Branch = final.Branch!,
            Status = PrStatus.Open,
            FilesChanged = new[] { "src/features/favourites/index.ts" },
        };
        Assert.Equal(RunStatus.PrOpen, orchestrator.RecordPr(runId, pr));

        using var session = _store.CreateContext();
        var jobRow = session.AgentJobs.Single(j => j.RunId == runId);
        Assert.Equal(final.PrUrl, jobRow.PrUrl);
    }

    [Fact]
    public void ForbiddenFileBlocksRun()
    {
        var (orchestrator, runId, _) = DispatchedRun();
        var pr = Pr(branch: "foundry/lin-123", filesChanged: new[] { "migrations/0002_add_table.sql" });
        Assert.Equal(RunStatus.Blocked, orchestrator.RecordPr(runId, pr));
    }

    [Fact]
    public void OversizedPrRequiresReview()
    {
        var (orchestrator, runId, _) = DispatchedRun(maxFilesChanged: 2);
        var pr = Pr(branch: "foundry/lin-123", filesChanged: new[] { "a.ts", "b.ts", "c.ts" });
        Assert.Equal(RunStatus.ReviewRequired, orchestrator.RecordPr(runId, pr));
    }

    [Fact]
    public void VagueTicketNeedsClarificationAndCannotDispatch()
    {
        var orchestrator = Orchestrator();
        var runId = orchestrator.IntakeAndPlan(
            new RawTicket { IssueId = "i", IssueKey = "LIN-9", Title = "Make it nicer" },
            triggerType: "comment_command");
        Assert.Equal(RunStatus.NeedsClarification, Status(runId));
        // Cannot approve a run that is not awaiting approval.
        Assert.Throws<OrchestratorException>(() => orchestrator.Approve(runId, user: "lead@example.com"));
    }

    [Fact]
    public void AuthChangeIsHumanOnlyAndBlocksDispatch()
    {
        var orchestrator = Orchestrator(provider: new InMemoryFakeProvider());
        var ticket = TestData.ReadyTicket(
            title: "Rotate auth login session tokens",
            description: "Acceptance Criteria:\n- auth tokens rotate\n- login still works");
        var runId = orchestrator.IntakeAndPlan(ticket, triggerType: "label");
        // High-risk auth work is still planned and awaits approval...
        Assert.Equal(RunStatus.WaitingApproval, Status(runId));
        orchestrator.Approve(runId, user: "lead@example.com",
            grantedRoles: new HashSet<ApprovalRole> { ApprovalRole.Engineering });
        // ...but the policy gate keeps auth changes human-only, so dispatch is blocked.
        Assert.Throws<OrchestratorException>(() => orchestrator.DispatchAgent(runId));
        Assert.Equal(RunStatus.Blocked, Status(runId));
    }

    [Fact]
    public void DispatchRequiresApprovalFirst()
    {
        var orchestrator = Orchestrator(provider: new InMemoryFakeProvider());
        var runId = orchestrator.IntakeAndPlan(TestData.ReadyTicket(), triggerType: "label");
        Assert.Throws<OrchestratorException>(() => orchestrator.DispatchAgent(runId));
    }

    [Fact]
    public void TrackerReceivesCommentAndStateOnIntake()
    {
        var tracker = new InMemoryIssueTracker();
        var orchestrator = Orchestrator(tracker: tracker);
        orchestrator.IntakeAndPlan(TestData.ReadyTicket(), triggerType: "label");
        Assert.Single(tracker.Comments["i-1"]);
        Assert.Contains("Foundry analysis complete", tracker.Comments["i-1"][0]);
        Assert.Equal("Foundry: Waiting Approval", tracker.States["i-1"]);
    }

    [Fact]
    public void TrackerStateFollowsRunThroughToPr()
    {
        var tracker = new InMemoryIssueTracker();
        var provider = new InMemoryFakeProvider();
        var orchestrator = Orchestrator(provider: provider, tracker: tracker);
        var runId = orchestrator.IntakeAndPlan(TestData.ReadyTicket(), triggerType: "label");
        orchestrator.Approve(runId, user: "lead@example.com");
        Assert.Equal("Foundry: Approved", tracker.States["i-1"]);
        var job = orchestrator.DispatchAgent(runId);
        Assert.Equal("Foundry: Agent Running", tracker.States["i-1"]);
        provider.Run(job.JobId);
        orchestrator.RecordPr(runId, Pr(branch: "foundry/lin-123", filesChanged: new[] { "src/x.ts" }));
        Assert.Equal("Foundry: PR Open", tracker.States["i-1"]);
    }

    // -- PR lifecycle: guardrails re-run on every push ------------------------------

    [Fact]
    public void PrUpdateAfterOpenIsRecordedNotRejected()
    {
        var (orchestrator, runId, _) = DispatchedRun();
        Assert.Equal(RunStatus.PrOpen, orchestrator.RecordPr(runId, Pr()));
        // A second (synchronize) event must not raise.
        Assert.Equal(RunStatus.PrOpen, orchestrator.RecordPr(runId, Pr()));
    }

    [Fact]
    public void ForbiddenFilePushedAfterOpenBlocksRun()
    {
        // An agent cannot open a clean PR and sneak forbidden files in later.
        var (orchestrator, runId, _) = DispatchedRun();
        Assert.Equal(RunStatus.PrOpen, orchestrator.RecordPr(runId, Pr()));
        var latePush = Pr(filesChanged: new[] { "src/ok.ts", "migrations/0042_drop_users.sql" });
        Assert.Equal(RunStatus.Blocked, orchestrator.RecordPr(runId, latePush));
        // Blocked is sticky: further events are refused, a human must intervene.
        Assert.Throws<OrchestratorException>(() => orchestrator.RecordPr(runId, Pr()));
    }

    [Fact]
    public void EventlessUpdateDoesNotWeakenReviewRequired()
    {
        var (orchestrator, runId, _) = DispatchedRun();
        var big = Pr(filesChanged: Enumerable.Range(0, 20).Select(i => $"src/f{i}.ts").ToList());
        Assert.Equal(RunStatus.ReviewRequired, orchestrator.RecordPr(runId, big));
        // A review/CI event carries no file list; the file-based decision stands.
        var noFiles = Pr(filesChanged: Array.Empty<string>());
        Assert.Equal(RunStatus.ReviewRequired, orchestrator.RecordPr(runId, noFiles));
    }

    [Fact]
    public void PrShrinkingBackUnderLimitRecovers()
    {
        var (orchestrator, runId, _) = DispatchedRun();
        var big = Pr(filesChanged: Enumerable.Range(0, 20).Select(i => $"src/f{i}.ts").ToList());
        Assert.Equal(RunStatus.ReviewRequired, orchestrator.RecordPr(runId, big));
        Assert.Equal(RunStatus.PrOpen, orchestrator.RecordPr(runId, Pr()));
    }

    [Fact]
    public void MergedPrCompletesRunAndJob()
    {
        var (orchestrator, runId, _) = DispatchedRun();
        orchestrator.RecordPr(runId, Pr());
        Assert.Equal(RunStatus.Complete, orchestrator.RecordPr(runId, Pr(status: PrStatus.Merged)));
        using var session = _store.CreateContext();
        var jobRow = session.AgentJobs.Single(j => j.RunId == runId);
        Assert.NotNull(jobRow.CompletedAt);
    }

    [Fact]
    public void ClosedUnmergedPrBlocksRun()
    {
        var (orchestrator, runId, _) = DispatchedRun();
        orchestrator.RecordPr(runId, Pr());
        Assert.Equal(RunStatus.Blocked, orchestrator.RecordPr(runId, Pr(status: PrStatus.Closed)));
    }

    // -- diff-aware risk -------------------------------------------------------------

    [Fact]
    public void DiffTouchingUnflaggedSensitiveAreaEscalates()
    {
        // The ticket said 'favourites'; the diff touched auth. Escalate.
        var (orchestrator, runId, _) = DispatchedRun();
        var sneaky = Pr(filesChanged: new[] { "src/auth/session_handler.ts" });
        Assert.Equal(RunStatus.ReviewRequired, orchestrator.RecordPr(runId, sneaky));
    }

    [Fact]
    public void DiffInAnticipatedSensitiveAreaDoesNotEscalate()
    {
        // An area the upfront risk pass flagged was already approved by a human.
        var provider = new InMemoryFakeProvider();
        var orchestrator = Orchestrator(provider: provider);
        var ticket = TestData.ReadyTicket(
            title: "Tune the helm chart resource limits",
            description: TestData.ReadyDescription
                + "\nAdjust the helm chart for the favourites service.");
        var runId = orchestrator.IntakeAndPlan(ticket, triggerType: "label");
        // Infrastructure risk (medium) requires an engineering-role approval.
        orchestrator.Approve(runId, user: "lead@example.com",
            grantedRoles: new HashSet<ApprovalRole> { ApprovalRole.Engineering });
        var job = orchestrator.DispatchAgent(runId);
        provider.Run(job.JobId);
        // The diff touches infrastructure paths - anticipated, approved, no escalation.
        var pr = Pr(filesChanged: new[] { "deploy/helm/values.yaml" });
        Assert.Equal(RunStatus.PrOpen, orchestrator.RecordPr(runId, pr));
    }

    [Fact]
    public void CustomSensitiveGlobsAreHonoured()
    {
        var (orchestrator, runId, _) = DispatchedRun(
            sensitivePathGlobs: new Dictionary<string, IReadOnlyList<string>>
            {
                ["payments"] = new[] { "**/money/**" },
            });
        var pr = Pr(filesChanged: new[] { "src/money/charge.ts" });
        Assert.Equal(RunStatus.ReviewRequired, orchestrator.RecordPr(runId, pr));
    }

    // -- PR correlation ---------------------------------------------------------------

    [Fact]
    public void CorrelatePrByExactBranch()
    {
        var (orchestrator, runId, _) = DispatchedRun();
        Assert.Equal(runId, orchestrator.CorrelatePr(Pr()));
    }

    [Fact]
    public void CorrelatePrByIssueKeyInBranch()
    {
        var (orchestrator, runId, _) = DispatchedRun();
        var pr = Pr(branch: "cursor/lin-123-something-cursor-chose");
        Assert.Equal(runId, orchestrator.CorrelatePr(pr));
    }

    [Fact]
    public void CorrelatePrByIssueKeyInTitle()
    {
        var (orchestrator, runId, _) = DispatchedRun();
        var pr = Pr(branch: "opaque-name", title: "LIN-123: add favourites");
        Assert.Equal(runId, orchestrator.CorrelatePr(pr));
    }

    [Fact]
    public void CorrelatePrNoMatchReturnsNull()
    {
        var (orchestrator, _, _) = DispatchedRun();
        var pr = Pr(branch: "other-branch", title: "OTHER-9 unrelated");
        Assert.Null(orchestrator.CorrelatePr(pr));
    }

    // -- governed remediation loop ----------------------------------------------------

    [Fact]
    public void CiFailureRedispatchesAgent()
    {
        var (orchestrator, runId, _) = DispatchedRun();
        Assert.Equal(RunStatus.PrOpen, orchestrator.RecordPr(runId, Pr()));

        var failing = Pr(
            filesChanged: Array.Empty<string>(),
            ciStatus: CiStatus.Failing,
            summary: "pytest: 2 failed");
        Assert.Equal(RunStatus.AgentRunning, orchestrator.RecordPr(runId, failing));
        // A second agent job was dispatched for the remediation.
        Assert.Equal(2, JobCount(runId));
    }

    [Fact]
    public void RemediationJobTargetsSameBranchWithContext()
    {
        var (orchestrator, runId, provider) = DispatchedRun();
        orchestrator.RecordPr(runId, Pr(branch: "cursor/lin-123-own-branch"));

        var failing = Pr(
            branch: "cursor/lin-123-own-branch",
            ciStatus: CiStatus.Failing,
            summary: "- unit tests: 2 failed");
        orchestrator.RecordPr(runId, failing);
        var remediationInput = provider.Inputs[^1];
        // Same branch the agent actually used, not a fresh Foundry-named one.
        Assert.Equal("cursor/lin-123-own-branch", remediationInput.BranchName);
        Assert.Contains("REMEDIATION REQUEST", remediationInput.AgentInstructions);
        Assert.Contains("- unit tests: 2 failed", remediationInput.AgentInstructions);
    }

    [Fact]
    public void RemediationCapParksRunForHumans()
    {
        var tracker = new InMemoryIssueTracker();
        var (orchestrator, runId, _) = DispatchedRun(tracker: tracker, maxAgentRetries: 1);
        orchestrator.RecordPr(runId, Pr());

        var failing = Pr(ciStatus: CiStatus.Failing);
        // Attempt 1: within the cap, re-dispatches.
        Assert.Equal(RunStatus.AgentRunning, orchestrator.RecordPr(runId, failing));
        // The agent pushes again, PR re-opens, CI fails again.
        Assert.Equal(RunStatus.PrOpen, orchestrator.RecordPr(runId, Pr()));
        // Attempt 2: over the cap -> denied by policy, parked for review.
        Assert.Equal(RunStatus.ReviewRequired, orchestrator.RecordPr(runId, failing));
        Assert.Equal(2, JobCount(runId)); // no third dispatch
        // And a human-readable comment landed on the issue.
        Assert.Contains(tracker.Comments["i-1"], c => c.Contains("could not remediate"));
    }

    [Fact]
    public void ChangesRequestedReviewTriggersRemediation()
    {
        var (orchestrator, runId, _) = DispatchedRun();
        orchestrator.RecordPr(runId, Pr());
        var review = Pr(filesChanged: Array.Empty<string>(), reviewStatus: ReviewStatus.ChangesRequested);
        Assert.Equal(RunStatus.AgentRunning, orchestrator.RecordPr(runId, review));
    }

    [Fact]
    public void RetryOnConfigDisablesRemediation()
    {
        var (orchestrator, runId, _) = DispatchedRun(retryOn: Array.Empty<string>());
        orchestrator.RecordPr(runId, Pr());
        var failing = Pr(ciStatus: CiStatus.Failing);
        // Remediation disabled: CI failure is recorded but the run stays PrOpen.
        Assert.Equal(RunStatus.PrOpen, orchestrator.RecordPr(runId, failing));
        Assert.Equal(1, JobCount(runId));
    }

    [Fact]
    public void BudgetCapDeniesRemediation()
    {
        var tracker = new InMemoryIssueTracker();
        var (orchestrator, runId, _) = DispatchedRun(tracker: tracker, maxCostPerRun: 5.0);
        orchestrator.RecordPr(runId, Pr());
        // The first job already burned through the budget.
        using (var session = _store.CreateContext())
        {
            var job = session.AgentJobs.Single(j => j.RunId == runId);
            job.CostUsd = 6.0;
            session.SaveChanges();
        }

        var failing = Pr(ciStatus: CiStatus.Failing);
        Assert.Equal(RunStatus.ReviewRequired, orchestrator.RecordPr(runId, failing));
        Assert.Equal(1, JobCount(runId)); // no re-dispatch
        Assert.Contains(tracker.Comments["i-1"], c => c.Contains("budget cap"));
    }

    [Fact]
    public void RemediationAllowedWhenUnderBudget()
    {
        var (orchestrator, runId, _) = DispatchedRun(maxCostPerRun: 50.0);
        orchestrator.RecordPr(runId, Pr());
        using (var session = _store.CreateContext())
        {
            var job = session.AgentJobs.Single(j => j.RunId == runId);
            job.CostUsd = 6.0;
            session.SaveChanges();
        }
        var failing = Pr(ciStatus: CiStatus.Failing);
        Assert.Equal(RunStatus.AgentRunning, orchestrator.RecordPr(runId, failing));
    }

    [Fact]
    public void NoRemediationForForbiddenPathBlock()
    {
        // Blocked is sticky; remediation never resurrects a forbidden-path block.
        var (orchestrator, runId, _) = DispatchedRun();
        var bad = Pr(filesChanged: new[] { "migrations/0001_drop.sql" }, ciStatus: CiStatus.Failing);
        Assert.Equal(RunStatus.Blocked, orchestrator.RecordPr(runId, bad));
        Assert.Equal(1, JobCount(runId));
    }

    // -- one active run per issue ------------------------------------------------------

    [Fact]
    public void SecondIntakeForActiveIssueIsRefused()
    {
        var orchestrator = Orchestrator();
        orchestrator.IntakeAndPlan(TestData.ReadyTicket(), triggerType: "label");
        Assert.Throws<OrchestratorException>(
            () => orchestrator.IntakeAndPlan(TestData.ReadyTicket(), triggerType: "label"));
    }

    [Fact]
    public void RejectedIssueCanBeReanalysed()
    {
        var orchestrator = Orchestrator();
        var first = orchestrator.IntakeAndPlan(TestData.ReadyTicket(), triggerType: "label");
        orchestrator.Reject(first, user: "lead@example.com");
        var second = orchestrator.IntakeAndPlan(TestData.ReadyTicket(), triggerType: "label");
        Assert.NotEqual(first, second);
        Assert.Equal(RunStatus.WaitingApproval, Status(second));
    }

    [Fact]
    public void CursorViaLinearDelegationEndToEnd()
    {
        // Foundry governs, then hands the approved work to Cursor via a Linear comment.
        var tracker = new InMemoryIssueTracker();
        var orchestrator = Orchestrator(
            provider: new CursorViaLinearProvider(tracker),
            tracker: tracker);
        var runId = orchestrator.IntakeAndPlan(TestData.ReadyTicket(), triggerType: "label");
        orchestrator.Approve(runId, user: "lead@example.com");
        orchestrator.DispatchAgent(runId);
        // An @Cursor delegation comment was posted in addition to the analysis comment.
        Assert.Contains(tracker.Comments["i-1"], c => c.StartsWith("@Cursor"));
        Assert.Equal(RunStatus.AgentRunning, Status(runId));
    }
}
