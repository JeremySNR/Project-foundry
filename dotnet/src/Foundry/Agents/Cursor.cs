// Cursor coding-agent providers.
//
// Two ways to hand approved work to Cursor, both extending CodingAgentProvider:
//
// - CursorViaLinearProvider - the *preferred* path. Foundry has already
//   gathered context, classified risk and obtained approval; it then delegates
//   to Cursor through Linear by posting an @Cursor comment with the governed
//   instructions. Cursor's Linear integration runs the cloud agent, reports
//   status back in Linear and auto-opens the PR. Foundry stays the control
//   plane above.
//
// - CursorCloudAgentProvider - the direct Cloud Agents API
//   (POST https://api.cursor.com/v0/agents). Useful when there is no Linear
//   hand-off. The HTTP calls are injected so this is testable without network
//   and without a real Cursor key.
//
// Both go through CreateJob on the base class, so the secret-leak guard runs
// before anything is dispatched.

using System.Text.Json;
using Foundry.Connectors;
using Foundry.Schemas;

namespace Foundry.Agents;

/// <summary>httpGet(url, headers) -> response json.</summary>
public delegate JsonElement JsonHttpGet(string url, IReadOnlyDictionary<string, string> headers);

/// <summary>Delegate to Cursor by commenting @Cursor on the Linear issue.</summary>
public sealed class CursorViaLinearProvider : CodingAgentProvider
{
    public const string ProviderName = "cursor_via_linear";

    private readonly IIssueTracker _tracker;

    public CursorViaLinearProvider(IIssueTracker tracker)
    {
        _tracker = tracker;
    }

    public override string Name => ProviderName;

    protected override CodingAgentJob Dispatch(CodingAgentJobInput jobInput)
    {
        if (string.IsNullOrEmpty(jobInput.TrackerIssueId))
        {
            throw new ArgumentException(
                "CursorViaLinearProvider requires jobInput.TrackerIssueId (the Linear issue to delegate on)");
        }
        var body = Comments.FormatCursorDelegation(jobInput.AgentInstructions);
        _tracker.PostComment(jobInput.TrackerIssueId, body);
        // The agent now runs inside Cursor's Linear integration; progress and
        // the PR arrive via Linear/GitHub webhooks, which drive the
        // orchestrator's RecordPr.
        return new CodingAgentJob
        {
            JobId = $"cursor-linear:{jobInput.TrackerIssueId}",
            Provider = Name,
            Status = AgentJobStatus.Running,
        };
    }

    public override CodingAgentJobStatus GetJobStatus(string jobId) =>
        // Status is observed out-of-band (Linear/GitHub), not polled here.
        new() { JobId = jobId, Provider = Name, Status = AgentJobStatus.Running };

    public override void CancelJob(string jobId)
    {
        // Cancellation is a human action in Linear/Cursor for this path.
    }
}

/// <summary>Launch a Cursor cloud agent directly via the Cloud Agents API.</summary>
public sealed class CursorCloudAgentProvider : CodingAgentProvider
{
    public const string ProviderName = "cursor_cloud";
    private const string AgentsUrl = "https://api.cursor.com/v0/agents";

    // Cursor cloud-agent status -> Foundry job status.
    private static readonly IReadOnlyDictionary<string, AgentJobStatus> CursorStatus =
        new Dictionary<string, AgentJobStatus>
        {
            ["CREATING"] = AgentJobStatus.Created,
            ["PENDING"] = AgentJobStatus.Created,
            ["RUNNING"] = AgentJobStatus.Running,
            ["FINISHED"] = AgentJobStatus.Succeeded,
            ["COMPLETED"] = AgentJobStatus.Succeeded,
            ["ERROR"] = AgentJobStatus.Failed,
            ["FAILED"] = AgentJobStatus.Failed,
            ["CANCELLED"] = AgentJobStatus.Cancelled,
            ["EXPIRED"] = AgentJobStatus.Cancelled,
        };

    private readonly JsonHttpPost _httpPost;
    private readonly JsonHttpGet? _httpGet;
    private readonly bool _autoCreatePr;

