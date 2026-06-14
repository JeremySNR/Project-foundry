// Tests for environment- and YAML-driven settings (mirrors test_config.py).

using Foundry.Configuration;
using Xunit;

namespace Foundry.Tests;

public class ConfigTests : IDisposable
{
    private readonly string _tempDir = Directory.CreateTempSubdirectory("foundry-config-tests").FullName;

    public void Dispose() => Directory.Delete(_tempDir, recursive: true);

    private string WriteYaml(string content, string name = "foundry.yaml")
    {
        var path = Path.Combine(_tempDir, name);
        File.WriteAllText(path, content);
        return path;
    }

    private static Dictionary<string, string> Env(params (string Key, string Value)[] entries) =>
        entries.ToDictionary(e => e.Key, e => e.Value);

    private const string Yaml = """
database:
  url: "postgresql+psycopg://u@h/db"
analyzer:
  provider: openai
  model: gpt-4o-2026-04-23
policy:
  repo_confidence_threshold: 85
  max_files_changed: 5
  forbidden_globs:
    - "infra/**"
    - "secrets/**"
  sensitive_path_globs:
    auth: ["**/iam/**"]
    payments: ["**/billing/**"]
triggers:
  label: "ai:go"
  status: "Ready for Foundry"
approval:
  approvers:
    - email: "alice@example.com"
      roles: ["engineering", "security"]
    - email: "bob@example.com"
      roles: []
temporal:
  address: "temporal.internal:7233"
  task_queue: "tq"
""";

    [Fact]
    public void DefaultsWhenEnvEmpty()
    {
        var settings = Settings.FromEnv(Env());
        Assert.StartsWith("sqlite", settings.DatabaseUrl);
        Assert.Equal("gpt-5.5", settings.OpenaiModel);
        Assert.False(settings.UseOpenaiAnalyzer);
        Assert.Null(settings.GithubWebhookSecret);
        Assert.Null(settings.ApiToken);
        Assert.Equal("foundry-ticket-to-pr", settings.TaskQueue);
        Assert.Empty(settings.Approvers);
        // Built-in diff-risk globs exist out of the box.
        Assert.Contains("auth", settings.SensitiveGlobsMap.Keys);
    }

    [Fact]
    public void ReadsEnv()
    {
        var settings = Settings.FromEnv(Env(
            ("FOUNDRY_DATABASE_URL", "postgresql+psycopg://u@h/db"),
            ("FOUNDRY_LINEAR_WEBHOOK_SECRET", "lw"),
            ("FOUNDRY_GITHUB_WEBHOOK_SECRET", "gw"),
            ("FOUNDRY_LINEAR_API_TOKEN", "lt"),
            ("FOUNDRY_GITHUB_API_TOKEN", "gt"),
            ("FOUNDRY_USE_OPENAI_ANALYZER", "true"),
            ("FOUNDRY_OPENAI_MODEL", "gpt-4o-2026-04-23"),
            ("TEMPORAL_ADDRESS", "temporal:7233")));
        Assert.StartsWith("postgresql", settings.DatabaseUrl);
        Assert.Equal("lw", settings.LinearWebhookSecret);
        Assert.Equal("gw", settings.GithubWebhookSecret);
        Assert.Equal("lt", settings.LinearApiToken);
        Assert.Equal("gt", settings.GithubApiToken);
        Assert.True(settings.UseOpenaiAnalyzer);
        Assert.Equal("gpt-4o-2026-04-23", settings.OpenaiModel);
        Assert.Equal("temporal:7233", settings.TemporalAddress);
    }

    [Theory]
    [InlineData("1", true)]
    [InlineData("YES", true)]
    [InlineData("0", false)]
    [InlineData("no", false)]
    public void BoolParsingVariants(string value, bool expected)
    {
        var settings = Settings.FromEnv(Env(("FOUNDRY_USE_OPENAI_ANALYZER", value)));
        Assert.Equal(expected, settings.UseOpenaiAnalyzer);
    }

