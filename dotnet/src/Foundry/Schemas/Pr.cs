// PullRequestState - Foundry's observed view of a GitHub PR.
//
// Foundry never assumes the agent succeeded; it monitors the PR independently.

namespace Foundry.Schemas;

public sealed record PullRequestState
{
    public required string Repo { get; init; }

    public required int PrNumber { get; init; }

    public required string Url { get; init; }

    public required string Branch { get; init; }

    /// <summary>
    /// PR title, used to correlate delegated-agent PRs back to a run via the
    /// embedded issue key when the branch name was not chosen by Foundry.
    /// </summary>
    public string Title { get; init; } = "";

    public required PrStatus Status { get; init; }

    public CiStatus CiStatus { get; init; } = CiStatus.Unknown;

    public ReviewStatus ReviewStatus { get; init; } = ReviewStatus.None;

    public IReadOnlyList<string> FilesChanged { get; init; } = Array.Empty<string>();

    public OverallRisk RiskDelta { get; init; } = OverallRisk.Low;

    public string Summary { get; init; } = "";
}
