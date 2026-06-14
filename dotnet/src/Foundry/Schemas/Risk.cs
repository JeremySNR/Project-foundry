// RiskAssessment - classification produced before any agent is launched.
//
// Risk classification is advisory input to the policy gate. The *hard*
// decisions (what is allowed) live in Foundry.Policy, not in the LLM that
// fills this in.

namespace Foundry.Schemas;

public sealed record SensitiveAreas
{
    public bool Auth { get; init; }
    public bool Payments { get; init; }
    public bool CustomerData { get; init; }
    public bool Pii { get; init; }
    public bool DatabaseMigration { get; init; }
    public bool Infrastructure { get; init; }
    public bool ProductionDeploy { get; init; }

    private IEnumerable<(string Name, bool Flagged)> Flags()
    {
        yield return ("auth", Auth);
        yield return ("payments", Payments);
        yield return ("customer_data", CustomerData);
        yield return ("pii", Pii);
        yield return ("database_migration", DatabaseMigration);
        yield return ("infrastructure", Infrastructure);
        yield return ("production_deploy", ProductionDeploy);
    }

    public bool AnySet() => Flags().Any(f => f.Flagged);

    /// <summary>Names of sensitive areas that are flagged true, in deterministic order.</summary>
    public IReadOnlyList<string> Names() =>
        Flags().Where(f => f.Flagged).Select(f => f.Name).OrderBy(n => n, StringComparer.Ordinal).ToList();
}

public sealed record RiskAssessment
{
    public required OverallRisk OverallRisk { get; init; }

    public IReadOnlyList<string> RiskReasons { get; init; } = Array.Empty<string>();

    public SensitiveAreas SensitiveAreas { get; init; } = new();

    public required AgentMode AllowedAgentMode { get; init; }

    public IReadOnlyList<ApprovalRole> RequiredApprovals { get; init; } = Array.Empty<ApprovalRole>();

    /// <summary>Links back to the recorded OPA/policy decision that produced this view.</summary>
    public string? PolicyDecisionId { get; init; }
}
