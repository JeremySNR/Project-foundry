// FoundryOrchestrator - drives a single Ticket-to-PR run.
//
// This is the connective tissue between the intelligence engines, the policy
// gate, the coding-agent providers and the data model. It is deliberately
// infrastructure-light: it persists artifacts/audit/policy rows through a
// DbContext factory and pauses at human approval. A durable workflow engine
// can later wrap these same steps as activities; LLM engines slot in behind
// the engine interfaces. None of this layer makes a network call.
//
// Lifecycle:
//
//     IntakeAndPlan(ticket)        // analyse -> enrich -> risk -> plan -> gate
//         -> WaitingApproval | NeedsClarification | Blocked
//     Approve(runId, ...)          // records approval, -> Approved
//     DispatchAgent(runId)         // re-checks policy, launches provider -> AgentRunning
//     RecordPr(runId, prState)     // PrOpen | ReviewRequired | Blocked

using System.Text.Json;
using System.Text.RegularExpressions;
using Foundry.Agents;
using Foundry.Connectors;
using Foundry.Db;
using Foundry.Engines;
using Foundry.Policy;
using Foundry.Schemas;

namespace Foundry.Orchestration;

/// <summary>Raised when a run cannot proceed (e.g. policy blocked dispatch).</summary>
public class OrchestratorException : Exception
{
    public OrchestratorException(string message) : base(message) { }
}

public sealed partial class FoundryOrchestrator
{
    // Run states in which PR webhook events are meaningful for the run.
    private static readonly IReadOnlySet<RunStatus> PrObservableStatuses = new HashSet<RunStatus>
    {
        RunStatus.AgentRunning,
        RunStatus.PrOpen,
        RunStatus.ReviewRequired,
    };

    // Linear-style issue keys (ENG-123) appearing in branch names or PR titles.
    [GeneratedRegex(@"\b([A-Za-z][A-Za-z0-9]{1,9}-\d+)\b")]
    private static partial Regex IssueKeyRegex();

    private readonly Func<FoundryDbContext> _contextFactory;
    private readonly ITicketAnalyzer _analyzer;
    private readonly IContextEnricher _enricher;
    private readonly IRiskClassifier _risk;
    private readonly IDeliveryPlanner _planner;
    private readonly IPolicyEngine _policy;
    private readonly CodingAgentProvider _provider;
    private readonly IIssueTracker? _tracker;
    private readonly int _maxFilesChanged;
    private readonly List<string> _forbiddenGlobs;
    private readonly Dictionary<string, IReadOnlyList<string>> _sensitivePathGlobs;
    private readonly int _maxAgentRetries;
    private readonly IReadOnlySet<string> _retryOn;
    private readonly double? _maxCostPerRun;

    public FoundryOrchestrator(
        Func<FoundryDbContext> contextFactory,
        ITicketAnalyzer? analyzer = null,
        IContextEnricher? enricher = null,
        IRiskClassifier? riskClassifier = null,
        IDeliveryPlanner? planner = null,
        IPolicyEngine? policyEngine = null,
        CodingAgentProvider? provider = null,
        IIssueTracker? issueTracker = null,
        int maxFilesChanged = 12,
        IReadOnlyList<string>? forbiddenGlobs = null,
        IReadOnlyDictionary<string, IReadOnlyList<string>>? sensitivePathGlobs = null,
        int maxAgentRetries = 2,
        IReadOnlyList<string>? retryOn = null,
        double? maxCostPerRun = null)
    {
        _contextFactory = contextFactory;
        _analyzer = analyzer ?? new HeuristicAnalyzer();
        _enricher = enricher ?? new StaticContextEnricher();
        _risk = riskClassifier ?? new HeuristicRiskClassifier();
        _planner = planner ?? new TemplatePlanner();
        _policy = policyEngine ?? new LocalPolicyEngine();
        _provider = provider ?? new ManualProvider();
        // Optional: when set, Foundry writes progress/state back to the tracker.
        _tracker = issueTracker;
        _maxFilesChanged = maxFilesChanged;
        _forbiddenGlobs = (forbiddenGlobs ?? Planning.DefaultForbiddenGlobs).ToList();
        _sensitivePathGlobs = new Dictionary<string, IReadOnlyList<string>>(
            sensitivePathGlobs ?? Configuration.Settings.DefaultSensitivePathGlobs);
        _maxAgentRetries = maxAgentRetries;
        _retryOn = new HashSet<string>(retryOn ?? new[] { "ci_failed", "changes_requested" });
        _maxCostPerRun = maxCostPerRun;
    }

