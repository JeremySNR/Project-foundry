// Structured-LLM abstraction for the intelligence engines.
//
// The LLM-backed engines depend only on IStructuredLlm - "given a system
// prompt, a user prompt and a schema name, return a parsed JSON object". This
// keeps the engines testable with FakeStructuredLlm (no key, no network) and
// isolates every vendor SDK detail behind one seam: implement IStructuredLlm
// over the OpenAI .NET SDK (or any other client) to go live.
//
// Note vs the Python port: Pydantic generates the JSON schema from the model;
// .NET 8 has no built-in JSON schema generation, so the schema contract here
// is the schema *name* plus strict deserialisation into the artifact type
// (unknown fields rejected, ranges validated). The governance result is the
// same: invalid model output never becomes an artifact.

using System.Text.Json;
using System.Text.Json.Nodes;
using Foundry.Schemas;

namespace Foundry.Engines;

/// <summary>Raised when the LLM call or its response cannot be used.</summary>
public class LlmException : Exception
{
    public LlmException(string message) : base(message) { }
}

public interface IStructuredLlm
{
    JsonObject Generate(string system, string user, string schemaName);
}

/// <summary>Test double that returns canned responses and records the prompts it saw.</summary>
public sealed class FakeStructuredLlm : IStructuredLlm
{
    private readonly Queue<JsonObject> _responses;

    public FakeStructuredLlm(IEnumerable<JsonObject> responses)
    {
        _responses = new Queue<JsonObject>(responses);
    }

    public List<(string System, string User, string SchemaName)> Calls { get; } = new();

    public JsonObject Generate(string system, string user, string schemaName)
    {
        Calls.Add((system, user, schemaName));
        if (_responses.Count == 0)
        {
            throw new LlmException("FakeStructuredLlm ran out of canned responses");
        }
        return _responses.Dequeue();
    }
}

/// <summary>
/// LLM-backed ticket analyzer.
///
/// Implements ITicketAnalyzer using an IStructuredLlm. This is the
/// *pre-approval gate* intelligence: judge readiness, surface what's missing,
/// and normalise acceptance criteria from natural-language tickets - the thing
/// the heuristic analyzer can't actually do. It deliberately does NOT plan the
/// implementation; that stays with the coding agent.
///
/// Robustness: the model output is validated against the TicketAnalysis schema
/// and retried with corrective feedback; identity fields are overwritten from
/// the ticket so a hallucinated id/title can't slip through.
/// </summary>
public sealed class LlmTicketAnalyzer : ITicketAnalyzer
{
    private const string SystemPrompt = """
You analyse engineering tickets. You do NOT write code and you do NOT produce an \
implementation plan. Classify the work, identify missing information, extract or \
normalise acceptance criteria, and decide whether the ticket is ready to implement.

Hard rules:
- Return ONLY a JSON object matching the TicketAnalysis schema.
- Do not invent facts. List anything you infer under "assumptions", separate from facts.
- If the ticket is unclear or lacks acceptance criteria, set implementation_readiness \
to "needs_clarification" and list what is missing.
- If the ticket is a question rather than a unit of work, set "not_suitable".
- For a bug, missing reproduction steps means it is not ready.
- Only set "ready" when the work is clear enough that a coding agent could start.
""";

    private readonly IStructuredLlm _llm;
    private readonly int _maxAttempts;

    public LlmTicketAnalyzer(IStructuredLlm llm, int maxAttempts = 2)
    {
        _llm = llm;
        _maxAttempts = Math.Max(1, maxAttempts);
    }

    public TicketAnalysis Analyse(RawTicket ticket)
    {
        var user = RenderUser(ticket);
        Exception? lastError = null;

        for (var attempt = 0; attempt < _maxAttempts; attempt++)
        {
            var prompt = attempt == 0 ? user : $"{user}\n\n{Feedback(lastError)}";
            var raw = _llm.Generate(SystemPrompt, prompt, "TicketAnalysis");
            // Identity comes from the ticket, never the model.
            raw["ticket_id"] = string.IsNullOrEmpty(ticket.IssueKey) ? ticket.IssueId : ticket.IssueKey;
            raw["title"] = ticket.Title;
            try
            {
                return FoundryJson.Deserialize<TicketAnalysis>(
                    raw.ToJsonString(new JsonSerializerOptions()));
            }
            catch (SchemaValidationException exc)
            {
                lastError = exc;
            }
        }

        throw new LlmException(
            $"LLM analyzer could not produce a valid TicketAnalysis after "
            + $"{_maxAttempts} attempts: {lastError?.Message}");
    }

    private static string RenderUser(RawTicket ticket)
    {
        var parts = new List<string>
        {
            $"Issue: {(string.IsNullOrEmpty(ticket.IssueKey) ? ticket.IssueId : ticket.IssueKey)}",
            $"Title: {ticket.Title}",
            $"Labels: {(ticket.Labels.Count > 0 ? string.Join(", ", ticket.Labels) : "(none)")}",
            "",
            "Description:",
            string.IsNullOrEmpty(ticket.Description) ? "(empty)" : ticket.Description,
        };
        if (ticket.Comments.Count > 0)
        {
            parts.Add("");
            parts.Add("Comments:");
            parts.AddRange(ticket.Comments);
        }
        return string.Join("\n", parts);
    }

    private static string Feedback(Exception? error) =>
        "Your previous response was invalid and rejected by the schema "
        + $"validator:\n{error?.Message}\nReturn a corrected JSON object only.";
}
