// Foundry policy gate.
//
// Policy decisions are *hard rules*, not prompts. Every risky action passes
// through here before it is allowed to proceed.
//
// LocalPolicyEngine is a pure-C# evaluator that mirrors the Rego bundle in
// foundry.rego (and the Python LocalPolicyEngine). It is the default so the
// foundation is testable with no OPA server running. An OPA-backed engine can
// implement the same IPolicyEngine interface for production.

using System.Globalization;
using Foundry.Schemas;

namespace Foundry.Policy;

public sealed record PolicyTicket
{
    public string WorkType { get; init; } = "unknown";

    public ImplementationReadiness Readiness { get; init; } = ImplementationReadiness.NeedsClarification;
}

public sealed record PolicyRisk
{
    public OverallRisk OverallRisk { get; init; } = OverallRisk.Medium;
    public bool Auth { get; init; }
    public bool Payments { get; init; }
    public bool CustomerData { get; init; }
    public bool Pii { get; init; }
    public bool DatabaseMigration { get; init; }
    public bool Infrastructure { get; init; }
    public bool ProductionDeploy { get; init; }
}

public sealed record PolicyRepo
{
    private readonly int _confidence;

    public string? Name { get; init; }

    public int Confidence
    {
        get => _confidence;
        init => _confidence = Guard.Range(value, 0, 100, nameof(Confidence));
    }
}

public sealed record PolicyActor
{
    public string Type { get; init; } = "foundry";

    public string User { get; init; } = "agent-system";
}

/// <summary>Remediation attempt counters; only meaningful for retry_agent.</summary>
public sealed record PolicyRetry
{
    private readonly int _attempt;
    private readonly int _maxAttempts = 2;

    public int Attempt
    {
        get => _attempt;
        init => _attempt = Guard.Min(value, 0, nameof(Attempt));
    }

    public int MaxAttempts
    {
        get => _maxAttempts;
        init => _maxAttempts = Guard.Min(value, 0, nameof(MaxAttempts));
    }
}

/// <summary>Run spend so far vs the configured cap; only checked for retry_agent.</summary>
public sealed record PolicyBudget
{
    private readonly double _costUsd;
    private readonly double? _maxCostUsd;

    public double CostUsd
    {
        get => _costUsd;
        init => _costUsd = Guard.MinDouble(value, 0, nameof(CostUsd));
    }

    /// <summary>Null = no budget cap configured.</summary>
    public double? MaxCostUsd
    {
        get => _maxCostUsd;
        init => _maxCostUsd = Guard.Positive(value, nameof(MaxCostUsd));
    }
}

/// <summary>The full context handed to the policy gate for a single action.</summary>
public sealed record PolicyInput
{
    public required PolicyAction Action { get; init; }

    public PolicyActor Actor { get; init; } = new();

    public PolicyTicket Ticket { get; init; } = new();

    public PolicyRisk Risk { get; init; } = new();

    public PolicyRepo Repo { get; init; } = new();

    public PolicyRetry Retry { get; init; } = new();

    public PolicyBudget Budget { get; init; } = new();

    /// <summary>Map of approval role -> granted. Missing keys are treated as not granted.</summary>
    public IReadOnlyDictionary<string, bool> Approval { get; init; } = new Dictionary<string, bool>();
}

public sealed record PolicyDecision
{
    public string DecisionId { get; init; } = Guid.NewGuid().ToString();

    public required string PolicyName { get; init; }

    public required bool Allowed { get; init; }

    public IReadOnlyList<string> Reasons { get; init; } = Array.Empty<string>();

    /// <summary>The strongest agent mode permitted given the inputs.</summary>
    public AgentMode AllowedAgentMode { get; init; } = AgentMode.HumanOnly;

    public IReadOnlyList<ApprovalRole> RequiredApprovals { get; init; } = Array.Empty<ApprovalRole>();
}

public interface IPolicyEngine
{
    PolicyDecision Evaluate(PolicyInput payload);
}

/// <summary>
/// Pure-C# implementation of the minimum policy rules.
///
/// Kept deliberately close to foundry.rego (and the Python LocalPolicyEngine)
/// so the implementations stay in lock-step.
/// </summary>
public sealed class LocalPolicyEngine : IPolicyEngine
{
    public const string PolicyName = "foundry.ticket_to_pr.v1";

    // Actions that may never run autonomously in this version, regardless of
    // risk level or approvals. Evaluating them produces a recorded deny decision.
    private static readonly IReadOnlySet<PolicyAction> ForbiddenActions = new HashSet<PolicyAction>
    {
        PolicyAction.AutoMerge,
        PolicyAction.ProductionDeploy,
    };

    // Actions that actually launch or progress autonomous work.
    private static readonly IReadOnlySet<PolicyAction> AutonomousActions = new HashSet<PolicyAction>
    {
        PolicyAction.StartAgent,
        PolicyAction.CreateBranch,
        PolicyAction.OpenPr,
        PolicyAction.RetryAgent,
        PolicyAction.MarkComplete,
    };

    // Read-only / advisory actions: always allowed, still recorded. This is an
    // explicit allowlist - anything not listed here or in AutonomousActions is
    // denied (default-deny), so a new action cannot slip through ungoverned.
    private static readonly IReadOnlySet<PolicyAction> AdvisoryActions = new HashSet<PolicyAction>
    {
        PolicyAction.AnalyseTicket,
        PolicyAction.CreatePlan,
        PolicyAction.RequestApproval,
        PolicyAction.RequestChanges,
    };

