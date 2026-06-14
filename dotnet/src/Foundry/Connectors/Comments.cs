// Rendering Foundry's view back into the tracker.
//
// Two things the orchestrator writes to Linear:
//
// - a concise analysis/plan comment when a run is planned, and
// - a "Foundry: ..." workflow state that mirrors the run status.
//
// Comments are kept short and skimmable - the ticket is the system of record
// for delivery status, not a wall of text.

using Foundry.Schemas;

namespace Foundry.Connectors;

public static class Comments
{
    // Suggested Linear workflow states, mapped from the run status.
    private static readonly IReadOnlyDictionary<RunStatus, string> StateNames =
        new Dictionary<RunStatus, string>
        {
            [RunStatus.Analysing] = "Foundry: Analysing",
            [RunStatus.NeedsClarification] = "Foundry: Needs Clarification",
            [RunStatus.PlanReady] = "Foundry: Plan Ready",
            [RunStatus.WaitingApproval] = "Foundry: Waiting Approval",
            [RunStatus.Approved] = "Foundry: Approved",
            [RunStatus.AgentRunning] = "Foundry: Agent Running",
            [RunStatus.PrOpen] = "Foundry: PR Open",
            [RunStatus.ReviewRequired] = "Foundry: Review Required",
            [RunStatus.Complete] = "Foundry: Complete",
            [RunStatus.Blocked] = "Foundry: Blocked",
            [RunStatus.ExecutionFailed] = "Foundry: Blocked",
            [RunStatus.Rejected] = "Foundry: Blocked",
        };

    public static string StateFor(RunStatus status) =>
        StateNames.TryGetValue(status, out var name)
            ? name
            : $"Foundry: {Wire.ToTitle(status.ToWire())}";

    private static string Bullets(IReadOnlyList<string> items, string empty = "_none_") =>
        items.Count > 0 ? string.Join("\n", items.Select(i => $"- {i}")) : empty;

    /// <summary>
    /// Draft acceptance-criteria skeletons the ticket author can edit.
    ///
    /// Clarification should do work for the author, not just bounce the
    /// ticket: a copy-editable draft converts "needs clarification" from a
    /// rejection into a 30-second fix.
    /// </summary>
    public static List<string> DraftAcceptanceCriteria(TicketAnalysis analysis)
    {
        var drafts = new List<string>
        {
            $"Given <starting state>, when {analysis.Title.Trim().TrimEnd('.').ToLowerInvariant()}, "
            + "then <observable outcome>",
        };
        if (analysis.MissingInformation.Contains("reproduction steps"))
        {
            drafts.Add("Steps to reproduce: 1. <go to...> 2. <do...> 3. <observe...>");
        }
        if (analysis.MissingInformation.Contains("a description of the desired outcome"))
        {
            drafts.Add("The desired outcome is <what the user should see/be able to do>");
        }
        drafts.Add("Out of scope: <anything this ticket deliberately does not change>");
        return drafts;
    }

    /// <summary>Render the planning summary comment posted to the issue.</summary>
    public static string FormatAnalysisComment(
        TicketAnalysis analysis, RiskAssessment risk, DeliveryPlan plan, RunStatus status)
    {
        var repo = plan.AffectedRepositories.Count > 0 ? plan.AffectedRepositories[0] : "_unknown_";
        var approvals = risk.RequiredApprovals.Select(r => r.ToWire()).ToList();
        var lines = new List<string>
        {
            "**Foundry analysis complete.**",
            "",
            $"- Work type: `{analysis.WorkType.ToWire()}`",
            $"- Readiness: `{analysis.ImplementationReadiness.ToWire()}`",
            $"- Risk: `{risk.OverallRisk.ToWire()}` (suggested mode: `{risk.AllowedAgentMode.ToWire()}`)",
            $"- Affected repo: `{repo}`",
            "",
            "**Acceptance criteria**",
            Bullets(analysis.AcceptanceCriteria),
        };
        if (analysis.MissingInformation.Count > 0)
        {
            lines.AddRange(new[] { "", "**Missing information**", Bullets(analysis.MissingInformation) });
        }
        if (plan.ImplementationSteps.Count > 0)
        {
            lines.AddRange(new[]
            {
                "",
                "**Plan**",
                string.Join("\n", plan.ImplementationSteps.Select(s => $"{s.Step}. {s.Description}")),
            });
        }
        if (approvals.Count > 0)
        {
            lines.AddRange(new[] { "", $"**Required approval:** {string.Join(", ", approvals)}" });
        }

        if (status == RunStatus.WaitingApproval)
        {
            lines.AddRange(new[]
            {
                "",
                "Reply to proceed:",
                "`/foundry approve` · `/foundry reject` · `/foundry stop`",
            });
        }
        else if (status == RunStatus.NeedsClarification)
        {
            lines.AddRange(new[]
            {
                "",
                "_Needs clarification before an agent can start._",
                "",
                "**Suggested acceptance criteria** (edit these and add them to the "
                + "ticket, then re-trigger Foundry):",
                Bullets(DraftAcceptanceCriteria(analysis)),
            });
        }
        else if (status == RunStatus.Blocked)
        {
            var blockedReasons = risk.RiskReasons.Count > 0
                ? risk.RiskReasons
                : new[] { "see policy" };
            lines.AddRange(new[] { "", "_Blocked: " + string.Join("; ", blockedReasons) + "._" });
        }
        return string.Join("\n", lines);
    }

    /// <summary>
    /// The @Cursor delegation comment that hands approved work to Cursor.
    ///
    /// Foundry has already gathered context, classified risk and obtained
    /// approval; this passes the *governed* instructions to Cursor's Linear
    /// integration, which runs the cloud agent, reports status in Linear and
    /// opens the PR.
    /// </summary>
    public static string FormatCursorDelegation(string agentInstructions) =>
        "@Cursor please implement this. Work strictly within the scope below; "
        + "Foundry has approved it.\n\n"
        + agentInstructions;
}
