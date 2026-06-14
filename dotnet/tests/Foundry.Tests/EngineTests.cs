// Tests for the deterministic reference intelligence engines (mirrors test_engines.py).

using Foundry.Engines;
using Foundry.Schemas;
using Xunit;

namespace Foundry.Tests;

public class EngineTests
{
    private const string ReadyDescription = """
We want customers to be able to favourite items.

Acceptance Criteria:
- A favourites button exists on each item
- Favourites persist across sessions
""";

    private static RawTicket ReadyTicket(
        string title = "Add customer favourites",
        string? description = null,
        IReadOnlyList<LinkedResource>? linkedResources = null) =>
        TestData.ReadyTicket(
            title: title,
            description: description ?? ReadyDescription,
            linkedResources: linkedResources);

    // -- analyzer -----------------------------------------------------------------

    [Fact]
    public void ClearFeatureWithAcIsReady()
    {
        var analysis = new HeuristicAnalyzer().Analyse(ReadyTicket());
        Assert.Equal(WorkType.Feature, analysis.WorkType);
        Assert.Equal(ImplementationReadiness.Ready, analysis.ImplementationReadiness);
        Assert.Equal(2, analysis.AcceptanceCriteria.Count);
        Assert.True(analysis.IsReadyToBuild);
    }

    [Fact]
    public void VagueFeatureNeedsClarification()
    {
        var ticket = new RawTicket { IssueId = "i", IssueKey = "LIN-9", Title = "Make it nicer" };
        var analysis = new HeuristicAnalyzer().Analyse(ticket);
        Assert.Equal(ImplementationReadiness.NeedsClarification, analysis.ImplementationReadiness);
        Assert.Contains("acceptance criteria", analysis.MissingInformation);
    }

    [Fact]
    public void BugWithoutReproNeedsClarification()
    {
        var ticket = new RawTicket
        {
            IssueId = "i",
            IssueKey = "LIN-7",
            Title = "Checkout button is broken",
            Description = "It errors sometimes.",
        };
        var analysis = new HeuristicAnalyzer().Analyse(ticket);
        Assert.Equal(WorkType.Bug, analysis.WorkType);
        Assert.Contains("reproduction steps", analysis.MissingInformation);
        Assert.False(analysis.IsReadyToBuild);
    }

    [Fact]
    public void EmptyTicketIsNotBuildable()
    {
        var analysis = new HeuristicAnalyzer().Analyse(
            new RawTicket { IssueId = "i", IssueKey = "LIN-0", Title = "x" });
        Assert.False(analysis.IsReadyToBuild);
        Assert.True(analysis.AmbiguityScore >= 50);
    }

    [Fact]
    public void QuestionIsNotSuitable()
    {
        var ticket = new RawTicket
        {
            IssueId = "i",
            IssueKey = "LIN-Q",
            Title = "Question: how do we handle refunds?",
        };
        var analysis = new HeuristicAnalyzer().Analyse(ticket);
        Assert.Equal(WorkType.Question, analysis.WorkType);
        Assert.Equal(ImplementationReadiness.NotSuitable, analysis.ImplementationReadiness);
    }

    [Fact]
    public void AnalysisIsDeterministic()
    {
        var a = new HeuristicAnalyzer().Analyse(ReadyTicket());
        var b = new HeuristicAnalyzer().Analyse(ReadyTicket());
        Assert.Equal(FoundryJson.Canonical(a), FoundryJson.Canonical(b));
    }

    // -- enrichment ---------------------------------------------------------------

    [Fact]
    public void KnownRepoIsConfident()
    {
        var ticket = ReadyTicket();
        var analysis = new HeuristicAnalyzer().Analyse(ticket);
        var context = new StaticContextEnricher().Enrich(ticket, analysis);
        Assert.True(context.HasConfidentRepository());
        Assert.Equal("customer-web", context.BestRepository!.Repo);
    }

    [Fact]
    public void NoRepoSignalYieldsUnknowns()
    {
        var ticket = new RawTicket
        {
            IssueId = "i", IssueKey = "LIN-3", Title = "Do a thing", Description = "x",
        };
        var analysis = new HeuristicAnalyzer().Analyse(ticket);
        var context = new StaticContextEnricher().Enrich(ticket, analysis);
        Assert.False(context.HasConfidentRepository());
        Assert.NotEmpty(context.Unknowns);
    }

    [Fact]
    public void CatalogKeywordMatch()
    {
        var ticket = new RawTicket
        {
            IssueId = "i",
            IssueKey = "LIN-4",
            Title = "Improve favourites",
            Description = "Acceptance Criteria:\n- favourites work",
        };
        var analysis = new HeuristicAnalyzer().Analyse(ticket);
        var enricher = new StaticContextEnricher(new Dictionary<string, IReadOnlyList<string>>
        {
            ["customer-web"] = new[] { "favourites" },
        });
        var context = enricher.Enrich(ticket, analysis);
        Assert.Contains(context.CandidateRepositories, c => c.Repo == "customer-web");
    }

    [Fact]
    public void LinkedPrSurfacesRelatedPr()
    {
        var ticket = ReadyTicket(linkedResources: new[]
        {
            new LinkedResource { Kind = "github_pr", Url = "https://github.com/x/y/pull/5", Repo = "y" },
        });
        var analysis = new HeuristicAnalyzer().Analyse(ticket);
        var context = new StaticContextEnricher().Enrich(ticket, analysis);
        Assert.Contains("https://github.com/x/y/pull/5", context.RelatedPrs);
    }

