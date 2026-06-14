// Schema contract tests for the run artifacts (mirrors test_schemas.py).

using Foundry.Schemas;
using Xunit;

namespace Foundry.Tests;

public class SchemaTests
{
    [Fact]
    public void ReadyAnalysisIsBuildable()
    {
        Assert.True(TestData.ReadyAnalysis().IsReadyToBuild);
    }

    [Fact]
    public void ReadyWithoutAcceptanceCriteriaIsNotBuildable()
    {
        var analysis = new TicketAnalysis
        {
            TicketId = "LIN-1",
            Title = "Vague idea",
            WorkType = WorkType.Feature,
            Summary = "Do something nice",
            AcceptanceCriteria = Array.Empty<string>(),
            AmbiguityScore = 80,
            ImplementationReadiness = ImplementationReadiness.Ready,
            Confidence = 50,
        };
        // Even if the LLM claims "ready", missing acceptance criteria blocks build.
        Assert.False(analysis.IsReadyToBuild);
    }

    [Fact]
    public void OutOfRangeAmbiguityScoreIsRejected()
    {
        Assert.Throws<SchemaValidationException>(() => new TicketAnalysis
        {
            TicketId = "LIN-2",
            Title = "x",
            WorkType = WorkType.Bug,
            Summary = "s",
            AmbiguityScore = 200, // out of range
            ImplementationReadiness = ImplementationReadiness.Ready,
            Confidence = 50,
        });
    }

    [Fact]
    public void UnknownFieldIsRejectedOnDeserialize()
    {
        const string json = """
        {
            "ticket_id": "LIN-3",
            "title": "x",
            "work_type": "feature",
            "summary": "s",
            "ambiguity_score": 10,
            "implementation_readiness": "ready",
            "confidence": 50,
            "hallucinated_field": true
        }
        """;
        Assert.Throws<SchemaValidationException>(() => FoundryJson.Deserialize<TicketAnalysis>(json));
    }

    [Fact]
    public void ContextBestRepository()
    {
        var context = TestData.ConfidentContext();
        Assert.NotNull(context.BestRepository);
        Assert.Equal("customer-web", context.BestRepository!.Repo);
    }

    [Fact]
    public void ContextNoRepoMatchIsNotConfident()
    {
        var bundle = new ContextBundle();
        Assert.Null(bundle.BestRepository);
        Assert.False(bundle.HasConfidentRepository());
    }

    [Fact]
    public void ContextLowConfidenceBlocks()
    {
        var bundle = new ContextBundle
        {
            CandidateRepositories = new[]
            {
                new CandidateRepository { Repo = "maybe", Confidence = 40, Reason = "weak match" },
            },
        };
        Assert.False(bundle.HasConfidentRepository());
    }

    [Fact]
    public void ContextAmbiguousMultipleReposIsNotConfident()
    {
        var bundle = new ContextBundle
        {
            CandidateRepositories = new[]
            {
                new CandidateRepository { Repo = "a", Confidence = 85, Reason = "match" },
                new CandidateRepository { Repo = "b", Confidence = 80, Reason = "also match" },
            },
        };
        // Two repos above threshold is ambiguous -> needs human confirmation.
        Assert.False(bundle.HasConfidentRepository());
    }

    [Fact]
    public void DeliveryPlanRoundTripsThroughJson()
    {
        var plan = TestData.SampleDeliveryPlan();
        var dumped = FoundryJson.Serialize(plan);
        var restored = FoundryJson.Deserialize<DeliveryPlan>(dumped);
        Assert.Equal(FoundryJson.Canonical(plan), FoundryJson.Canonical(restored));
        Assert.Equal(1, restored.ImplementationSteps[0].Step);
    }

    [Fact]
    public void DeliveryPlanStepMustBePositive()
    {
        const string json = """
        {
            "goal": "g",
            "implementation_steps": [
                {"step": 0, "description": "d", "expected_output": "o"}
            ]
        }
        """;
        Assert.Throws<SchemaValidationException>(() => FoundryJson.Deserialize<DeliveryPlan>(json));
    }
}
