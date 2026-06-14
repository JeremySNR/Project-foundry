// Claude Code coding-agent provider.
//
// Dispatches approved work to Claude Code running headless in the target
// repo's own CI via a GitHub Actions workflow_dispatch event. The repo
// installs a small reference workflow (see examples/claude-code-runner.yml in
// the repository root) that runs Claude Code with the governed instructions,
// pushes the branch and opens the PR. Foundry then observes that PR through
// the normal GitHub webhook (correlated by branch name, which Foundry chooses
// here).
//
// This keeps the trust boundary clean: Foundry never holds an Anthropic key -
// the *repo's* CI does - and the agent only ever receives the policy-gated
// instructions that passed the secret-leak guard in CreateJob.

using System.Text.Json;
using Foundry.Schemas;

namespace Foundry.Agents;

/// <summary>httpPost(url, jsonBody, headers) -> parsed JSON (or null for 204s).</summary>
public delegate JsonElement? JsonHttpPost(
    string url, JsonElement body, IReadOnlyDictionary<string, string> headers);

/// <summary>Launch Claude Code via workflow_dispatch in the target repository.</summary>
public sealed class ClaudeCodeProvider : CodingAgentProvider
{
    public const string ProviderName = "claude_code";
    public const string DefaultWorkflowFile = "foundry-claude-code.yml";
    public const string GitHubApiBase = "https://api.github.com";

    private readonly JsonHttpPost _httpPost;
    private readonly string _workflowFile;
    private readonly string _baseUrl;

    public ClaudeCodeProvider(
        JsonHttpPost httpPost,
        string workflowFile = DefaultWorkflowFile,
        string baseUrl = GitHubApiBase)
    {
        _httpPost = httpPost;
        _workflowFile = workflowFile;
        _baseUrl = baseUrl.TrimEnd('/');
    }

    public override string Name => ProviderName;

    protected override CodingAgentJob Dispatch(CodingAgentJobInput jobInput)
    {
        var url = $"{_baseUrl}/repos/{jobInput.Repo}/actions/workflows/{_workflowFile}/dispatches";
        var body = JsonSerializer.SerializeToElement(new Dictionary<string, object>
        {
            ["ref"] = jobInput.BaseBranch,
            ["inputs"] = new Dictionary<string, string>
            {
                // GitHub limits workflow_dispatch inputs to strings.
                ["run_id"] = jobInput.RunId,
                ["branch_name"] = jobInput.BranchName,
                ["ticket_url"] = jobInput.TicketUrl,
                ["instructions"] = jobInput.AgentInstructions,
                ["do_not_modify"] = string.Join("\n", jobInput.Constraints.DoNotModify),
                ["required_tests"] = string.Join("\n", jobInput.Constraints.RequiredTests),
            },
        });
        // The GitHub token comes from the injected transport's headers; it is
        // never part of the job input, so the secret guard never sees it.
        _httpPost(url, body, new Dictionary<string, string>());
        return new CodingAgentJob
        {
            JobId = $"claude-gha:{jobInput.Repo}:{jobInput.BranchName}",
            Provider = Name,
            Status = AgentJobStatus.Running,
        };
    }

    public override CodingAgentJobStatus GetJobStatus(string jobId) =>
        // Progress is observed out-of-band: the workflow opens a PR on the
        // branch Foundry chose, and the GitHub webhook drives RecordPr.
        new() { JobId = jobId, Provider = Name, Status = AgentJobStatus.Running };

    public override void CancelJob(string jobId)
    {
        // Cancelling a workflow run is a human action in the GitHub UI for now.
    }
}
