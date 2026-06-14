// Shared enumerations used across Foundry artifact schemas.
//
// These mirror the vocabulary in the Ticket-to-PR build plan. Serialised as
// snake_case strings so the JSON representation is human-readable, stable for
// golden tests, and wire-compatible with the Python implementation.

namespace Foundry.Schemas;

public enum WorkType
{
    Feature,
    Bug,
    TechDebt,
    Incident,
    Question,
    Unknown,
}

public enum ImplementationReadiness
{
    Ready,
    NeedsClarification,
    NotSuitable,
}

public enum OverallRisk
{
    Low,
    Medium,
    High,
    Blocked,
}

/// <summary>How much autonomy an agent is permitted for a given run.</summary>
public enum AgentMode
{
    AnalysisOnly,
    DraftPr,
    HumanOnly,
}

public enum ApprovalRole
{
    Product,
    Engineering,
    Security,
    Qa,
}

/// <summary>Foundry run lifecycle. Maps to the suggested Linear states.</summary>
public enum RunStatus
{
    Analysing,
    NeedsClarification,
    PlanReady,
    WaitingApproval,
    Approved,
    AgentRunning,
    PrOpen,
    ReviewRequired,
    Complete,
    Blocked,
    ExecutionFailed,
    Rejected,
}

public enum PrStatus
{
    Draft,
    Open,
    ChangesRequested,
    Approved,
    Merged,
    Closed,
}

public enum CiStatus
{
    Pending,
    Passing,
    Failing,
    Unknown,
}

public enum ReviewStatus
{
    None,
    BotReviewed,
    HumanReviewed,
    ChangesRequested,
    Approved,
}

public enum AgentJobStatus
{
    Created,
    Running,
    Succeeded,
    Failed,
    Cancelled,
}

/// <summary>Risky actions that must pass through the policy gate.</summary>
public enum PolicyAction
{
    AnalyseTicket,
    CreatePlan,
    RequestApproval,
    StartAgent,
    CreateBranch,
    OpenPr,
    RequestChanges,
    RetryAgent,
    MarkComplete,
    // Modelled explicitly so "never autonomous" is an enforced decision with an
    // audit row, not an absence of code. Both are denied unconditionally in
    // this version regardless of risk level or approvals.
    AutoMerge,
    ProductionDeploy,
}

public static class Common
{
    /// <summary>
    /// Run states that are still in flight. A ticket with a run in one of these
    /// states cannot start another run; anything else (clarification, rejection,
    /// blocked, failed, complete) is restartable by a fresh trigger.
    /// </summary>
    public static readonly IReadOnlySet<RunStatus> ActiveRunStatuses = new HashSet<RunStatus>
    {
        RunStatus.Analysing,
        RunStatus.PlanReady,
        RunStatus.WaitingApproval,
        RunStatus.Approved,
        RunStatus.AgentRunning,
        RunStatus.PrOpen,
        RunStatus.ReviewRequired,
    };

    /// <summary>
    /// Confidence threshold (0-100) below which a repository match cannot be
    /// trusted to start autonomous work.
    /// </summary>
    public const int RepoConfidenceThreshold = 70;

    /// <summary>Areas considered sensitive enough to require explicit approval / restrict mode.</summary>
    public static readonly IReadOnlyList<string> SensitiveAreaKeys = new[]
    {
        "auth",
        "payments",
        "customer_data",
        "pii",
        "database_migration",
        "infrastructure",
        "production_deploy",
    };
}
