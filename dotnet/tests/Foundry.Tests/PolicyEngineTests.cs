// Policy gate tests (mirrors test_policy_engine.py, which itself mirrors
// foundry_test.rego). These hold the LocalPolicyEngine to the same behaviour
// the Rego bundle asserts.

using Foundry.Policy;
using Foundry.Schemas;
using Xunit;

namespace Foundry.Tests;

public class PolicyEngineTests
{
    private static LocalPolicyEngine Engine() => new();

    [Fact]
    public void LowRiskFrontendChangeAllowsDraftPr()
    {
        var decision = Engine().Evaluate(new PolicyInput
        {
            Action = PolicyAction.StartAgent,
            Ticket = new PolicyTicket { WorkType = "feature", Readiness = ImplementationReadiness.Ready },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.Low },
            Repo = new PolicyRepo { Name = "customer-web", Confidence = 90 },
        });
        Assert.True(decision.Allowed);
        Assert.Equal(AgentMode.DraftPr, decision.AllowedAgentMode);
    }

    [Fact]
    public void AuthChangeRequiresEngineeringApproval()
    {
        var payload = new PolicyInput
        {
            Action = PolicyAction.StartAgent,
            Ticket = new PolicyTicket { Readiness = ImplementationReadiness.Ready },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.Medium, Auth = true },
            Repo = new PolicyRepo { Confidence = 90 },
        };
        var decision = Engine().Evaluate(payload);
        Assert.False(decision.Allowed);
        Assert.Contains(ApprovalRole.Engineering, decision.RequiredApprovals);

        var approved = Engine().Evaluate(payload with
        {
            Approval = new Dictionary<string, bool> { ["engineering"] = true },
        });
        Assert.True(approved.Allowed);
    }

    [Fact]
    public void MigrationBlocksAutonomousExecution()
    {
        var decision = Engine().Evaluate(new PolicyInput
        {
            Action = PolicyAction.StartAgent,
            Ticket = new PolicyTicket { Readiness = ImplementationReadiness.Ready },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.Medium, DatabaseMigration = true },
            Repo = new PolicyRepo { Confidence = 95 },
        });
        Assert.False(decision.Allowed);
        Assert.Contains(decision.Reasons, r => r.Contains("migration"));
    }

    [Fact]
    public void UnknownRepoBlocksExecution()
    {
        var decision = Engine().Evaluate(new PolicyInput
        {
            Action = PolicyAction.StartAgent,
            Ticket = new PolicyTicket { Readiness = ImplementationReadiness.Ready },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.Low },
            Repo = new PolicyRepo { Confidence = 40 },
        });
        Assert.False(decision.Allowed);
        Assert.Contains(decision.Reasons, r => r.Contains("confidence"));
    }

    [Fact]
    public void NotReadyBlocksExecution()
    {
        var decision = Engine().Evaluate(new PolicyInput
        {
            Action = PolicyAction.StartAgent,
            Ticket = new PolicyTicket { Readiness = ImplementationReadiness.NeedsClarification },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.Low },
            Repo = new PolicyRepo { Confidence = 90 },
        });
        Assert.False(decision.Allowed);
    }

    [Fact]
    public void ProductionDeployBlockedInMvp()
    {
        var decision = Engine().Evaluate(new PolicyInput
        {
            Action = PolicyAction.OpenPr,
            Ticket = new PolicyTicket { Readiness = ImplementationReadiness.Ready },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.Low, ProductionDeploy = true },
            Repo = new PolicyRepo { Confidence = 90 },
        });
        Assert.False(decision.Allowed);
    }

    [Fact]
    public void HighRiskOnlyAllowsHumanOnlyMode()
    {
        var decision = Engine().Evaluate(new PolicyInput
        {
            Action = PolicyAction.StartAgent,
            Ticket = new PolicyTicket { Readiness = ImplementationReadiness.Ready },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.High },
            Repo = new PolicyRepo { Confidence = 90 },
        });
        // High risk is allowed through the gate but must not run autonomously.
        Assert.Equal(AgentMode.HumanOnly, decision.AllowedAgentMode);
    }

    [Fact]
    public void ReadOnlyAnalysisAlwaysAllowed()
    {
        var decision = Engine().Evaluate(new PolicyInput
        {
            Action = PolicyAction.AnalyseTicket,
            Ticket = new PolicyTicket { Readiness = ImplementationReadiness.NeedsClarification },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.High },
            Repo = new PolicyRepo { Confidence = 0 },
        });
        Assert.True(decision.Allowed);
    }

    [Fact]
    public void CustomerDataRequiresSecurityApproval()
    {
        var decision = Engine().Evaluate(new PolicyInput
        {
            Action = PolicyAction.StartAgent,
            Ticket = new PolicyTicket { Readiness = ImplementationReadiness.Ready },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.Medium, CustomerData = true },
            Repo = new PolicyRepo { Confidence = 90 },
        });
        Assert.False(decision.Allowed);
        Assert.Contains(ApprovalRole.Security, decision.RequiredApprovals);
    }

    [Fact]
    public void DecisionRecordsPolicyNameAndId()
    {
        var decision = Engine().Evaluate(new PolicyInput { Action = PolicyAction.AnalyseTicket });
        Assert.Equal("foundry.ticket_to_pr.v1", decision.PolicyName);
        Assert.False(string.IsNullOrEmpty(decision.DecisionId));
    }

    [Fact]
    public void AutoMergeDeniedEvenForPerfectRun()
    {
        // 'No auto-merge' is an enforced decision, not an absence of code.
        var decision = Engine().Evaluate(new PolicyInput
        {
            Action = PolicyAction.AutoMerge,
            Ticket = new PolicyTicket { Readiness = ImplementationReadiness.Ready },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.Low },
            Repo = new PolicyRepo { Name = "customer-web", Confidence = 100 },
            Approval = new Dictionary<string, bool> { ["engineering"] = true, ["security"] = true },
        });
        Assert.False(decision.Allowed);
        Assert.Equal(AgentMode.HumanOnly, decision.AllowedAgentMode);
        Assert.Contains(decision.Reasons, r => r.Contains("never run autonomously"));
    }

    [Fact]
    public void RetryWithinCapAllowedPastCapDenied()
    {
        var basePayload = new PolicyInput
        {
            Action = PolicyAction.RetryAgent,
            Ticket = new PolicyTicket { Readiness = ImplementationReadiness.Ready },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.Low },
            Repo = new PolicyRepo { Confidence = 90 },
        };
        var within = Engine().Evaluate(basePayload with
        {
            Retry = new PolicyRetry { Attempt = 2, MaxAttempts = 2 },
        });
        Assert.True(within.Allowed);

        var over = Engine().Evaluate(basePayload with
        {
            Retry = new PolicyRetry { Attempt = 3, MaxAttempts = 2 },
        });
        Assert.False(over.Allowed);
        Assert.Contains(over.Reasons, r => r.Contains("exceeds the maximum"));
    }

    [Fact]
    public void RetryOverBudgetDeniedUnderBudgetAllowed()
    {
        var basePayload = new PolicyInput
        {
            Action = PolicyAction.RetryAgent,
            Ticket = new PolicyTicket { Readiness = ImplementationReadiness.Ready },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.Low },
            Repo = new PolicyRepo { Confidence = 90 },
        };
        var over = Engine().Evaluate(basePayload with
        {
            Budget = new PolicyBudget { CostUsd = 5.5, MaxCostUsd = 5.0 },
        });
        Assert.False(over.Allowed);
        Assert.Contains(over.Reasons, r => r.Contains("budget cap"));

        var under = Engine().Evaluate(basePayload with
        {
            Budget = new PolicyBudget { CostUsd = 1.0, MaxCostUsd = 5.0 },
        });
        Assert.True(under.Allowed);

        // No cap configured -> spend is informational only.
        var uncapped = Engine().Evaluate(basePayload with
        {
            Budget = new PolicyBudget { CostUsd = 999.0 },
        });
        Assert.True(uncapped.Allowed);
    }

    [Fact]
    public void ProductionDeployActionDeniedUnconditionally()
    {
        var decision = Engine().Evaluate(new PolicyInput
        {
            Action = PolicyAction.ProductionDeploy,
            Ticket = new PolicyTicket { Readiness = ImplementationReadiness.Ready },
            Risk = new PolicyRisk { OverallRisk = OverallRisk.Low },
            Repo = new PolicyRepo { Confidence = 100 },
        });
        Assert.False(decision.Allowed);
    }
}
