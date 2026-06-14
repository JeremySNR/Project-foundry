// Ticket Intelligence Engine - analysis stage.
//
// ITicketAnalyzer is the interface the orchestrator depends on. A real
// implementation will be an LLM agent constrained to emit TicketAnalysis JSON.
// HeuristicAnalyzer is a deterministic reference implementation: it makes the
// contract exercisable end-to-end with no model, and encodes the hard rules
// ("no acceptance criteria -> do not start coding"; "bug without reproduction
// -> needs clarification") so they are tested regardless of which backend
// produces the analysis.

using System.Text.RegularExpressions;
using Foundry.Schemas;

namespace Foundry.Engines;

public interface ITicketAnalyzer
{
    TicketAnalysis Analyse(RawTicket ticket);
}

/// <summary>Deterministic, rule-based reference analyzer (no model required).</summary>
public sealed partial class HeuristicAnalyzer : ITicketAnalyzer
{
    // Keyword -> work type. All keywords are matched; the type with the most
    // hits wins. Priority is used as a tiebreaker: earlier entries beat later ones.
    private static readonly (WorkType WorkType, string[] Keywords)[] WorkTypeKeywords =
    {
        (WorkType.Incident, new[] { "incident", "outage", "sev1", "sev2", "production down" }),
        (WorkType.Bug, new[] { "bug", "broken", "crash", "regression", "fails", "traceback", "not working" }),
        (WorkType.TechDebt, new[] { "refactor", "tech debt", "cleanup", "deprecate", "migrate code" }),
        (WorkType.Question, new[] { "question", "how do we", "should we", "what is the" }),
    };

    private static readonly string[] ReproductionHints =
    {
        "steps to reproduce", "reproduce", "repro:", "stack trace", "logs",
    };

    // Headings under which acceptance criteria are commonly listed.
    // Handles plain text, markdown bold (**...**), and ATX headings (## ...).
    [GeneratedRegex(
        @"(?:\*{1,2}|#{1,6}\s*)?(acceptance criteria|acceptance:|ac:|definition of done|dod:)(?:\*{1,2})?",
        RegexOptions.IgnoreCase)]
    private static partial Regex AcHeading();

    [GeneratedRegex(@"^\s*(?:[-*•]|\d+[.):])\s+(.*\S)\s*$")]
    private static partial Regex Bullet();

    public TicketAnalysis Analyse(RawTicket ticket)
    {
        var blob = ticket.TextBlob();
        var workType = DetectWorkType(blob);
        var acceptanceCriteria = ExtractAcceptanceCriteria(ticket.Description);

        var missing = new List<string>();
        if (acceptanceCriteria.Count == 0)
        {
            missing.Add("acceptance criteria");
        }
        if (workType == WorkType.Bug && !ReproductionHints.Any(blob.Contains))
        {
            missing.Add("reproduction steps");
        }
        if (string.IsNullOrWhiteSpace(ticket.Description))
        {
            missing.Add("a description of the desired outcome");
        }

        var readiness = Readiness(workType, acceptanceCriteria, missing);
        var ambiguity = AmbiguityScore(ticket, acceptanceCriteria, missing);
        var confidence = Math.Max(0, 100 - ambiguity);

        return new TicketAnalysis
        {
            TicketId = string.IsNullOrEmpty(ticket.IssueKey) ? ticket.IssueId : ticket.IssueKey,
            Title = ticket.Title,
            WorkType = workType,
            Summary = Summary(ticket, workType),
            UserProblem = string.IsNullOrWhiteSpace(ticket.Description) ? null : ticket.Description.Trim(),
            BusinessValue = null,
            AcceptanceCriteria = acceptanceCriteria,
            MissingInformation = missing,
            Assumptions = Array.Empty<string>(),
            AmbiguityScore = ambiguity,
            ImplementationReadiness = readiness,
            Confidence = confidence,
        };
    }

    /// <summary>Pick the work type with the most keyword hits; priority breaks ties.</summary>
    private static WorkType DetectWorkType(string blob)
    {
        var scores = new Dictionary<WorkType, int>();
        foreach (var (workType, keywords) in WorkTypeKeywords)
        {
            var count = keywords.Count(blob.Contains);
            if (count > 0)
            {
                scores[workType] = count;
            }
        }

        if (scores.Count == 0)
        {
            return WorkType.Feature;
        }

        var priority = WorkTypeKeywords
            .Select((entry, index) => (entry.WorkType, Index: index))
            .ToDictionary(p => p.WorkType, p => p.Index);
        return scores.Keys
            .OrderByDescending(wt => scores[wt])
            .ThenBy(wt => priority[wt])
            .First();
    }

    /// <summary>
    /// Pull bullet lines that follow an acceptance-criteria heading.
    ///
    /// Deterministic and forgiving: once a heading is seen, consecutive bullet
    /// lines are collected until a blank line or a non-bullet line ends the section.
    /// </summary>
    internal static List<string> ExtractAcceptanceCriteria(string description)
    {
        var criteria = new List<string>();
        var inSection = false;
        foreach (var line in description.Split('\n'))
        {
            if (AcHeading().IsMatch(line))
            {
                inSection = true;
                // Allow "Acceptance criteria: do the thing" on one line.
                var after = AcHeading().Replace(line, "").Trim(' ', ':', '-', '*', '#');
                if (after.Length > 0)
                {
                    criteria.Add(after);
                }
                continue;
            }
            if (inSection)
            {
                var match = Bullet().Match(line);
                if (match.Success)
                {
                    criteria.Add(match.Groups[1].Value.Trim());
                }
                else if (line.Trim().Length == 0)
                {
                    continue;
                }
                else
                {
                    // A non-blank, non-bullet line ends the section.
                    break;
                }
            }
        }
        return criteria;
    }

    private static ImplementationReadiness Readiness(
        WorkType workType, List<string> acceptanceCriteria, List<string> missing)
    {
        if (workType == WorkType.Question)
        {
            // A question is not a unit of implementable work.
            return ImplementationReadiness.NotSuitable;
        }
        if (acceptanceCriteria.Count == 0 || missing.Count > 0)
        {
            return ImplementationReadiness.NeedsClarification;
        }
        return ImplementationReadiness.Ready;
    }

    private static int AmbiguityScore(
        RawTicket ticket, List<string> acceptanceCriteria, List<string> missing)
    {
        var score = 0;
        if (acceptanceCriteria.Count == 0)
        {
            score += 40;
        }
        if (ticket.Description.Trim().Length < 40)
        {
            score += 25;
        }
        // Each missing field adds proportionally less weight to avoid saturation.
        score += missing.Select((_, i) => Math.Min(20, 15 - i * 3)).Sum();
        return Math.Min(score, 100);
    }

    private static string Summary(RawTicket ticket, WorkType workType) =>
        $"{Wire.ToTitle(workType.ToWire())}: {ticket.Title}".Trim();
}