    public CursorCloudAgentProvider(
        JsonHttpPost httpPost, JsonHttpGet? httpGet = null, bool autoCreatePr = true)
    {
        _httpPost = httpPost;
        _httpGet = httpGet;
        _autoCreatePr = autoCreatePr;
    }

    public override string Name => ProviderName;

    protected override CodingAgentJob Dispatch(CodingAgentJobInput jobInput)
    {
        var body = JsonSerializer.SerializeToElement(new Dictionary<string, object>
        {
            ["prompt"] = new Dictionary<string, string> { ["text"] = jobInput.AgentInstructions },
            ["source"] = new Dictionary<string, string>
            {
                ["repository"] = RepoUrl(jobInput.Repo),
                ["ref"] = jobInput.BaseBranch,
            },
            ["target"] = new Dictionary<string, object>
            {
                ["autoCreatePr"] = _autoCreatePr,
                ["branchName"] = jobInput.BranchName,
            },
        });
        // The API key is supplied by the injected transport's headers, never
        // in the job input - so the secret guard never sees it.
        var response = _httpPost(AgentsUrl, body, new Dictionary<string, string>())
            ?? throw new InvalidOperationException("Cursor API returned no response body");
        var statusText = response.TryGetProperty("status", out var statusProperty)
            ? statusProperty.GetString() ?? ""
            : "";
        return new CodingAgentJob
        {
            JobId = response.GetProperty("id").ToString(),
            Provider = Name,
            Status = CursorStatus.GetValueOrDefault(statusText, AgentJobStatus.Created),
        };
    }

    public override CodingAgentJobStatus GetJobStatus(string jobId)
    {
        if (_httpGet is null)
        {
            throw new InvalidOperationException("CursorCloudAgentProvider needs httpGet to poll status");
        }
        var data = _httpGet($"{AgentsUrl}/{jobId}", new Dictionary<string, string>());
        JsonElement target = default;
        var hasTarget = data.TryGetProperty("target", out target)
            && target.ValueKind == JsonValueKind.Object;
        var statusText = data.TryGetProperty("status", out var statusProperty)
            ? statusProperty.GetString() ?? ""
            : "";
        return new CodingAgentJobStatus
        {
            JobId = jobId,
            Provider = Name,
            Status = CursorStatus.GetValueOrDefault(statusText, AgentJobStatus.Running),
            Branch = hasTarget ? GetStringOrNull(target, "branchName") : null,
            PrUrl = hasTarget
                ? GetStringOrNull(target, "prUrl") ?? GetStringOrNull(target, "url")
                : null,
            CostUsd = ExtractCost(data),
        };
    }

    public override void CancelJob(string jobId) =>
        _httpPost($"{AgentsUrl}/{jobId}/cancel",
            JsonSerializer.SerializeToElement(new Dictionary<string, object>()),
            new Dictionary<string, string>());

    private static string RepoUrl(string repo) =>
        repo.StartsWith("http://") || repo.StartsWith("https://")
            ? repo
            : $"https://github.com/{repo}";

    private static string? GetStringOrNull(JsonElement element, string property) =>
        element.TryGetProperty(property, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;

    /// <summary>Provider-reported spend, tolerant of the usage shape evolving.</summary>
    internal static double? ExtractCost(JsonElement data)
    {
        var hasUsage = data.TryGetProperty("usage", out var usage)
            && usage.ValueKind == JsonValueKind.Object;
        foreach (var key in new[] { "totalCostUsd", "costUsd", "totalCost", "cost_usd" })
        {
            JsonElement value = default;
            var found = (hasUsage && usage.TryGetProperty(key, out value))
                || data.TryGetProperty(key, out value);
            if (found && value.ValueKind is not (JsonValueKind.Null or JsonValueKind.Undefined))
            {
                if (value.ValueKind == JsonValueKind.Number)
                {
                    return value.GetDouble();
                }
                if (value.ValueKind == JsonValueKind.String
                    && double.TryParse(value.GetString(), out var parsed))
                {
                    return parsed;
                }
                return null;
            }
        }
        return null;
    }
}
