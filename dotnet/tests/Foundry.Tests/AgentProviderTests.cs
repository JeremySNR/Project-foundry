// Coding-agent provider contract and security tests (mirrors test_agent_provider.py).

using Foundry.Agents;
using Foundry.Schemas;
using Xunit;

namespace Foundry.Tests;

public class AgentProviderTests
{
    private static CodingAgentJobInput JobInput(
        string agentInstructions = "Implement favourites per the plan.",
        DeliveryPlan? deliveryPlan = null) => new()
    {
        RunId = "run-1",
        Repo = "customer-web",
        BranchName = "foundry/lin-123-customer-favourites",
        TicketUrl = "https://linear.app/x/issue/LIN-123",
        DeliveryPlan = deliveryPlan ?? new DeliveryPlan { Goal = "Add favourites" },
        AgentInstructions = agentInstructions,
        Constraints = new JobConstraints { DoNotModify = new[] { "infra/**", "migrations/**" } },
    };

    [Theory]
    [InlineData("manual")]
    [InlineData("fake")]
    public void EveryProviderAcceptsSameInput(string providerName)
    {
        var provider = ProviderRegistry.GetProvider(providerName);
        var job = provider.CreateJob(JobInput());
        Assert.False(string.IsNullOrEmpty(job.JobId));
        Assert.Equal(providerName, job.Provider);
    }

    [Fact]
    public void RegistryListsKnownProviders()
    {
        var available = ProviderRegistry.AvailableProviders();
        Assert.Contains("manual", available);
        Assert.Contains("fake", available);
    }

    [Fact]
    public void ManualProviderCreatesJobWithoutWriting()
    {
        var provider = new ManualProvider();
        var job = provider.CreateJob(JobInput());
        var status = provider.GetJobStatus(job.JobId);
        Assert.Equal(AgentJobStatus.Created, status.Status);
        Assert.Null(status.PrUrl); // manual provider never opens a PR itself
    }

    [Fact]
    public void FakeProviderSimulatesBranchAndPr()
    {
        var provider = new InMemoryFakeProvider();
        var job = provider.CreateJob(JobInput());
        var final = provider.Run(job.JobId);
        Assert.Equal(AgentJobStatus.Succeeded, final.Status);
        Assert.Equal("foundry/lin-123-customer-favourites", final.Branch);
        Assert.NotNull(final.PrUrl);
        Assert.EndsWith("/pull/1", final.PrUrl);
    }

    [Fact]
    public void FakeProviderFailureMarksFailed()
    {
        var provider = new InMemoryFakeProvider(fail: true);
        var job = provider.CreateJob(JobInput());
        var final = provider.Run(job.JobId);
        Assert.Equal(AgentJobStatus.Failed, final.Status);
        Assert.False(string.IsNullOrEmpty(final.Error));
    }

    [Fact]
    public void SecretInInstructionsIsRejected()
    {
        var provider = new ManualProvider();
        var leaky = JobInput(agentInstructions: "Use api_key=SuperSecretValue1234 to authenticate.");
        Assert.Throws<SecretLeakException>(() => provider.CreateJob(leaky));
    }

    [Fact]
    public void PrivateKeyInPlanIsRejected()
    {
        var provider = new InMemoryFakeProvider();
        var leaky = JobInput(deliveryPlan: new DeliveryPlan
        {
            Goal = "Add favourites",
            OpenQuestions = new[]
            {
                "-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----",
            },
        });
        Assert.Throws<SecretLeakException>(() => provider.CreateJob(leaky));
    }

    [Fact]
    public void ForbiddenGlobsCarriedInConstraints()
    {
        var jobInput = JobInput();
        Assert.Contains("infra/**", jobInput.Constraints.DoNotModify);
        Assert.False(jobInput.Constraints.AllowNewDependencies);
    }

    [Fact]
    public void ConstraintsDefaultMaxFiles()
    {
        Assert.Equal(12, new JobConstraints().MaxFilesChanged);
    }

    [Fact]
    public void UnknownProviderRaises()
    {
        Assert.Throws<ArgumentException>(() => ProviderRegistry.GetProvider("does-not-exist"));
    }

    [Fact]
    public void WebhookProviderSignsPayload()
    {
        byte[]? seenBody = null;
        IReadOnlyDictionary<string, string>? seenHeaders = null;
        var provider = new WebhookProvider(
            "https://agent.example.com/jobs",
            (url, body, headers) =>
            {
                seenBody = body;
                seenHeaders = headers;
                return null;
            },
            signingSecret: "topsecret");
        var job = provider.CreateJob(JobInput());

        Assert.Equal("webhook:run-1", job.JobId);
        Assert.NotNull(seenBody);
        Assert.NotNull(seenHeaders);
        Assert.Equal(
            WebhookProvider.SignPayload("topsecret", seenBody!),
            seenHeaders!["X-Foundry-Signature"]);
        Assert.StartsWith("sha256=", seenHeaders!["X-Foundry-Signature"]);
    }
}
