// Delivery planning stage.
//
// Turns a *ready* ticket plus verified context into a coding-agent-ready
// DeliveryPlan. Hard rule enforced here: AgentInstructions is only populated
// when the ticket is genuinely ready to build (acceptance criteria present)
// AND a confident repository exists. Otherwise the plan is produced for humans
// but carries no instructions an agent could act on.

using System.Text;
using Foundry.Schemas;

namespace Foundry.Engines;

public interface IDeliveryPlanner
{
    DeliveryPlan Plan(RawTicket ticket, TicketAnalysis analysis, ContextBundle context, RiskAssessment risk);
}

public static class Planning
{
    /// <summary>Default forbidden globs for the coding agent (also enforced by policy).</summary>
    public static readonly IReadOnlyList<string> DefaultForbiddenGlobs = new[]
    {
        "infra/**", "migrations/**", "**/.env*", "**/secrets/**",
    };

    /// <summary>Deterministic, sanitised branch name for a ticket.</summary>
    public static string BranchNameFor(RawTicket ticket)
    {
        var slugBuilder = new StringBuilder();
        foreach (var c in ticket.Title.ToLowerInvariant())
        {
            slugBuilder.Append(char.IsLetterOrDigit(c) ? c : '-');
        }
        var slug = slugBuilder.ToString().Trim('-');
        while (slug.Contains("--"))
        {
            slug = slug.Replace("--", "-");
        }
        slug = slug[..Math.Min(40, slug.Length)].Trim('-');
        var key = (string.IsNullOrEmpty(ticket.IssueKey) ? ticket.IssueId : ticket.IssueKey)
            .ToLowerInvariant();
        return slug.Length > 0 ? $"foundry/{key}-{slug}" : $"foundry/{key}";
    }
}

/// <summary>Reference planner that assembles a structured plan from upstream artifacts.</summary>
public sealed class TemplatePlanner : IDeliveryPlanner
{
    private const string InstructionTemplate = """
You are working on Linear issue {issue_key}: {title}.

Goal:
{goal}

Scope:
{scope}

Out of scope:
{out_of_scope}

Repository:
{repo}

Branch:
{branch}

Implementation plan:
{steps}

Constraints:
- Do not modify files matching: {forbidden}
- Do not add dependencies unless explicitly required.
- Do not perform database migrations.
- Do not change auth, payment, PII or infrastructure code.
- Stop and ask for human input if the change grows beyond the stated scope.

When you are done, open a draft PR whose description summarises what changed,
why, how it was tested, and any follow-ups.
""";

    public DeliveryPlan Plan(
        RawTicket ticket, TicketAnalysis analysis, ContextBundle context, RiskAssessment risk)
    {
        var bestRepo = context.BestRepository;
        var affected = bestRepo is null ? new List<string>() : new List<string> { bestRepo.Repo };

        var steps = analysis.AcceptanceCriteria
            .Select((criterion, index) => new ImplementationStep
            {
                Step = index + 1,
                Description = $"Satisfy acceptance criterion: {criterion}",
                ExpectedOutput = "Code + tests covering this criterion.",
            })
            .ToList();

        var testPlan = new TestPlan
        {
            UnitTests = analysis.AcceptanceCriteria.Select(c => $"Cover: {c}").ToList(),
            ManualChecks = context.TestCommands.ToList(),
        };

        var plan = new DeliveryPlan
        {
            Goal = analysis.Summary,
            Scope = analysis.AcceptanceCriteria.ToList(),
            OutOfScope = new[] { "Anything not listed in the acceptance criteria." },
            AffectedRepositories = affected,
            ExpectedFilesOrAreas = Array.Empty<string>(), // populated by richer context enrichers
            ImplementationSteps = steps,
            TestPlan = testPlan,
            RollbackConsiderations = new[] { "Revert the PR; no data migration involved." },
            OpenQuestions = analysis.MissingInformation.ToList(),
            AgentInstructions = null,
        };

        if (CanInstructAgent(analysis, context))
        {
            plan = plan with
            {
                AgentInstructions = RenderInstructions(ticket, plan, bestRepo!.Repo),
            };
        }
        return plan;
    }

    private static bool CanInstructAgent(TicketAnalysis analysis, ContextBundle context) =>
        analysis.IsReadyToBuild && context.HasConfidentRepository();

    private static string RenderInstructions(RawTicket ticket, DeliveryPlan plan, string repo)
    {
        var steps = string.Join("\n", plan.ImplementationSteps.Select(s => $"{s.Step}. {s.Description}"));
        var scope = plan.Scope.Count > 0
            ? string.Join("\n", plan.Scope.Select(s => $"- {s}"))
            : "- (none)";
        var outOfScope = string.Join("\n", plan.OutOfScope.Select(s => $"- {s}"));
        return InstructionTemplate
            .Replace("{issue_key}", string.IsNullOrEmpty(ticket.IssueKey) ? ticket.IssueId : ticket.IssueKey)
            .Replace("{title}", ticket.Title)
            .Replace("{goal}", plan.Goal)
            .Replace("{scope}", scope)
            .Replace("{out_of_scope}", outOfScope)
            .Replace("{repo}", repo)
            .Replace("{branch}", Planning.BranchNameFor(ticket))
            .Replace("{steps}", steps.Length > 0 ? steps : "(derive from acceptance criteria)")
            .Replace("{forbidden}", string.Join(", ", Planning.DefaultForbiddenGlobs));
    }
}
