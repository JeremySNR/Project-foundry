// Coding-agent job contracts.
//
// These are provider-agnostic: every ICodingAgentProvider (Cursor, Claude Code,
// OpenAI agent, manual) accepts the same CodingAgentJobInput and reports the
// same CodingAgentJob shape. Secrets must never travel in these objects.

namespace Foundry.Schemas;

public sealed record JobConstraints
{
    private readonly int _maxFilesChanged = 12;

    public IReadOnlyList<string> DoNotModify { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> RequiredTests { get; init; } = Array.Empty<string>();

    public int MaxFilesChanged
    {
        get => _maxFilesChanged;
        init => _maxFilesChanged = Guard.Min(value, 1, nameof(MaxFilesChanged));
    }

    public bool AllowNewDependencies { get; init; }
}

public sealed record CodingAgentJobInput
{
    public required string RunId { get; init; }

    public required string Repo { get; init; }

    public string BaseBranch { get; init; } = "main";

    public required string BranchName { get; init; }

    public required string TicketUrl { get; init; }

    public required DeliveryPlan DeliveryPlan { get; init; }

    public required string AgentInstructions { get; init; }

    public JobConstraints Constraints { get; init; } = new();

    /// <summary>
    /// Identifier of the source issue in the tracker (e.g. Linear), so a provider
    /// that delegates via the tracker (Cursor-via-Linear) can address it.
    /// </summary>
    public string? TrackerIssueId { get; init; }
}

/// <summary>Handle returned when a job is created with a provider.</summary>
public sealed record CodingAgentJob
{
    public required string JobId { get; init; }

    public required string Provider { get; init; }

    public AgentJobStatus Status { get; init; } = AgentJobStatus.Created;
}

public sealed record CodingAgentJobStatus
{
    public required string JobId { get; init; }

    public required string Provider { get; init; }

    public required AgentJobStatus Status { get; init; }

    public string? Branch { get; init; }

    public string? PrUrl { get; init; }

    public string? Error { get; init; }

    /// <summary>
    /// Spend reported by the provider for this job, where the provider exposes
    /// usage (e.g. the Cursor Cloud Agents API). Null = unknown, not zero.
    /// </summary>
    public double? CostUsd { get; init; }
}
