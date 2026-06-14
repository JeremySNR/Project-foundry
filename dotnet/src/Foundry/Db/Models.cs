// Foundry core data model.
//
// Tables (from the build plan):
//
// - foundry_runs             - one row per Ticket-to-PR run.
// - foundry_artifacts        - versioned, content-hashed run artifacts.
// - foundry_audit_events     - append-only audit trail.
// - foundry_policy_decisions - every policy gate decision.
// - foundry_agent_jobs       - coding-agent jobs dispatched for a run.
//
// Artifact and audit rows carry a content hash so the immutable input snapshot
// and every decision can be verified after the fact.

using Foundry.Schemas;

namespace Foundry.Db;

public enum ArtifactType
{
    TicketSnapshot,
    TicketAnalysis,
    ContextBundle,
    RiskAssessment,
    DeliveryPlan,
    ApprovalRecord,
    AgentJob,
    PrState,
    FinalSummary,
}

public enum AuditEventType
{
    RunStarted,
    TicketFetched,
    AnalysisCompleted,
    ContextCompleted,
    PolicyEvaluated,
    ApprovalRequested,
    ApprovalGranted,
    ApprovalRejected,
    AgentStarted,
    AgentFailed,
    AgentRemediationRequested,
    PrOpened,
    PrUpdated,
    RiskEscalated,
    CiFailed,
    ReviewCompleted,
    RunCompleted,
    RunBlocked,
}

public static class AuditEventTypeWire
{
    // The Python values are dotted ("run.started"); kept identical so the two
    // implementations can share a database.
    private static readonly IReadOnlyDictionary<AuditEventType, string> ToWireMap =
        new Dictionary<AuditEventType, string>
        {
            [AuditEventType.RunStarted] = "run.started",
            [AuditEventType.TicketFetched] = "ticket.fetched",
            [AuditEventType.AnalysisCompleted] = "analysis.completed",
            [AuditEventType.ContextCompleted] = "context.completed",
            [AuditEventType.PolicyEvaluated] = "policy.evaluated",
            [AuditEventType.ApprovalRequested] = "approval.requested",
            [AuditEventType.ApprovalGranted] = "approval.granted",
            [AuditEventType.ApprovalRejected] = "approval.rejected",
            [AuditEventType.AgentStarted] = "agent.started",
            [AuditEventType.AgentFailed] = "agent.failed",
            [AuditEventType.AgentRemediationRequested] = "agent.remediation_requested",
            [AuditEventType.PrOpened] = "pr.opened",
            [AuditEventType.PrUpdated] = "pr.updated",
            [AuditEventType.RiskEscalated] = "risk.escalated",
            [AuditEventType.CiFailed] = "ci.failed",
            [AuditEventType.ReviewCompleted] = "review.completed",
            [AuditEventType.RunCompleted] = "run.completed",
            [AuditEventType.RunBlocked] = "run.blocked",
        };

    private static readonly IReadOnlyDictionary<string, AuditEventType> FromWireMap =
        ToWireMap.ToDictionary(p => p.Value, p => p.Key);

    public static string ToWire(this AuditEventType value) => ToWireMap[value];

    public static AuditEventType AuditEventTypeFromWire(string wire) => FromWireMap[wire];
}

public class FoundryRun
{
    // NOTE: deliberately no unique constraint on LinearIssueId - a ticket may
    // be re-analysed after clarification, rejection or failure. "At most one
    // *active* run per issue" is enforced at intake, not by the schema.
    public required string Id { get; set; }

    public required string LinearIssueId { get; set; }

    public required string LinearIssueKey { get; set; }

    public RunStatus Status { get; set; } = RunStatus.Analysing;

    public required string TriggerType { get; set; }

    public string? CreatedBy { get; set; }

    public string? CurrentStep { get; set; }

    public OverallRisk? RiskLevel { get; set; }

    public AgentMode? AgentMode { get; set; }

    public string? ApprovedBy { get; set; }

    public DateTime? ApprovedAt { get; set; }

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;

    public DateTime UpdatedAt { get; set; } = DateTime.UtcNow;

    public List<FoundryArtifact> Artifacts { get; set; } = new();

    public List<FoundryAuditEvent> AuditEvents { get; set; } = new();

    public List<FoundryPolicyDecision> PolicyDecisions { get; set; } = new();

    public List<FoundryAgentJob> AgentJobs { get; set; } = new();
}

public class FoundryArtifact
{
    public required string Id { get; set; }

    public required string RunId { get; set; }

    public ArtifactType ArtifactType { get; set; }

    public int Version { get; set; } = 1;

    public required string ContentJson { get; set; }

    public required string ContentHash { get; set; }

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;

    public string? CreatedBy { get; set; }

    public FoundryRun? Run { get; set; }
}

public class FoundryAuditEvent
{
    public required string Id { get; set; }

    public required string RunId { get; set; }

    /// <summary>
    /// Monotonic per-run sequence number so audit events have a guaranteed
    /// order independent of sub-millisecond timestamp ties.
    /// </summary>
    public int Sequence { get; set; }

    public AuditEventType EventType { get; set; }

    public required string ActorType { get; set; }

    public string? ActorId { get; set; }

    public string? InputHash { get; set; }

    public string? OutputHash { get; set; }

    public string? MetadataJson { get; set; }

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;

    public FoundryRun? Run { get; set; }
}

public class FoundryPolicyDecision
{
    public required string Id { get; set; }

    public required string RunId { get; set; }

    public required string PolicyName { get; set; }

    public required string InputJson { get; set; }

    public required string DecisionJson { get; set; }

    public bool Allowed { get; set; }

    public string? Reason { get; set; }

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;

    public FoundryRun? Run { get; set; }
}

public class FoundryAgentJob
{
    public required string Id { get; set; }

    public required string RunId { get; set; }

    public required string Provider { get; set; }

    public string? ProviderJobId { get; set; }

    public AgentJobStatus Status { get; set; } = AgentJobStatus.Created;

    public string? Repo { get; set; }

    public string? Branch { get; set; }

    public string? PrUrl { get; set; }

    public DateTime? StartedAt { get; set; }

    public DateTime? CompletedAt { get; set; }

    public string? Error { get; set; }

    /// <summary>Provider-reported spend; null = the provider does not expose usage.</summary>
    public double? CostUsd { get; set; }

    public FoundryRun? Run { get; set; }
}
