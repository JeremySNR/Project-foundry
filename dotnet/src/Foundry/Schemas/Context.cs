// ContextBundle - evidence gathered about where work should happen.
//
// The core rule: never assume the repo from the title alone; attach a
// confidence to every candidate and refuse to choose when confidence is low.

using System.Text.Json.Serialization;

namespace Foundry.Schemas;

public sealed record CandidateRepository
{
    private readonly int _confidence;

    public required string Repo { get; init; }

    public required int Confidence
    {
        get => _confidence;
        init => _confidence = Guard.Range(value, 0, 100, nameof(Confidence));
    }

    public required string Reason { get; init; }
}

public sealed record CandidateFile
{
    public required string Path { get; init; }

    public required string Reason { get; init; }
}

public sealed record ContextBundle
{
    public IReadOnlyList<CandidateRepository> CandidateRepositories { get; init; } = Array.Empty<CandidateRepository>();

    public IReadOnlyList<CandidateFile> CandidateFiles { get; init; } = Array.Empty<CandidateFile>();

    public IReadOnlyList<string> RelatedPrs { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> RelatedIssues { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> TestCommands { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> Docs { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> Unknowns { get; init; } = Array.Empty<string>();

    /// <summary>Highest-confidence repository candidate, if any.</summary>
    [JsonIgnore]
    public CandidateRepository? BestRepository =>
        CandidateRepositories.Count == 0
            ? null
            : CandidateRepositories.MaxBy(r => r.Confidence);

    /// <summary>
    /// True when exactly one repo clears the confidence threshold.
    ///
    /// Multiple plausible repos above threshold is ambiguous and should route
    /// to human confirmation rather than autonomous execution.
    /// </summary>
    public bool HasConfidentRepository(int threshold = Common.RepoConfidenceThreshold) =>
        CandidateRepositories.Count(r => r.Confidence >= threshold) == 1;
}
