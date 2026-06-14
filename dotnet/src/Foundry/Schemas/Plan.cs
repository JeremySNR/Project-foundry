// DeliveryPlan - the coding-agent-ready plan produced from a ready ticket.
//
// Hard rules from the build plan: include scope and out-of-scope, a test plan,
// stop conditions, forbidden changes, and a PR description template. The plan
// must not contain agent_instructions when acceptance criteria are insufficient.

namespace Foundry.Schemas;

public sealed record ImplementationStep
{
    private readonly int _step;

    public required int Step
    {
        get => _step;
        init => _step = Guard.Min(value, 1, nameof(Step));
    }

    public required string Description { get; init; }

    public required string ExpectedOutput { get; init; }
}

public sealed record TestPlan
{
    public IReadOnlyList<string> UnitTests { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> IntegrationTests { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> E2eTests { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> ManualChecks { get; init; } = Array.Empty<string>();
}

public sealed record DeliveryPlan
{
    public required string Goal { get; init; }

    public IReadOnlyList<string> Scope { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> OutOfScope { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> AffectedRepositories { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> ExpectedFilesOrAreas { get; init; } = Array.Empty<string>();

    public IReadOnlyList<ImplementationStep> ImplementationSteps { get; init; } = Array.Empty<ImplementationStep>();

    public TestPlan TestPlan { get; init; } = new();

    public IReadOnlyList<string> RollbackConsiderations { get; init; } = Array.Empty<string>();

    public IReadOnlyList<string> OpenQuestions { get; init; } = Array.Empty<string>();

    /// <summary>Must be null until the ticket is genuinely ready to build.</summary>
    public string? AgentInstructions { get; init; }
}
