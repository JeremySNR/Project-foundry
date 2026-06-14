// Risk classification stage.
//
// Produces a RiskAssessment from the ticket text and context. This is
// *advisory* input to the policy gate - it flags sensitive areas and proposes
// a risk level, but the hard allow/deny decision is made by Foundry.Policy.

using System.Text;
using System.Text.RegularExpressions;
using Foundry.Schemas;

namespace Foundry.Engines;

public static class Globbing
{
    /// <summary>
    /// fnmatch-style matching (Python semantics: '*' crosses '/' boundaries)
    /// with a usable '**/' prefix: '**/auth/**' also matches a path that
    /// *starts* with 'auth/' (fnmatch alone would require a leading slash).
    /// </summary>
    public static bool GlobMatch(string path, string pattern)
    {
        if (FnMatch(path, pattern))
        {
            return true;
        }
        return pattern.StartsWith("**/") && FnMatch(path, pattern[3..]);
    }

    /// <summary>Python fnmatch.fnmatchcase: '*' -> '.*', '?' -> '.', '[seq]' kept.</summary>
    public static bool FnMatch(string path, string pattern) =>
        Regex.IsMatch(path, Translate(pattern), RegexOptions.Singleline);

    internal static string Translate(string pattern)
    {
        var regex = new StringBuilder("^");
        var i = 0;
        while (i < pattern.Length)
        {
            var c = pattern[i];
            i += 1;
            switch (c)
            {
                case '*':
                    regex.Append(".*");
                    break;
                case '?':
                    regex.Append('.');
                    break;
                case '[':
                    var j = i;
                    if (j < pattern.Length && pattern[j] == '!')
                    {
                        j += 1;
                    }
                    if (j < pattern.Length && pattern[j] == ']')
                    {
                        j += 1;
                    }
                    while (j < pattern.Length && pattern[j] != ']')
                    {
                        j += 1;
                    }
                    if (j >= pattern.Length)
                    {
                        regex.Append(@"\[");
                    }
                    else
                    {
                        var inner = pattern[i..j].Replace(@"\", @"\\");
                        regex.Append('[');
                        if (inner.StartsWith('!'))
                        {
                            regex.Append('^').Append(inner[1..]);
                        }
                        else
                        {
                            regex.Append(inner);
                        }
                        regex.Append(']');
                        i = j + 1;
                    }
                    break;
                default:
                    regex.Append(Regex.Escape(c.ToString()));
                    break;
            }
        }
        regex.Append('$');
        return regex.ToString();
    }

    /// <summary>
    /// Classify changed file paths against sensitive-area globs.
    ///
    /// This is the diff-aware half of risk classification: the upfront pass
    /// reads the *ticket text*, but the risk that matters materialises in the
    /// *diff*. Returns {area: [matching files...]} for every area actually touched.
    /// </summary>
    public static SortedDictionary<string, List<string>> SensitiveAreasForPaths(
        IReadOnlyList<string> files,
        IReadOnlyDictionary<string, IReadOnlyList<string>> globsMap)
    {
        var touched = new SortedDictionary<string, List<string>>(StringComparer.Ordinal);
        foreach (var path in files)
        {
            foreach (var (area, patterns) in globsMap)
            {
                if (patterns.Any(p => GlobMatch(path, p)))
                {
                    if (!touched.TryGetValue(area, out var list))
                    {
                        touched[area] = list = new List<string>();
                    }
                    list.Add(path);
                }
            }
        }
        foreach (var list in touched.Values)
        {
            list.Sort(StringComparer.Ordinal);
        }
        return touched;
    }
}

public interface IRiskClassifier
{
    RiskAssessment Classify(RawTicket ticket, TicketAnalysis analysis, ContextBundle context);
}

/// <summary>Keyword-driven reference risk classifier.</summary>
public sealed class HeuristicRiskClassifier : IRiskClassifier
{
    // Keyword signals for each sensitive area. Prefer multi-word phrases over
    // single words to reduce false positives (e.g. "error" is not a payment
    // signal, "checkout" alone doesn't mean payments, "infra" alone is too broad).
    private static readonly IReadOnlyDictionary<string, string[]> SensitiveKeywords =
        new Dictionary<string, string[]>
        {
            ["auth"] = new[]
            {
                "oauth", "sso", "session token", "login flow", "authentication", "authorisation",
                "authorization", "access token", "jwt", "password reset",
            },
            ["payments"] = new[]
            {
                "payment", "billing", "stripe", "invoice", "payment gateway",
                "credit card", "card number", "transaction",
            },
            ["customer_data"] = new[] { "customer data", "customer record", "personal data" },
            ["pii"] = new[]
            {
                "pii", "gdpr", "email address", "phone number", "passport",
                "date of birth", "national insurance", "social security",
            },
            ["database_migration"] = new[]
            {
                "migration", "schema change", "alter table", "drop column",
                "drop table", "add column",
            },
            ["infrastructure"] = new[]
            {
                "terraform", "kubernetes", "helm chart", "deployment config",
                "infrastructure as code", "k8s manifest",
            },
            ["production_deploy"] = new[]
            {
                "deploy to production", "prod deploy", "release to prod", "production release",
            },
        };