    // -- intake + planning ----------------------------------------------------

    /// <summary>Run analysis -> context -> risk -> plan -> policy gate; persist all.</summary>
    public string IntakeAndPlan(RawTicket ticket, string triggerType, string? createdBy = null)
    {
        using var span = Observability.Span("foundry.intake_and_plan");
        // At most one *active* run per issue; finished/blocked runs may be
        // superseded by a fresh trigger (e.g. after the ticket is clarified).
        var active = FindActiveRunIdForIssue(ticket.IssueId);
        if (active is not null)
        {
            throw new OrchestratorException(
                $"issue {ticket.IssueId} already has an active run ({active})");
        }
        var runId = Audit.Events.NewId("run");
        var analysis = _analyzer.Analyse(ticket);
        var context = _enricher.Enrich(ticket, analysis);
        var risk = _risk.Classify(ticket, analysis, context);
        var plan = _planner.Plan(ticket, analysis, context, risk);
        var payload = BuildPolicyInput(PolicyAction.StartAgent, analysis, context, risk);
        var decision = _policy.Evaluate(payload);

        var status = PostPlanStatus(analysis, risk);

        using (var session = _contextFactory())
        {
            var run = new FoundryRun
            {
                Id = runId,
                LinearIssueId = ticket.IssueId,
                LinearIssueKey = ticket.IssueKey,
                Status = RunStatus.Analysing,
                TriggerType = triggerType,
                CreatedBy = createdBy,
                CurrentStep = "intake",
                RiskLevel = risk.OverallRisk,
                AgentMode = decision.AllowedAgentMode,
            };
            session.Runs.Add(run);
            AddArtifact(session, runId, ArtifactType.TicketSnapshot, ticket);
            AddArtifact(session, runId, ArtifactType.TicketAnalysis, analysis);
            AddArtifact(session, runId, ArtifactType.ContextBundle, context);
            AddArtifact(session, runId, ArtifactType.RiskAssessment, risk);
            AddArtifact(session, runId, ArtifactType.DeliveryPlan, plan);
            session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                runId, AuditEventType.RunStarted, "foundry", outputContent: ticket));
            session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                runId, AuditEventType.AnalysisCompleted, "foundry", outputContent: analysis));
            session.PolicyDecisions.Add(Audit.Events.BuildPolicyDecisionRow(runId, payload, decision));
            session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                runId, AuditEventType.PolicyEvaluated, "foundry", outputContent: decision));
            run.Status = status;
            run.CurrentStep = "planned";
            if (status == RunStatus.WaitingApproval)
            {
                session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                    runId, AuditEventType.ApprovalRequested, "foundry"));
            }
            else if (status == RunStatus.Blocked)
            {
                session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                    runId, AuditEventType.RunBlocked, "foundry"));
            }
            session.SaveChanges();
        }

        // Mirror the outcome back to the tracker (Linear) if one is configured.
        if (_tracker is not null)
        {
            try
            {
                _tracker.PostComment(
                    ticket.IssueId,
                    Comments.FormatAnalysisComment(analysis, risk, plan, status));
                _tracker.SetState(ticket.IssueId, Comments.StateFor(status));
            }
            catch
            {
                // Tracker write-back failed; Foundry state is authoritative but
                // the tracker may be stale.
            }
        }
        return runId;
    }

    private void NotifyState(string issueId, RunStatus status)
    {
        if (_tracker is null)
        {
            return;
        }
        try
        {
            _tracker.SetState(issueId, Comments.StateFor(status));
        }
        catch
        {
            // Tracker state update failed; never break the governance path.
        }
    }

    private void NotifyComment(string issueId, string body)
    {
        if (_tracker is null)
        {
            return;
        }
        try
        {
            _tracker.PostComment(issueId, body);
        }
        catch
        {
            // Tracker comment failed; never break the governance path.
        }
    }

    private static RunStatus PostPlanStatus(TicketAnalysis analysis, RiskAssessment risk)
    {
        // Readiness first: an unclear ticket should be clarified before we
        // worry about anything downstream (it usually also lacks a resolvable repo).
        if (!analysis.IsReadyToBuild)
        {
            return RunStatus.NeedsClarification;
        }
        // The ticket is clear, but the work still can't be scoped to a repo.
        if (risk.OverallRisk == OverallRisk.Blocked)
        {
            return RunStatus.Blocked;
        }
        // A ready, scoped plan awaits human approval before any agent runs.
        return RunStatus.WaitingApproval;
    }

    // -- approval -------------------------------------------------------------

    public void Approve(string runId, string user, IReadOnlySet<ApprovalRole>? grantedRoles = null)
    {
        grantedRoles ??= new HashSet<ApprovalRole>();
        string issueId;
        using (var session = _contextFactory())
        {
            var run = RequireRun(session, runId);
            if (run.Status != RunStatus.WaitingApproval)
            {
                throw new OrchestratorException(
                    $"run {runId} is '{run.Status.ToWire()}', not awaiting approval");
            }
            var approvalRecord = new Dictionary<string, object?>
            {
                ["user"] = user,
                ["granted_roles"] = grantedRoles.Select(r => r.ToWire())
                    .OrderBy(r => r, StringComparer.Ordinal).ToList(),
            };
            AddArtifact(session, runId, ArtifactType.ApprovalRecord, approvalRecord);
            session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                runId, AuditEventType.ApprovalGranted, "human", actorId: user,
                outputContent: approvalRecord));
            run.Status = RunStatus.Approved;
            run.ApprovedBy = user;
            run.ApprovedAt = DateTime.UtcNow;
            run.CurrentStep = "approved";
            issueId = run.LinearIssueId;
            session.SaveChanges();
        }
        NotifyState(issueId, RunStatus.Approved);
    }

    /// <summary>Terminate a run a human declined (from /foundry reject).</summary>
    public void Reject(string runId, string user) =>
        Terminate(runId, user, RunStatus.Rejected, AuditEventType.ApprovalRejected);

    /// <summary>Halt a run a human stopped (from /foundry stop).</summary>
    public void Stop(string runId, string user) =>
        Terminate(runId, user, RunStatus.Blocked, AuditEventType.RunBlocked);

    private void Terminate(string runId, string user, RunStatus status, AuditEventType eventType)
    {
        string issueId;
        using (var session = _contextFactory())
        {
            var run = RequireRun(session, runId);
            run.Status = status;
            run.CurrentStep = status.ToWire();
            session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                runId, eventType, "human", actorId: user));
            issueId = run.LinearIssueId;
            session.SaveChanges();
        }
        NotifyState(issueId, status);
    }

    // -- read helpers (used by the API) ---------------------------------------

    public string? FindRunIdForIssue(string linearIssueId)
    {
        using var session = _contextFactory();
        return session.Runs
            .Where(r => r.LinearIssueId == linearIssueId)
            .OrderByDescending(r => r.CreatedAt)
            .Select(r => r.Id)
            .FirstOrDefault();
    }

    /// <summary>The in-flight run for an issue, if any (null means restartable).</summary>
    public string? FindActiveRunIdForIssue(string linearIssueId)
    {
        using var session = _contextFactory();
        var activeStatuses = Common.ActiveRunStatuses.ToList();
        return session.Runs.AsEnumerable()
            .Where(r => r.LinearIssueId == linearIssueId && activeStatuses.Contains(r.Status))
            .OrderByDescending(r => r.CreatedAt)
            .Select(r => r.Id)
            .FirstOrDefault();
    }

    /// <summary>Associate an observed PR back to its run via the agent job's branch.</summary>
    public string? FindRunIdForBranch(string branch)
    {
        if (string.IsNullOrEmpty(branch))
        {
            return null;
        }
        using var session = _contextFactory();
        return session.AgentJobs
            .Where(j => j.Branch == branch)
            .OrderByDescending(j => j.StartedAt)
            .Select(j => j.RunId)
            .FirstOrDefault();
    }

    /// <summary>
    /// Find the run an observed PR belongs to.
    ///
    /// Exact branch match first (direct providers control the branch name).
    /// Falls back to Linear issue keys found in the branch name or PR title,
    /// because delegated agents (e.g. Cursor via Linear) choose their own
    /// branch names but embed the issue key. Only runs in a PR-observable
    /// state are matched, so stale runs for the same issue are not revived.
    /// </summary>
    public string? CorrelatePr(PullRequestState prState)
    {
        var runId = FindRunIdForBranch(prState.Branch);
        if (runId is not null)
        {
            return runId;
        }

        var keys = new List<string>();
        foreach (var text in new[] { prState.Branch, prState.Title })
        {
            keys.AddRange(IssueKeyRegex().Matches(text ?? "")
                .Select(m => m.Groups[1].Value.ToUpperInvariant()));
        }
        if (keys.Count == 0)
        {
            return null;
        }
        using var session = _contextFactory();
        foreach (var key in keys.Distinct()) // de-dup, preserve order
        {
            var run = session.Runs.AsEnumerable()
                .Where(r => r.LinearIssueKey == key && PrObservableStatuses.Contains(r.Status))
                .OrderByDescending(r => r.CreatedAt)
                .FirstOrDefault();
            if (run is not null)
            {
                return run.Id;
            }
        }
        return null;
    }

    public FoundryRun? GetRun(string runId)
    {
        using var session = _contextFactory();
        return session.Runs.Find(runId);
    }

    public List<FoundryRun> ListRuns()
    {
        using var session = _contextFactory();
        return session.Runs.OrderBy(r => r.CreatedAt).ToList();
    }

    // -- agent dispatch -------------------------------------------------------

    /// <summary>Re-check policy with the recorded approvals, then launch the provider.</summary>
    public CodingAgentJob DispatchAgent(string runId)
    {
        using var span = Observability.Span("foundry.dispatch_agent");
        string issueId;
        CodingAgentJob job;
        using (var session = _contextFactory())
        {
            var run = RequireRun(session, runId);
            if (run.Status != RunStatus.Approved)
            {
                throw new OrchestratorException(
                    $"run {runId} is '{run.Status.ToWire()}', not approved");
            }
            var analysis = Load<TicketAnalysis>(session, runId, ArtifactType.TicketAnalysis);
            var context = Load<ContextBundle>(session, runId, ArtifactType.ContextBundle);
            var risk = Load<RiskAssessment>(session, runId, ArtifactType.RiskAssessment);
            var plan = Load<DeliveryPlan>(session, runId, ArtifactType.DeliveryPlan);
            var ticket = Load<RawTicket>(session, runId, ArtifactType.TicketSnapshot);
            var granted = LoadGrantedRoles(session, runId);

            var payload = BuildPolicyInput(
                PolicyAction.StartAgent, analysis, context, risk, approvals: granted);
            var decision = _policy.Evaluate(payload);
            session.PolicyDecisions.Add(Audit.Events.BuildPolicyDecisionRow(runId, payload, decision));
            if (!decision.Allowed || decision.AllowedAgentMode == Schemas.AgentMode.HumanOnly)
            {
                run.Status = RunStatus.Blocked;
                session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                    runId, AuditEventType.RunBlocked, "foundry", outputContent: decision));
                var blockedIssue = run.LinearIssueId;
                session.SaveChanges();
                NotifyState(blockedIssue, RunStatus.Blocked);
                throw new OrchestratorException(
                    "policy gate blocked agent dispatch: " + string.Join("; ", decision.Reasons));
            }

            var jobInput = BuildJobInput(runId, ticket, plan, context);
            job = _provider.CreateJob(jobInput);

            session.AgentJobs.Add(new FoundryAgentJob
            {
                Id = Audit.Events.NewId("job"),
                RunId = runId,
                Provider = job.Provider,
                ProviderJobId = job.JobId,
                Status = AgentJobStatus.Running,
                Repo = jobInput.Repo,
                Branch = jobInput.BranchName,
                StartedAt = DateTime.UtcNow,
            });
            session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                runId, AuditEventType.AgentStarted, "foundry",
                metadata: new Dictionary<string, object?>
                {
                    ["provider"] = job.Provider,
                    ["job_id"] = job.JobId,
                }));
            run.Status = RunStatus.AgentRunning;
            run.CurrentStep = "agent_running";
            issueId = run.LinearIssueId;
            session.SaveChanges();
        }
        NotifyState(issueId, RunStatus.AgentRunning);
        return job;
    }

    // -- PR monitoring --------------------------------------------------------

    /// <summary>Mark a run as failed when the agent crashes without creating a PR.</summary>
    public void MarkAgentFailed(string runId, string reason = "agent error")
    {
        string issueId;
        using (var session = _contextFactory())
        {
            var run = RequireRun(session, runId);
            run.Status = RunStatus.ExecutionFailed;
            run.CurrentStep = "failed";
            session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                runId, AuditEventType.AgentFailed, "foundry",
                metadata: new Dictionary<string, object?> { ["reason"] = reason }));
            var job = session.AgentJobs
                .Where(j => j.RunId == runId)
                .OrderByDescending(j => j.StartedAt)
                .FirstOrDefault();
            if (job is not null)
            {
                job.Status = AgentJobStatus.Failed;
                job.Error = reason;
                job.CompletedAt = DateTime.UtcNow;
            }
            issueId = run.LinearIssueId;
            session.SaveChanges();
        }
        NotifyState(issueId, RunStatus.ExecutionFailed);
    }

    /// <summary>
    /// Record an observed PR event and decide the resulting run status.
    ///
    /// Called for *every* observed event (opened, synchronize, reviews, CI),
    /// not just the first - the guardrails are re-evaluated on every push so an
    /// agent cannot open a clean PR and add forbidden or sensitive files later.
    ///
    /// Outcomes, in precedence order:
    ///
    /// - merged                      -> Complete
    /// - closed without merge        -> Blocked (a human must restart)
    /// - forbidden paths in the diff -> Blocked (sticky; human must intervene)
    /// - diff touches a sensitive area the upfront risk pass never flagged
    ///                               -> ReviewRequired (risk escalation)
    /// - more files than allowed     -> ReviewRequired
    /// - otherwise                   -> PrOpen
    ///
    /// Events that carry no file list (reviews, check suites) update CI/review
    /// state without weakening a prior file-based decision.
    /// </summary>
    public RunStatus RecordPr(string runId, PullRequestState prState)
    {
        using var span = Observability.Span("foundry.record_pr");
        string issueId;
        RunStatus resultStatus;
        using (var session = _contextFactory())
        {
            var run = RequireRun(session, runId);
            if (!PrObservableStatuses.Contains(run.Status))
            {
                throw new OrchestratorException(
                    $"run {runId} is '{run.Status.ToWire()}'; PR events are only "
                    + "recorded for runs with a dispatched agent");
            }
            var firstObservation = run.Status == RunStatus.AgentRunning;
            AddArtifact(session, runId, ArtifactType.PrState, prState);
            session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                runId,
                firstObservation ? AuditEventType.PrOpened : AuditEventType.PrUpdated,
                "agent",
                outputContent: prState));
            var job = session.AgentJobs
                .Where(j => j.RunId == runId)
                .OrderByDescending(j => j.StartedAt)
                .FirstOrDefault();
            if (job is not null)
            {
                job.PrUrl = string.IsNullOrEmpty(prState.Url) ? job.PrUrl : prState.Url;
                if (!string.IsNullOrEmpty(prState.Branch))
                {
                    // Delegated agents pick their own branch; record the actual
                    // one so subsequent events correlate by exact branch match.
                    job.Branch = prState.Branch;
                }
            }

            run.Status = NextStatusForPr(session, run, prState);
            if (run.Status == RunStatus.Complete && job is not null)
            {
                job.Status = AgentJobStatus.Succeeded;
                job.CompletedAt = DateTime.UtcNow;
            }

            if (prState.CiStatus == CiStatus.Failing)
            {
                session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                    runId, AuditEventType.CiFailed, "foundry",
                    metadata: new Dictionary<string, object?> { ["pr"] = prState.Url }));
            }

            run.CurrentStep = run.Status.ToWire();
            issueId = run.LinearIssueId;
            resultStatus = run.Status;
            session.SaveChanges();
        }
        NotifyState(issueId, resultStatus);

        // Feedback loop: a failing check or a changes-requested review on an
        // otherwise-open PR re-dispatches the agent with the failure context,
        // still through the policy gate and bounded by the retry cap.
        var reason = RemediationReason(prState);
        if (resultStatus == RunStatus.PrOpen && reason is not null)
        {
            return AttemptRemediation(runId, reason, prState);
        }
        return resultStatus;
    }

    private static string? RemediationReason(PullRequestState prState)
    {
        if (prState.CiStatus == CiStatus.Failing)
        {
            return "ci_failed";
        }
        if (prState.ReviewStatus == ReviewStatus.ChangesRequested)
        {
            return "changes_requested";
        }
        return null;
    }

    /// <summary>
    /// Re-dispatch the agent to fix its own PR, governed and bounded.
    ///
    /// The attempt passes the policy gate as RetryAgent (which re-checks
    /// approvals and the retry cap). A denied attempt parks the run at
    /// ReviewRequired with a tracker comment - never silent, never unbounded.
    /// </summary>
    private RunStatus AttemptRemediation(string runId, string reason, PullRequestState prState)
    {
        if (!_retryOn.Contains(reason))
        {
            return RunStatus.PrOpen;
        }

        string issueId;
        using (var session = _contextFactory())
        {
            var run = RequireRun(session, runId);
            var ticket = Load<RawTicket>(session, runId, ArtifactType.TicketSnapshot);
            var analysis = Load<TicketAnalysis>(session, runId, ArtifactType.TicketAnalysis);
            var context = Load<ContextBundle>(session, runId, ArtifactType.ContextBundle);
            var risk = Load<RiskAssessment>(session, runId, ArtifactType.RiskAssessment);
            var plan = Load<DeliveryPlan>(session, runId, ArtifactType.DeliveryPlan);
            var granted = LoadGrantedRoles(session, runId);

            // The first job was the original dispatch; everything after is a
            // remediation attempt (attempt N = N-th re-dispatch).
            var attempt = session.AgentJobs.Count(j => j.RunId == runId);

            // Refresh provider-reported spend before the budget check so the
            // decision is made on the freshest numbers we can get.
            RefreshJobCosts(session, runId);
            var runCost = session.AgentJobs
                .Where(j => j.RunId == runId)
                .AsEnumerable()
                .Sum(j => j.CostUsd ?? 0.0);

            var payload = BuildPolicyInput(
                PolicyAction.RetryAgent, analysis, context, risk,
                approvals: granted,
                retry: new PolicyRetry { Attempt = attempt, MaxAttempts = _maxAgentRetries },
                budget: new PolicyBudget { CostUsd = runCost, MaxCostUsd = _maxCostPerRun });
            var decision = _policy.Evaluate(payload);
            session.PolicyDecisions.Add(Audit.Events.BuildPolicyDecisionRow(runId, payload, decision));

            if (!decision.Allowed)
            {
                run.Status = RunStatus.ReviewRequired;
                run.CurrentStep = "remediation_denied";
                session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                    runId, AuditEventType.RunBlocked, "foundry",
                    metadata: new Dictionary<string, object?>
                    {
                        ["reason"] = $"remediation for '{reason}' denied",
                        ["policy_reasons"] = decision.Reasons,
                    }));
                issueId = run.LinearIssueId;
                session.SaveChanges();
                NotifyState(issueId, RunStatus.ReviewRequired);
                NotifyComment(
                    issueId,
                    $"Foundry could not remediate ({reason.Replace('_', ' ')}): "
                    + string.Join("; ", decision.Reasons)
                    + "\n\nA human needs to take this PR over the line.");
                return RunStatus.ReviewRequired;
            }

            var jobInput = BuildJobInput(
                runId, ticket, plan, context,
                branch: string.IsNullOrEmpty(prState.Branch) ? null : prState.Branch,
                extraInstructions: RemediationInstructions(reason, prState));
            var job = _provider.CreateJob(jobInput);
            session.AgentJobs.Add(new FoundryAgentJob
            {
                Id = Audit.Events.NewId("job"),
                RunId = runId,
                Provider = job.Provider,
                ProviderJobId = job.JobId,
                Status = AgentJobStatus.Running,
                Repo = jobInput.Repo,
                Branch = jobInput.BranchName,
                StartedAt = DateTime.UtcNow,
            });
            session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                runId, AuditEventType.AgentRemediationRequested, "foundry",
                metadata: new Dictionary<string, object?>
                {
                    ["reason"] = reason,
                    ["attempt"] = attempt,
                    ["max_attempts"] = _maxAgentRetries,
                    ["provider"] = job.Provider,
                    ["job_id"] = job.JobId,
                }));
            run.Status = RunStatus.AgentRunning;
            run.CurrentStep = "remediating";
            issueId = run.LinearIssueId;
            session.SaveChanges();
        }
        NotifyState(issueId, RunStatus.AgentRunning);
        return RunStatus.AgentRunning;
    }

    /// <summary>
    /// Pull provider-reported spend onto the job rows, best-effort.
    ///
    /// Providers that observe progress out-of-band report no usage; provider
    /// errors must never break the governance path that called this.
    /// </summary>
    private void RefreshJobCosts(FoundryDbContext session, string runId)
    {
        foreach (var job in session.AgentJobs.Where(j => j.RunId == runId).ToList())
        {
            if (string.IsNullOrEmpty(job.ProviderJobId) || job.Provider != _provider.Name)
            {
                continue;
            }
            CodingAgentJobStatus status;
            try
            {
                status = _provider.GetJobStatus(job.ProviderJobId);
            }
            catch
            {
                continue;
            }
            if (status.CostUsd is not null)
            {
                job.CostUsd = status.CostUsd;
            }
        }
    }

    private static string RemediationInstructions(string reason, PullRequestState prState)
    {
        var prRef = string.IsNullOrEmpty(prState.Url) ? $"#{prState.PrNumber}" : prState.Url;
        var lines = new List<string>
        {
            "",
            "---",
            "REMEDIATION REQUEST",
            $"Your previous work on PR {prRef} needs fixing: {reason.Replace('_', ' ')}.",
            "Push fixes to the same branch. Do not open a new PR. Stay strictly "
            + "within the original scope and constraints.",
        };
        if (!string.IsNullOrEmpty(prState.Summary))
        {
            lines.AddRange(new[] { "", "Failure details:", prState.Summary });
        }
        return string.Join("\n", lines);
    }

    private RunStatus NextStatusForPr(FoundryDbContext session, FoundryRun run, PullRequestState prState)
    {
        var runId = run.Id;
        if (prState.Status == PrStatus.Merged)
        {
            session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                runId, AuditEventType.RunCompleted, "foundry",
                metadata: new Dictionary<string, object?> { ["pr"] = prState.Url }));
            return RunStatus.Complete;
        }
        if (prState.Status == PrStatus.Closed)
        {
            session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                runId, AuditEventType.RunBlocked, "foundry",
                metadata: new Dictionary<string, object?>
                {
                    ["reason"] = "PR closed without merge",
                    ["pr"] = prState.Url,
                }));
            return RunStatus.Blocked;
        }

        if (prState.FilesChanged.Count == 0)
        {
            // No diff information on this event; keep the current file-based
            // decision rather than silently downgrading it.
            return run.Status == RunStatus.AgentRunning ? RunStatus.PrOpen : run.Status;
        }

        var violations = ForbiddenViolations(prState.FilesChanged);
        if (violations.Count > 0)
        {
            session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                runId, AuditEventType.RunBlocked, "foundry",
                metadata: new Dictionary<string, object?> { ["forbidden_files"] = violations }));
            return RunStatus.Blocked;
        }

        var unexpected = UnexpectedSensitiveAreas(session, runId, prState.FilesChanged);
        if (unexpected.Count > 0)
        {
            session.AuditEvents.Add(Audit.Events.BuildAuditEvent(
                runId, AuditEventType.RiskEscalated, "foundry",
                metadata: new Dictionary<string, object?>
                {
                    ["reason"] = "diff touches sensitive areas the upfront risk "
                        + "assessment did not flag",
                    ["areas"] = unexpected,
                }));
            return RunStatus.ReviewRequired;
        }

        if (prState.FilesChanged.Count > _maxFilesChanged)
        {
            return RunStatus.ReviewRequired;
        }
        return RunStatus.PrOpen;
    }

    /// <summary>
    /// Sensitive areas the diff touches that intake never flagged.
    ///
    /// Areas flagged upfront already had their approval requirements enforced
    /// by the policy gate at dispatch; an area appearing *only* in the diff
    /// has had no human look at it, so it escalates the run.
    /// </summary>
    private Dictionary<string, List<string>> UnexpectedSensitiveAreas(
        FoundryDbContext session, string runId, IReadOnlyList<string> files)
    {
        var touched = Globbing.SensitiveAreasForPaths(files, _sensitivePathGlobs);
        if (touched.Count == 0)
        {
            return new Dictionary<string, List<string>>();
        }
        HashSet<string> anticipated;
        try
        {
            var risk = Load<RiskAssessment>(session, runId, ArtifactType.RiskAssessment);
            anticipated = new HashSet<string>(risk.SensitiveAreas.Names());
        }
        catch (OrchestratorException)
        {
            anticipated = new HashSet<string>();
        }
        return touched
            .Where(pair => !anticipated.Contains(pair.Key))
            .ToDictionary(pair => pair.Key, pair => pair.Value);
    }

    // -- helpers --------------------------------------------------------------

    private CodingAgentJobInput BuildJobInput(
        string runId,
        RawTicket ticket,
        DeliveryPlan plan,
        ContextBundle context,
        string? branch = null,
        string extraInstructions = "")
    {
        // Guaranteed by policy/readiness gating.
        var bestRepo = context.BestRepository
            ?? throw new OrchestratorException("no candidate repository for dispatched run");
        return new CodingAgentJobInput
        {
            RunId = runId,
            Repo = bestRepo.Repo,
            BranchName = branch ?? Planning.BranchNameFor(ticket),
            TicketUrl = $"https://linear.app/issue/{ticket.IssueKey}",
            DeliveryPlan = plan,
            AgentInstructions = (plan.AgentInstructions ?? "") + extraInstructions,
            Constraints = new JobConstraints
            {
                DoNotModify = _forbiddenGlobs.ToList(),
                RequiredTests = context.TestCommands.ToList(),
                MaxFilesChanged = _maxFilesChanged,
            },
            TrackerIssueId = ticket.IssueId,
        };
    }

    private static PolicyInput BuildPolicyInput(
        PolicyAction action,
        TicketAnalysis analysis,
        ContextBundle context,
        RiskAssessment risk,
        IReadOnlySet<string>? approvals = null,
        PolicyRetry? retry = null,
        PolicyBudget? budget = null)
    {
        var bestRepo = context.BestRepository;
        var sensitive = risk.SensitiveAreas;
        return new PolicyInput
        {
            Action = action,
            Ticket = new PolicyTicket
            {
                WorkType = analysis.WorkType.ToWire(),
                Readiness = analysis.ImplementationReadiness,
            },
            Risk = new PolicyRisk
            {
                OverallRisk = risk.OverallRisk,
                Auth = sensitive.Auth,
                Payments = sensitive.Payments,
                CustomerData = sensitive.CustomerData,
                Pii = sensitive.Pii,
                DatabaseMigration = sensitive.DatabaseMigration,
                Infrastructure = sensitive.Infrastructure,
                ProductionDeploy = sensitive.ProductionDeploy,
            },
            Repo = new PolicyRepo
            {
                Name = bestRepo?.Repo,
                Confidence = bestRepo?.Confidence ?? 0,
            },
            Retry = retry ?? new PolicyRetry(),
            Budget = budget ?? new PolicyBudget(),
            Approval = (approvals ?? new HashSet<string>()).ToDictionary(role => role, _ => true),
        };
    }

    private List<string> ForbiddenViolations(IReadOnlyList<string> files)
    {
        var violations = new List<string>();
        foreach (var path in files)
        {
            if (_forbiddenGlobs.Any(pattern => Globbing.GlobMatch(path, pattern)))
            {
                violations.Add(path);
            }
        }
        return violations;
    }

    private static void AddArtifact(
        FoundryDbContext session, string runId, ArtifactType artifactType, object content) =>
        session.Artifacts.Add(Audit.Events.BuildArtifact(runId, artifactType, content));

    private static FoundryRun RequireRun(FoundryDbContext session, string runId) =>
        session.Runs.Find(runId)
            ?? throw new OrchestratorException($"run {runId} not found");

    private static FoundryArtifact? LatestArtifact(
        FoundryDbContext session, string runId, ArtifactType artifactType) =>
        session.Artifacts
            .Where(a => a.RunId == runId && a.ArtifactType == artifactType)
            .OrderByDescending(a => a.Version)
            .ThenByDescending(a => a.CreatedAt)
            .AsEnumerable()
            .FirstOrDefault();

    private static T Load<T>(FoundryDbContext session, string runId, ArtifactType artifactType)
    {
        var row = LatestArtifact(session, runId, artifactType)
            ?? throw new OrchestratorException(
                $"missing artifact {artifactType.ToWire()} for {runId}");
        return FoundryJson.Deserialize<T>(row.ContentJson);
    }

    private static HashSet<string> LoadGrantedRoles(FoundryDbContext session, string runId)
    {
        var row = LatestArtifact(session, runId, ArtifactType.ApprovalRecord);
        if (row is null)
        {
            return new HashSet<string>();
        }
        using var document = JsonDocument.Parse(row.ContentJson);
        if (!document.RootElement.TryGetProperty("granted_roles", out var roles)
            || roles.ValueKind != JsonValueKind.Array)
        {
            return new HashSet<string>();
        }
        return roles.EnumerateArray()
            .Select(r => r.GetString())
            .Where(r => r is not null)
            .Select(r => r!)
            .ToHashSet();
    }
}
