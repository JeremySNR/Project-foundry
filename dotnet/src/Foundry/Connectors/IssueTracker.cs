// Connector contracts.
//
// Foundry sits *above* the tools it coordinates and talks to each through a
// thin adapter. IIssueTracker is the surface the orchestrator needs from a
// planning tool (Linear today): read the issue, write progress back, and move
// its state.
//
// Keeping this an interface means the orchestrator never references Linear
// directly, and tests use an in-memory fake.

using Foundry.Schemas;

namespace Foundry.Connectors;

public interface IIssueTracker
{
    RawTicket GetIssue(string issueId);

    void PostComment(string issueId, string body);

    void SetState(string issueId, string stateName);
}

/// <summary>Test double that records comments and state changes per issue.</summary>
public sealed class InMemoryIssueTracker : IIssueTracker
{
    private readonly Dictionary<string, RawTicket> _issues;

    public InMemoryIssueTracker(IDictionary<string, RawTicket>? issues = null)
    {
        _issues = issues is null ? new Dictionary<string, RawTicket>() : new Dictionary<string, RawTicket>(issues);
    }

    public Dictionary<string, List<string>> Comments { get; } = new();

    public Dictionary<string, string> States { get; } = new();

    public void AddIssue(RawTicket ticket) => _issues[ticket.IssueId] = ticket;

    public RawTicket GetIssue(string issueId) => _issues[issueId];

    public void PostComment(string issueId, string body)
    {
        if (!Comments.TryGetValue(issueId, out var list))
        {
            Comments[issueId] = list = new List<string>();
        }
        list.Add(body);
    }

    public void SetState(string issueId, string stateName) => States[issueId] = stateName;
}
