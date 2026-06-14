// Audit helpers - content hashing and persistence of the run's trail.
//
// Every decision, prompt input, output, approval and tool call should be
// storable and verifiable. These helpers keep hashing consistent across
// artifact, audit and policy rows.

using System.Security.Cryptography;
using System.Text;
using Foundry.Db;
using Foundry.Policy;
using Foundry.Schemas;

namespace Foundry.Audit;

public static class Events
{
    /// <summary>SHA-256 of the canonical JSON for <paramref name="content"/>.</summary>
    public static string ContentHash(object? content)
    {
        var canonical = FoundryJson.Canonical(content);
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(canonical)))
            .ToLowerInvariant();
    }

    public static string NewId(string prefix) => $"{prefix}-{Guid.NewGuid()}";

    /// <summary>Create a content-hashed artifact row (not yet persisted).</summary>
    public static FoundryArtifact BuildArtifact(
        string runId,
        ArtifactType artifactType,
        object content,
        int version = 1,
        string? createdBy = null)
    {
        var canonical = FoundryJson.Canonical(content);
        return new FoundryArtifact
        {
            Id = NewId("art"),
            RunId = runId,
            ArtifactType = artifactType,
            Version = version,
            ContentJson = canonical,
            ContentHash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(canonical)))
                .ToLowerInvariant(),
            CreatedBy = createdBy,
        };
    }

    /// <summary>Create an audit event row with hashed input/output (not yet persisted).</summary>
    public static FoundryAuditEvent BuildAuditEvent(
        string runId,
        AuditEventType eventType,
        string actorType,
        string? actorId = null,
        object? inputContent = null,
        object? outputContent = null,
        IReadOnlyDictionary<string, object?>? metadata = null)
    {
        return new FoundryAuditEvent
        {
            Id = NewId("evt"),
            RunId = runId,
            EventType = eventType,
            ActorType = actorType,
            ActorId = actorId,
            InputHash = inputContent is null ? null : ContentHash(inputContent),
            OutputHash = outputContent is null ? null : ContentHash(outputContent),
            MetadataJson = metadata is null ? null : FoundryJson.Canonical(metadata),
        };
    }

    /// <summary>Persist-ready row capturing a single policy gate decision.</summary>
    public static FoundryPolicyDecision BuildPolicyDecisionRow(
        string runId,
        PolicyInput payload,
        PolicyDecision decision)
    {
        return new FoundryPolicyDecision
        {
            Id = decision.DecisionId,
            RunId = runId,
            PolicyName = decision.PolicyName,
            InputJson = FoundryJson.Canonical(payload),
            DecisionJson = FoundryJson.Canonical(decision),
            Allowed = decision.Allowed,
            Reason = decision.Reasons.Count > 0 ? string.Join("; ", decision.Reasons) : null,
        };
    }
}
