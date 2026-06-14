// Generic webhook coding-agent provider.
//
// The escape hatch that keeps Foundry honest about vendor neutrality: instead
// of Foundry shipping an adapter for every agent (Codex CLI, Aider, an
// internal tool), you point this provider at *your* endpoint. Foundry POSTs
// the governed job input as JSON, HMAC-signed so your receiver can verify it
// really came from Foundry, and then watches for the PR through the normal
// GitHub webhook.
//
// Receiver contract:
//
// - POST <url> with the CodingAgentJobInput JSON body.
// - X-Foundry-Signature: sha256=<hex hmac of the raw body> when a secret is
//   configured. Verify it.
// - Respond 2xx. An optional JSON body {"job_id": "..."} names the job;
//   otherwise Foundry synthesises one from the run id.
// - Do the work on branch_name and open a PR; Foundry takes it from there.

using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Foundry.Schemas;

namespace Foundry.Agents;

/// <summary>httpPost(url, rawBodyBytes, headers) -> parsed JSON (or null).</summary>
public delegate JsonElement? RawHttpPost(string url, byte[] body, IReadOnlyDictionary<string, string> headers);

/// <summary>POST the governed job input to a configured endpoint.</summary>
public sealed class WebhookProvider : CodingAgentProvider
{
    public const string ProviderName = "webhook";

    private readonly string _url;
    private readonly RawHttpPost _httpPost;
    private readonly string? _secret;

    public WebhookProvider(string url, RawHttpPost httpPost, string? signingSecret = null)
    {
        _url = url;
        _httpPost = httpPost;
        _secret = signingSecret;
    }

    public override string Name => ProviderName;

    /// <summary>The signature value sent in X-Foundry-Signature.</summary>
    public static string SignPayload(string secret, byte[] body)
    {
        using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(secret));
        return "sha256=" + Convert.ToHexString(hmac.ComputeHash(body)).ToLowerInvariant();
    }

    protected override CodingAgentJob Dispatch(CodingAgentJobInput jobInput)
    {
        var body = Encoding.UTF8.GetBytes(FoundryJson.Canonical(jobInput));
        var headers = new Dictionary<string, string> { ["Content-Type"] = "application/json" };
        if (!string.IsNullOrEmpty(_secret))
        {
            headers["X-Foundry-Signature"] = SignPayload(_secret, body);
        }
        var response = _httpPost(_url, body, headers);
        string? reportedId = null;
        if (response is JsonElement element
            && element.ValueKind == JsonValueKind.Object
            && element.TryGetProperty("job_id", out var jobIdProperty))
        {
            reportedId = jobIdProperty.ToString();
        }
        return new CodingAgentJob
        {
            JobId = string.IsNullOrEmpty(reportedId) ? $"webhook:{jobInput.RunId}" : reportedId,
            Provider = Name,
            Status = AgentJobStatus.Running,
        };
    }

    public override CodingAgentJobStatus GetJobStatus(string jobId) =>
        // Observed out-of-band via the GitHub webhook, like the other
        // delegation-style providers.
        new() { JobId = jobId, Provider = Name, Status = AgentJobStatus.Running };

    public override void CancelJob(string jobId)
    {
        // Cancellation is the receiver's job.
    }
}
