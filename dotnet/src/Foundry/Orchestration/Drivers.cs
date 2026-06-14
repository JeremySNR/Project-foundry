// Run drivers - the single seam for *how* a run is executed.
//
// The API (and any other entrypoint) drives runs through an IRunDriver rather
// than calling the orchestrator directly. This gives one place that owns the
// "approve => approve + dispatch, reject => reject" semantics, and a clean
// swap point between execution strategies:
//
// - InlineDriver - run the steps synchronously in-process (the default; fully
//   tested here).
// - A future durable driver (e.g. Temporal) implements this same interface:
//   Start becomes client.StartWorkflow and SubmitDecision / ObservePr become
//   signal calls.

using Foundry.Schemas;

namespace Foundry.Orchestration;

public static class Decisions
{
    // Human decision verbs (match the /foundry <command> approval commands).
    public const string Approve = "approve";
    public const string Reject = "reject";
    public const string Stop = "stop";
}

public interface IRunDriver
{
    string Start(RawTicket ticket, string triggerType, string? createdBy = null);

    void SubmitDecision(string runId, string decision, string user, IReadOnlySet<ApprovalRole>? roles = null);

    void ObservePr(string runId, PullRequestState prState);
}

/// <summary>Execute run steps synchronously via the orchestrator, in-process.</summary>
public sealed class InlineDriver : IRunDriver
{
    private readonly FoundryOrchestrator _orchestrator;

    public InlineDriver(FoundryOrchestrator orchestrator)
    {
        _orchestrator = orchestrator;
    }

    public string Start(RawTicket ticket, string triggerType, string? createdBy = null) =>
        _orchestrator.IntakeAndPlan(ticket, triggerType, createdBy);

    public void SubmitDecision(
        string runId, string decision, string user, IReadOnlySet<ApprovalRole>? roles = null)
    {
        switch (decision)
        {
            case Decisions.Approve:
                _orchestrator.Approve(runId, user, roles ?? new HashSet<ApprovalRole>());
                try
                {
                    _orchestrator.DispatchAgent(runId);
                }
                catch (OrchestratorException)
                {
                    // A policy block (e.g. human-only work) already set the run
                    // to blocked; that is the outcome, not an error to surface here.
                }
                break;
            case Decisions.Reject:
                _orchestrator.Reject(runId, user);
                break;
            case Decisions.Stop:
                _orchestrator.Stop(runId, user);
                break;
            default:
                throw new ArgumentException($"unsupported decision '{decision}'");
        }
    }

    public void ObservePr(string runId, PullRequestState prState) =>
        _orchestrator.RecordPr(runId, prState);
}
