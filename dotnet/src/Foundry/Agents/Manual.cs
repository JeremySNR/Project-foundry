// Providers that do not depend on an external automation API.
//
// ManualProvider is the safe default for the MVP: it records an approved job
// for a human to pick up (e.g. launch Cursor by hand) rather than dispatching
// autonomously. InMemoryFakeProvider simulates the full lifecycle for tests
// and integration checks against a fake repo.

using Foundry.Schemas;

namespace Foundry.Agents;

/// <summary>Hands the approved plan to a human. Never writes to a repo itself.</summary>
public sealed class ManualProvider : CodingAgentProvider
{
    public const string ProviderName = "manual";

    private readonly Dictionary<string, CodingAgentJobStatus> _jobs = new();

    public override string Name => ProviderName;

    protected override CodingAgentJob Dispatch(CodingAgentJobInput jobInput)
    {
        var jobId = $"manual-{Guid.NewGuid()}";
        _jobs[jobId] = new CodingAgentJobStatus
        {
            JobId = jobId,
            Provider = Name,
            Status = AgentJobStatus.Created,
            Branch = jobInput.BranchName,
        };
        return new CodingAgentJob { JobId = jobId, Provider = Name };
    }

    public override CodingAgentJobStatus GetJobStatus(string jobId) => _jobs[jobId];

    public override void CancelJob(string jobId)
    {
        if (_jobs.TryGetValue(jobId, out var status))
        {
            _jobs[jobId] = status with { Status = AgentJobStatus.Cancelled };
        }
    }
}

/// <summary>
/// Test double that simulates a provider creating a branch and a PR.
///
/// <see cref="Run"/> advances a created job to Succeeded with a synthetic PR
/// URL so integration tests can exercise the downstream PR monitor without GitHub.
/// </summary>
public sealed class InMemoryFakeProvider : CodingAgentProvider
{
    public const string ProviderName = "fake";

    private readonly Dictionary<string, CodingAgentJobStatus> _jobs = new();
    private readonly Dictionary<string, CodingAgentJobInput> _inputs = new();
    private readonly List<CodingAgentJobInput> _orderedInputs = new();
    private readonly bool _fail;

    public InMemoryFakeProvider(bool fail = false)
    {
        _fail = fail;
    }

    public override string Name => ProviderName;

    /// <summary>The job inputs this provider has received, in dispatch order.</summary>
    public IReadOnlyList<CodingAgentJobInput> Inputs => _orderedInputs;

    protected override CodingAgentJob Dispatch(CodingAgentJobInput jobInput)
    {
        var jobId = $"fake-{Guid.NewGuid()}";
        _inputs[jobId] = jobInput;
        _orderedInputs.Add(jobInput);
        _jobs[jobId] = new CodingAgentJobStatus
        {
            JobId = jobId,
            Provider = Name,
            Status = AgentJobStatus.Running,
            Branch = jobInput.BranchName,
        };
        return new CodingAgentJob { JobId = jobId, Provider = Name, Status = AgentJobStatus.Running };
    }

    /// <summary>Simulate the agent finishing its work.</summary>
    public CodingAgentJobStatus Run(string jobId)
    {
        var status = _jobs[jobId];
        var jobInput = _inputs[jobId];
        status = _fail
            ? status with { Status = AgentJobStatus.Failed, Error = "simulated provider failure" }
            : status with
            {
                Status = AgentJobStatus.Succeeded,
                PrUrl = $"https://github.com/example/{jobInput.Repo}/pull/1",
            };
        _jobs[jobId] = status;
        return status;
    }

    public override CodingAgentJobStatus GetJobStatus(string jobId) => _jobs[jobId];

    public override void CancelJob(string jobId)
    {
        if (_jobs.TryGetValue(jobId, out var status))
        {
            _jobs[jobId] = status with { Status = AgentJobStatus.Cancelled };
        }
    }
}