    [Fact]
    public void LoadFromYaml()
    {
        var path = WriteYaml(Yaml);
        var settings = Settings.Load(path, Env());
        Assert.StartsWith("postgresql", settings.DatabaseUrl);
        Assert.True(settings.UseOpenaiAnalyzer);
        Assert.Equal("gpt-4o-2026-04-23", settings.OpenaiModel);
        Assert.Equal(85, settings.RepoConfidenceThreshold);
        Assert.Equal(5, settings.MaxFilesChanged);
        Assert.Equal(new[] { "infra/**", "secrets/**" }, settings.ForbiddenGlobs);
        Assert.Equal("ai:go", settings.TriggerLabel);
        Assert.Equal("Ready for Foundry", settings.TriggerStatus);
        Assert.Equal(
            new HashSet<string> { "alice@example.com", "bob@example.com" },
            settings.ApproverEmails);
        Assert.Equal(
            new HashSet<string> { "engineering", "security" },
            settings.RolesFor("alice@example.com"));
        Assert.Empty(settings.RolesFor("bob@example.com"));
        Assert.Empty(settings.RolesFor("nobody@example.com"));
        Assert.Equal(new[] { "**/iam/**" }, settings.SensitiveGlobsMap["auth"]);
        Assert.Equal(new[] { "**/billing/**" }, settings.SensitiveGlobsMap["payments"]);
        Assert.Equal(2, settings.SensitiveGlobsMap.Count);
        Assert.Equal("temporal.internal:7233", settings.TemporalAddress);
        Assert.Equal("tq", settings.TaskQueue);
    }

    [Fact]
    public void LegacyAuthorisedApproversYamlStillLoads()
    {
        var path = WriteYaml("approval:\n  authorised_approvers:\n    - 'lead@example.com'\n");
        var settings = Settings.Load(path, Env());
        Assert.Equal(new HashSet<string> { "lead@example.com" }, settings.ApproverEmails);
        Assert.Empty(settings.RolesFor("lead@example.com"));
    }

    [Fact]
    public void ApiTokenFromEnv()
    {
        var settings = Settings.FromEnv(Env(("FOUNDRY_API_TOKEN", "tok")));
        Assert.Equal("tok", settings.ApiToken);
    }

    [Fact]
    public void EnvOverridesYaml()
    {
        var path = WriteYaml(Yaml);
        // Env wins over YAML for the keys it covers.
        var settings = Settings.Load(path, Env(
            ("FOUNDRY_DATABASE_URL", "sqlite:///:memory:"),
            ("FOUNDRY_OPENAI_MODEL", "gpt-4o")));
        Assert.StartsWith("sqlite", settings.DatabaseUrl);
        Assert.Equal("gpt-4o", settings.OpenaiModel);
        // YAML-only knobs are untouched by env.
        Assert.Equal(85, settings.RepoConfidenceThreshold);
        Assert.Equal("ai:go", settings.TriggerLabel);
    }

    [Fact]
    public void MissingYamlPathIsDefaults()
    {
        var settings = Settings.Load("/no/such/file.yaml", Env());
        Assert.Equal(70, settings.RepoConfidenceThreshold);
        Assert.Equal("foundry:candidate", settings.TriggerLabel);
    }

    [Fact]
    public void RemediationAndBudgetYaml()
    {
        var path = WriteYaml("""
remediation:
  max_agent_retries: 1
  retry_on: [ci_failed]
budget:
  max_cost_per_run: 10.5
""");
        var settings = Settings.Load(path, Env());
        Assert.Equal(1, settings.MaxAgentRetries);
        Assert.Equal(new[] { "ci_failed" }, settings.RetryOn);
        Assert.Equal(10.5, settings.MaxCostPerRun);
    }

    [Theory]
    [InlineData("budget:\n  max_cost_per_run: 0\n")]
    [InlineData("remediation:\n  retry_on: [nonsense]\n")]
    [InlineData("remediation:\n  max_agent_retries: -1\n")]
    public void InvalidRemediationAndBudgetRejected(string content)
    {
        var path = WriteYaml(content, $"bad-{Guid.NewGuid()}.yaml");
        Assert.Throws<ArgumentException>(() => Settings.Load(path, Env()));
    }
}
