// Context enrichment stage.
//
// StaticContextEnricher is a deterministic reference that derives candidate
// repositories from explicit signals on the ticket plus an optional repo
// catalog of keywords. Core rule: never assume the repo from the title alone;
// attach a confidence to every candidate.

using Foundry.Schemas;

namespace Foundry.Engines;

public interface IContextEnricher
{
    ContextBundle Enrich(RawTicket ticket, TicketAnalysis analysis);
}

/// <summary>Reference enricher driven by explicit ticket signals + a keyword catalog.</summary>
public sealed class StaticContextEnricher : IContextEnricher
{
    // Confidence assigned to repositories surfaced by each kind of signal.
    private const int ExplicitRepoConfidence = 90;
    private const int LinkedRepoConfidence = 85;

    private readonly IReadOnlyDictionary<string, IReadOnlyList<string>> _catalog;
    private readonly IReadOnlyList<string> _defaultTestCommands;

    public StaticContextEnricher(
        IReadOnlyDictionary<string, IReadOnlyList<string>>? repoCatalog = null,
        IReadOnlyList<string>? defaultTestCommands = null)
    {
        // repo name -> keywords that, if present in the ticket, suggest that repo.
        _catalog = repoCatalog ?? new Dictionary<string, IReadOnlyList<string>>();
        _defaultTestCommands = defaultTestCommands ?? Array.Empty<string>();
    }

    public ContextBundle Enrich(RawTicket ticket, TicketAnalysis analysis)
    {
        var blob = ticket.TextBlob();
        var candidates = new Dictionary<string, CandidateRepository>();

        void Consider(string repo, int confidence, string reason)
        {
            if (!candidates.TryGetValue(repo, out var existing) || confidence > existing.Confidence)
            {
                candidates[repo] = new CandidateRepository
                {
                    Repo = repo,
                    Confidence = confidence,
                    Reason = reason,
                };
            }
        }

        foreach (var repo in ticket.KnownRepositories)
        {
            Consider(repo, ExplicitRepoConfidence, "Explicitly associated with the issue.");
        }

        foreach (var link in ticket.LinkedResources)
        {
            if (link.Repo is not null)
            {
                Consider(link.Repo, LinkedRepoConfidence, $"Linked {link.Kind} points at this repository.");
            }
        }

        foreach (var (repo, keywords) in _catalog)
        {
            var hits = keywords.Where(k => blob.Contains(k.ToLowerInvariant())).ToList();
            if (hits.Count > 0)
            {
                // Confidence scales with keyword hits. A single hit reaches 60%
                // (below the 70% dispatch threshold) so one coincidental keyword
                // cannot trigger autonomous work. Two independent hits are needed
                // to cross the threshold.
                var confidence = Math.Min(50 + 10 * hits.Count, 95);
                Consider(repo, confidence,
                    $"Ticket mentions {string.Join(", ", hits.OrderBy(h => h, StringComparer.Ordinal))}.");
            }
        }

        var relatedPrs = ticket.LinkedResources
            .Where(r => r.Kind == "github_pr").Select(r => r.Url).ToList();
        var relatedIssues = ticket.LinkedResources
            .Where(r => r.Kind == "github_issue").Select(r => r.Url).ToList();
        var unknowns = candidates.Count > 0
            ? new List<string>()
            : new List<string> { "No candidate repository could be identified." };

        return new ContextBundle
        {
            CandidateRepositories = candidates.Values
                .OrderByDescending(c => c.Confidence).ToList(),
            RelatedPrs = relatedPrs,
            RelatedIssues = relatedIssues,
            TestCommands = _defaultTestCommands.ToList(),
            Unknowns = unknowns,
        };
    }
}
