// The CodingAgentProvider abstraction.
//
// Foundry must not be built around one coding tool. Every backend (Cursor
// Cloud Agent, Claude Code, OpenAI agent, or a human picking up the plan)
// implements the same interface and accepts the same CodingAgentJobInput.
//
// A small guard (AssertNoSecrets) enforces the security rule that providers
// must never receive secrets in their prompt/instructions.

using System.Text.RegularExpressions;
using Foundry.Schemas;

namespace Foundry.Agents;

/// <summary>Raised when a job input appears to contain a secret.</summary>
public class SecretLeakException : Exception
{
    public SecretLeakException(string message) : base(message) { }
}

/// <summary>Provider-agnostic contract for launching and tracking a coding job.</summary>
public abstract class CodingAgentProvider
{
    // Heuristic patterns for obviously-secret material. This is a safety net,
    // not a substitute for never putting secrets in plans in the first place.
    private static readonly Regex[] SecretPatterns =
    {
        new(@"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        new(@"\bAKIA[0-9A-Z]{16}\b"), // AWS access key id
        new(@"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), // GitHub tokens
        new(@"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), // Slack tokens
        new(@"\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*\S{8,}", RegexOptions.IgnoreCase),
    };

    /// <summary>Stable identifier persisted on foundry_agent_jobs.provider.</summary>
    public abstract string Name { get; }

    /// <summary>Raise <see cref="SecretLeakException"/> if the job input looks like it leaks a secret.</summary>
    public static void AssertNoSecrets(CodingAgentJobInput jobInput)
    {
        var haystack = string.Join("\n", new[]
        {
            jobInput.AgentInstructions,
            FoundryJson.Serialize(jobInput.DeliveryPlan),
        });
        if (SecretPatterns.Any(pattern => pattern.IsMatch(haystack)))
        {
            throw new SecretLeakException(
                "coding-agent job input appears to contain a secret; refusing to dispatch");
        }
    }

    /// <summary>
    /// Validate, guard, then dispatch the job.
    ///
    /// Subclasses implement <see cref="Dispatch"/>; the secret guard runs first
    /// for every provider so the rule cannot be bypassed per-backend.
    /// </summary>
    public CodingAgentJob CreateJob(CodingAgentJobInput jobInput)
    {
        AssertNoSecrets(jobInput);
        return Dispatch(jobInput);
    }

    protected abstract CodingAgentJob Dispatch(CodingAgentJobInput jobInput);

    public abstract CodingAgentJobStatus GetJobStatus(string jobId);

    public abstract void CancelJob(string jobId);
}
