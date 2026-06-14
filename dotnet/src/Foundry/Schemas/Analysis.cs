// TicketAnalysis - structured output of the Ticket Intelligence Engine.
//
// The analysis classifies the work and decides whether the ticket is ready for
// implementation. Per the operating rules, it must NOT produce implementation
// instructions unless acceptance criteria are sufficient.

using System.Text.Json.Serialization;

namespace Foundry.Schemas;

public sealed record TicketAnalysis
{
    private readonly int _ambiguityScore;
    private readonly int _confidence;

    public required string TicketId { get; init; }

    public required string Title { get; init; }

    public required WorkType WorkType { get; init; }

    public required string Summary { get; init; }

    public string? UserProblem { get; init; }

    public string? BusinessValue { get; init; }

    public IReadOnlyList<string> AcceptanceCriteria { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> MissingInformation { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> Assumptions { get; init; } = Array.Empty<string>();

    public required int AmbiguityScore
    {
        get => _ambiguityScore;
        init => _ambiguityScore = Guard.Range(value, 0, 100, nameof(AmbiguityScore));
    }

    public required ImplementationReadiness ImplementationReadiness { get; init; }

    public required int Confidence
    {
        get => _confidence;
        init => _confidence = Guard.Range(value, 0, 100, nameof(Confidence));
    }

    /// <summary>
    /// A ticket is buildable only when ready AND it has acceptance criteria.
    ///
    /// This encodes the hard rule "if acceptance criteria are missing, do not
    /// start coding" directly into the artifact, independent of the LLM's own
    /// implementation_readiness claim.
    /// </summary>
    [JsonIgnore]
    public bool IsReadyToBuild =>
        ImplementationReadiness == ImplementationReadiness.Ready && AcceptanceCriteria.Count > 0;
}