    // -- risk ---------------------------------------------------------------------

    [Fact]
    public void CleanFeatureIsLowRisk()
    {
        var ticket = ReadyTicket();
        var analysis = new HeuristicAnalyzer().Analyse(ticket);
        var context = new StaticContextEnricher().Enrich(ticket, analysis);
        var risk = new HeuristicRiskClassifier().Classify(ticket, analysis, context);
        Assert.Equal(OverallRisk.Low, risk.OverallRisk);
        Assert.Equal(AgentMode.DraftPr, risk.AllowedAgentMode);
    }

    [Fact]
    public void AuthTicketFlagsSensitiveAndRequiresEngineering()
    {
        var ticket = ReadyTicket(
            title: "Change login session token handling",
            description: "Acceptance Criteria:\n- auth tokens rotate\nThis touches auth/login.");
        var analysis = new HeuristicAnalyzer().Analyse(ticket);
        var context = new StaticContextEnricher().Enrich(ticket, analysis);
        var risk = new HeuristicRiskClassifier().Classify(ticket, analysis, context);
        Assert.True(risk.SensitiveAreas.Auth);
        Assert.Equal(OverallRisk.High, risk.OverallRisk);
        Assert.Contains(ApprovalRole.Engineering, risk.RequiredApprovals);
        Assert.Equal(AgentMode.HumanOnly, risk.AllowedAgentMode);
    }

    [Fact]
    public void NoRepoMatchIsBlockedRisk()
    {
        var ticket = new RawTicket
        {
            IssueId = "i",
            IssueKey = "LIN-5",
            Title = "Add favourites",
            Description = "Acceptance Criteria:\n- it works",
        };
        var analysis = new HeuristicAnalyzer().Analyse(ticket);
        var context = new StaticContextEnricher().Enrich(ticket, analysis); // no repo signal
        var risk = new HeuristicRiskClassifier().Classify(ticket, analysis, context);
        Assert.Equal(OverallRisk.Blocked, risk.OverallRisk);
    }

    // -- planner ------------------------------------------------------------------

    [Fact]
    public void ReadyPlanHasAgentInstructions()
    {
        var ticket = ReadyTicket();
        var analysis = new HeuristicAnalyzer().Analyse(ticket);
        var context = new StaticContextEnricher().Enrich(ticket, analysis);
        var risk = new HeuristicRiskClassifier().Classify(ticket, analysis, context);
        var plan = new TemplatePlanner().Plan(ticket, analysis, context, risk);
        Assert.NotNull(plan.AgentInstructions);
        Assert.Contains("LIN-123", plan.AgentInstructions);
        Assert.Equal(2, plan.ImplementationSteps.Count);
        Assert.Equal(new[] { "customer-web" }, plan.AffectedRepositories);
    }

    [Fact]
    public void NotReadyPlanHasNoAgentInstructions()
    {
        var ticket = new RawTicket { IssueId = "i", IssueKey = "LIN-6", Title = "Vague" };
        var analysis = new HeuristicAnalyzer().Analyse(ticket);
        var context = new StaticContextEnricher().Enrich(ticket, analysis);
        var risk = new HeuristicRiskClassifier().Classify(ticket, analysis, context);
        var plan = new TemplatePlanner().Plan(ticket, analysis, context, risk);
        Assert.Null(plan.AgentInstructions);
    }

    [Fact]
    public void BranchNameIsSanitised()
    {
        var ticket = ReadyTicket(title: "Add Customer Favourites!!!");
        Assert.Equal("foundry/lin-123-add-customer-favourites", Planning.BranchNameFor(ticket));
    }

    // -- diff-aware risk classification --------------------------------------------

    [Fact]
    public void SensitiveAreasForPathsMatchesGlobs()
    {
        var globs = new Dictionary<string, IReadOnlyList<string>>
        {
            ["auth"] = new[] { "**/auth/**", "**/login/**" },
            ["payments"] = new[] { "**/billing/**" },
        };
        var touched = Globbing.SensitiveAreasForPaths(
            new[] { "src/auth/session.ts", "billing/charge.py", "src/ui/button.tsx" },
            globs);
        Assert.Equal(new[] { "auth", "payments" }, touched.Keys);
        Assert.Equal(new[] { "src/auth/session.ts" }, touched["auth"]);
        Assert.Equal(new[] { "billing/charge.py" }, touched["payments"]);
    }

    [Fact]
    public void SensitiveAreasForPathsEmptyWhenClean()
    {
        var touched = Globbing.SensitiveAreasForPaths(
            new[] { "src/ui/button.tsx" },
            new Dictionary<string, IReadOnlyList<string>> { ["auth"] = new[] { "**/auth/**" } });
        Assert.Empty(touched);
    }

    [Fact]
    public void GlobMatchHandlesLeadingDoublestar()
    {
        // fnmatch alone would miss a top-level directory against "**/x/**".
        Assert.True(Globbing.GlobMatch("auth/handler.py", "**/auth/**"));
        Assert.True(Globbing.GlobMatch("src/auth/handler.py", "**/auth/**"));
        Assert.False(Globbing.GlobMatch("src/author/file.py", "**/auth/**"));
    }
}
