// RawTicket - the immutable snapshot of a Linear issue at intake.
//
// This is the input to the intelligence engines. It is stored verbatim as the
// ticket_snapshot artifact so every downstream decision can be traced back to
// exactly what Foundry saw.

namespace Foundry.Schemas;

public sealed record LinkedResource
{
    /// <summary>e.g. "github_pr", "github_issue", "repo".</summary>
    public required string Kind { get; init; }

    public required string Url { get; init; }

    public string? Repo { get; init; }
}

public sealed record RawTicket
{
    public required string IssueId { get; init; }

    public required string IssueKey { get; init; }

    public required string Title { get; init; }

    public string Description { get; init; } = "";

    public IReadOnlyList<string> Labels { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> Comments { get; init; } = Array.Empty<string>();

    public IReadOnlyList<LinkedResource> LinkedResources { get; init; } = Array.Empty<LinkedResource>();

    /// <summary>Repositories the team has explicitly associated with the issue, if any.</summary>
    public IReadOnlyList<string> KnownRepositories { get; init; } = Array.Empty<string>();

    /// <summary>All free-text on the ticket, lower-cased, for keyword heuristics.</summary>
    public string TextBlob()
    {
        var parts = new List<string> { Title, Description };
        parts.AddRange(Comments);
        return string.Join("\n", parts).ToLowerInvariant();
    }

    /// <summary>
    /// Title and description only, excluding comments.
    ///
    /// Used for risk classification so that stale or speculative comments
    /// (e.g. 'maybe this touches payments') don't permanently flag a ticket
    /// as high-risk based on historical discussion rather than stated intent.
    /// </summary>
    public string RiskBlob() => string.Join("\n", new[] { Title, Description }).ToLowerInvariant();
}