    private readonly int _repoConfidenceThreshold;

    public LocalPolicyEngine(int repoConfidenceThreshold = Common.RepoConfidenceThreshold)
    {
        _repoConfidenceThreshold = repoConfidenceThreshold;
    }

    /// <summary>Derive required approval roles from the sensitive areas in play.</summary>
    private static List<ApprovalRole> RequiredApprovals(PolicyRisk risk)
    {
        var required = new List<ApprovalRole>();
        if (risk.Auth || risk.Infrastructure)
        {
            required.Add(ApprovalRole.Engineering);
        }
        if (risk.CustomerData || risk.Pii || risk.Payments)
        {
            required.Add(ApprovalRole.Security);
        }
        return required.Distinct().ToList();
    }

    public PolicyDecision Evaluate(PolicyInput payload)
    {
        var reasons = new List<string>();
        var required = RequiredApprovals(payload.Risk);
        var threshold = _repoConfidenceThreshold;

        // Hard-forbidden actions are denied unconditionally - no risk level or
        // approval can unlock them in this version.
        if (ForbiddenActions.Contains(payload.Action))
        {
            return new PolicyDecision
            {
                PolicyName = PolicyName,
                Allowed = false,
                Reasons = new[]
                {
                    $"action '{payload.Action.ToWire()}' may never run autonomously in this version",
                },
                AllowedAgentMode = AgentMode.HumanOnly,
                RequiredApprovals = required,
            };
        }

        // Read-only actions never need the autonomous-work gate, but we still
        // surface required approvals so the UI can plan ahead.
        if (AdvisoryActions.Contains(payload.Action))
        {
            return new PolicyDecision
            {
                PolicyName = PolicyName,
                Allowed = true,
                Reasons = new[] { $"action '{payload.Action.ToWire()}' is read-only / advisory" },
                AllowedAgentMode = AllowedMode(payload, blocked: false),
                RequiredApprovals = required,
            };
        }

        // Default-deny: an action this policy does not recognise is refused.
        if (!AutonomousActions.Contains(payload.Action))
        {
            return new PolicyDecision
            {
                PolicyName = PolicyName,
                Allowed = false,
                Reasons = new[]
                {
                    $"action '{payload.Action.ToWire()}' is not covered by this policy; denying by default",
                },
                AllowedAgentMode = AgentMode.HumanOnly,
                RequiredApprovals = required,
            };
        }

        // --- hard blocks (MVP) ---
        if (payload.Risk.ProductionDeploy)
        {
            reasons.Add("production deployment is blocked in the MVP");
        }
        if (payload.Risk.DatabaseMigration)
        {
            reasons.Add("database migrations are blocked in the MVP");
        }
        if (payload.Repo.Confidence < threshold)
        {
            reasons.Add(
                $"repository confidence {payload.Repo.Confidence} is below the threshold of {threshold}");
        }
        if (payload.Ticket.Readiness != ImplementationReadiness.Ready)
        {
            reasons.Add($"ticket readiness is '{payload.Ticket.Readiness.ToWire()}', not 'ready'");
        }
        if (payload.Risk.OverallRisk == OverallRisk.Blocked)
        {
            reasons.Add("risk assessment marked the work as blocked");
        }
        if (payload.Action == PolicyAction.RetryAgent && payload.Retry.Attempt > payload.Retry.MaxAttempts)
        {
            reasons.Add(
                $"remediation attempt {payload.Retry.Attempt} exceeds the maximum of {payload.Retry.MaxAttempts}");
        }
        if (payload.Action == PolicyAction.RetryAgent
            && payload.Budget.MaxCostUsd is double cap
            && payload.Budget.CostUsd >= cap)
        {
            var spend = payload.Budget.CostUsd.ToString("0.00", CultureInfo.InvariantCulture);
            var capText = cap.ToString("0.00", CultureInfo.InvariantCulture);
            reasons.Add($"run spend ${spend} has reached the budget cap of ${capText}");
        }

        // --- sensitive areas require explicit approval ---
        foreach (var role in required)
        {
            if (!payload.Approval.TryGetValue(role.ToWire(), out var granted) || !granted)
            {
                reasons.Add($"sensitive work requires '{role.ToWire()}' approval, which is missing");
            }
        }

        var allowed = reasons.Count == 0;
        if (allowed)
        {
            reasons.Add("all minimum policy checks passed");
        }

        return new PolicyDecision
        {
            PolicyName = PolicyName,
            Allowed = allowed,
            Reasons = reasons,
            AllowedAgentMode = AllowedMode(payload, blocked: !allowed),
            RequiredApprovals = required,
        };
    }

    /// <summary>Draft PR is permitted only for low/medium risk; otherwise human-only.</summary>
    private static AgentMode AllowedMode(PolicyInput payload, bool blocked)
    {
        if (blocked)
        {
            return AgentMode.HumanOnly;
        }
        return payload.Risk.OverallRisk is OverallRisk.Low or OverallRisk.Medium
            ? AgentMode.DraftPr
            : AgentMode.HumanOnly;
    }
}
