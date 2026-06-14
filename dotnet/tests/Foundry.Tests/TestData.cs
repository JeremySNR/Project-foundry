// Shared fixtures - the xUnit analogue of the Python conftest.py.

using Foundry.Schemas;

namespace Foundry.Tests;

public static class TestData
{
    public const string ReadyDescription = """
Customers want to favourite items.

Acceptance Criteria:
- A favourites button exists
- Favourites persist across sessions
""";

    public static TicketAnalysis ReadyAnalysis() => new()
    {
        TicketId = "LIN-123",
        Title = "Add customer favourites",
        WorkType = WorkType.Feature,
        Summary = "Let customers favourite items.",
        UserProblem = "Customers cannot save items.",
        BusinessValue = "Increases retention.",
        AcceptanceCriteria = new[] { "A favourites button exists", "Favourites persist" },
        MissingInformation = Array.Empty<string>(),
        Assumptions = new[] { "Auth already exists" },
        AmbiguityScore = 10,
        ImplementationReadiness = ImplementationReadiness.Ready,
        Confidence = 88,
    };

    public static ContextBundle ConfidentContext() => new()
    {
        CandidateRepositories = new[]
        {
            new CandidateRepository
            {
                Repo = "customer-web",
                Confidence = 82,
                Reason = "Repo contains favourites UI components.",
            },
        },
        TestCommands = new[] { "npm test", "npm run lint" },
    };

    public static DeliveryPlan SampleDeliveryPlan() => new()
    {
        Goal = "Add customer favourites",
        Scope = new[] { "Favourites UI", "API client call" },
        OutOfScope = new[] { "Recommendations engine" },
        AffectedRepositories = new[] { "customer-web" },
        ExpectedFilesOrAreas = new[] { "src/features/favourites" },
        ImplementationSteps = new[]
        {
            new ImplementationStep
            {
                Step = 1,
                Description = "Add favourites state",
                ExpectedOutput = "state slice",
            },
        },
        TestPlan = new TestPlan { UnitTests = new[] { "favourites reducer" } },
        AgentInstructions = "Implement favourites per the plan.",
    };

    public static RawTicket ReadyTicket(
        string issueId = "i-1",
        string issueKey = "LIN-123",
        string title = "Add customer favourites",
        string? description = null,
        IReadOnlyList<string>? knownRepositories = null,
        IReadOnlyList<LinkedResource>? linkedResources = null) => new()
    {
        IssueId = issueId,
        IssueKey = issueKey,
        Title = title,
        Description = description ?? ReadyDescription,
        KnownRepositories = knownRepositories ?? new[] { "customer-web" },
        LinkedResources = linkedResources ?? Array.Empty<LinkedResource>(),
    };
}
