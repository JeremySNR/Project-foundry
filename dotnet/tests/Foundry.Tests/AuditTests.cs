// Audit hashing and row-building tests (mirrors test_audit.py).

using Foundry.Audit;
using Foundry.Db;
using Foundry.Policy;
using Foundry.Schemas;
using Xunit;

namespace Foundry.Tests;

public class AuditTests
{
    [Fact]
    public void ContentHashIsStableAndOrderIndependent()
    {
        var a = new Dictionary<string, object?> { ["b"] = 1, ["a"] = 2 };
        var b = new Dictionary<string, object?> { ["a"] = 2, ["b"] = 1 };
        Assert.Equal(Events.ContentHash(a), Events.ContentHash(b));
    }

    [Fact]
    public void ContentHashChangesWithContent()
    {
        Assert.NotEqual(
            Events.ContentHash(new Dictionary<string, object?> { ["x"] = 1 }),
            Events.ContentHash(new Dictionary<string, object?> { ["x"] = 2 }));
    }

    [Fact]
    public void ArtifactModelHashesConsistently()
    {
        var first = Events.ContentHash(TestData.ReadyAnalysis());
        var second = Events.ContentHash(TestData.ReadyAnalysis());
        Assert.Equal(first, second);
    }

    [Fact]
    public void BuildArtifactSetsHash()
    {
        var analysis = TestData.ReadyAnalysis();
        var artifact = Events.BuildArtifact("run-1", ArtifactType.TicketAnalysis, analysis);
        Assert.Equal("run-1", artifact.RunId);
        Assert.Equal(ArtifactType.TicketAnalysis, artifact.ArtifactType);
        Assert.Equal(Events.ContentHash(analysis), artifact.ContentHash);
    }

    [Fact]
    public void BuildPolicyDecisionRowRecordsOutcome()
    {
        var payload = new PolicyInput
        {
            Action = PolicyAction.StartAgent,
            Ticket = new PolicyTicket { Readiness = ImplementationReadiness.Ready },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.Low },
            Repo = new PolicyRepo { Confidence = 90 },
        };
        var decision = new LocalPolicyEngine().Evaluate(payload);
        var row = Events.BuildPolicyDecisionRow("run-1", payload, decision);
        Assert.Equal(decision.DecisionId, row.Id);
        Assert.Equal(decision.Allowed, row.Allowed);
        Assert.Equal(decision.PolicyName, row.PolicyName);
    }

    [Fact]
    public void AuditEventsGetMonotonicPerRunSequences()
    {
        // The model promises a guaranteed per-run order; the context assigns it.
        using var store = FoundryDataStore.InMemory();

        static FoundryAuditEvent Event(string runId) =>
            Events.BuildAuditEvent(runId, AuditEventType.RunStarted, "foundry");

        using (var session = store.CreateContext())
        {
            foreach (var runId in new[] { "run-a", "run-b" })
            {
                session.Runs.Add(new FoundryRun
                {
                    Id = runId,
                    LinearIssueId = runId,
                    LinearIssueKey = runId,
                    TriggerType = "test",
                });
            }
            // Two events in one save, then one more in a later save.
            session.AuditEvents.Add(Event("run-a"));
            session.AuditEvents.Add(Event("run-a"));
            session.AuditEvents.Add(Event("run-b"));
            session.SaveChanges();
            session.AuditEvents.Add(Event("run-a"));
            session.SaveChanges();
        }

        using (var session = store.CreateContext())
        {
            var sequencesA = session.AuditEvents
                .Where(e => e.RunId == "run-a")
                .OrderBy(e => e.Sequence)
                .Select(e => e.Sequence)
                .ToList();
            var sequencesB = session.AuditEvents
                .Where(e => e.RunId == "run-b")
                .Select(e => e.Sequence)
                .ToList();
            Assert.Equal(new[] { 0, 1, 2 }, sequencesA); // monotonic per run, across separate saves
            Assert.Equal(new[] { 0 }, sequencesB); // independent counter per run
        }
    }
}