    public RiskAssessment Classify(RawTicket ticket, TicketAnalysis analysis, ContextBundle context)
    {
        // Use RiskBlob (title + description only) to avoid stale comments
        // inflating risk scores.
        var blob = ticket.RiskBlob();
        bool Flag(string area) => SensitiveKeywords[area].Any(blob.Contains);
        var sensitive = new SensitiveAreas
        {
            Auth = Flag("auth"),
            Payments = Flag("payments"),
            CustomerData = Flag("customer_data"),
            Pii = Flag("pii"),
            DatabaseMigration = Flag("database_migration"),
            Infrastructure = Flag("infrastructure"),
            ProductionDeploy = Flag("production_deploy"),
        };

        var reasons = sensitive.Names()
            .Select(area => $"Ticket text suggests it touches '{area}'.")
            .ToList();

        var overall = ComputeOverallRisk(sensitive, context);
        if (overall == OverallRisk.Blocked)
        {
            reasons.Add("No confident repository match; work cannot be scoped.");
        }

        return new RiskAssessment
        {
            OverallRisk = overall,
            RiskReasons = reasons,
            SensitiveAreas = sensitive,
            AllowedAgentMode = ComputeAgentMode(overall),
            RequiredApprovals = ComputeRequiredApprovals(sensitive),
        };
    }

    private static OverallRisk ComputeOverallRisk(SensitiveAreas sensitive, ContextBundle context)
    {
        if (!context.HasConfidentRepository())
        {
            return OverallRisk.Blocked;
        }
        if (sensitive.ProductionDeploy || sensitive.DatabaseMigration)
        {
            return OverallRisk.High;
        }
        if (sensitive.Auth || sensitive.Payments || sensitive.CustomerData || sensitive.Pii)
        {
            return OverallRisk.High;
        }
        if (sensitive.Infrastructure)
        {
            return OverallRisk.Medium;
        }
        return OverallRisk.Low;
    }

    private static List<ApprovalRole> ComputeRequiredApprovals(SensitiveAreas sensitive)
    {
        var required = new List<ApprovalRole>();
        if (sensitive.Auth || sensitive.Infrastructure)
        {
            required.Add(ApprovalRole.Engineering);
        }
        if (sensitive.CustomerData || sensitive.Pii || sensitive.Payments)
        {
            required.Add(ApprovalRole.Security);
        }
        return required.Distinct().ToList();
    }

    private static AgentMode ComputeAgentMode(OverallRisk overall) =>
        overall is OverallRisk.Blocked or OverallRisk.High
            ? AgentMode.HumanOnly
            : AgentMode.DraftPr;
}
