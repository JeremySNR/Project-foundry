// Configuration: a YAML file for behaviour, environment variables for secrets.
//
// Foundry is meant to be highly customisable without touching code. The knobs
// that shape *how it behaves* (which analyzer, the policy thresholds, the
// trigger label, who can approve) live in a YAML file. The things that are
// *secret* (webhook signing secrets, API tokens, the database URL) come from
// the environment and are never written to YAML.
//
// Layering, lowest priority first:
//
//     built-in defaults  <  foundry.yaml  <  environment variables
//
// so you can commit a sane YAML and let each deployment override the sensitive
// bits (and a few operational ones) from its environment.

using System.Globalization;
using YamlDotNet.Serialization;

namespace Foundry.Configuration;

public sealed record Settings
{
    private static readonly HashSet<string> TrueValues = new() { "1", "true", "yes", "on" };

    public static readonly IReadOnlyList<string> DefaultForbiddenGlobs = new[]
    {
        "infra/**", "migrations/**", "**/.env*", "**/secrets/**",
    };

    public const string DefaultTriggerLabel = "foundry:candidate";
    public const string DefaultTriggerStatus = "Ready for AI Analysis";

    /// <summary>
    /// Path patterns that indicate a PR actually touched a sensitive area. Used
    /// by the diff-aware risk check after a PR opens/updates - the upfront
    /// (ticket-text) risk classification can miss work that only becomes
    /// sensitive in the diff.
    /// </summary>
    public static readonly IReadOnlyDictionary<string, IReadOnlyList<string>> DefaultSensitivePathGlobs =
        new Dictionary<string, IReadOnlyList<string>>
        {
            ["auth"] = new[]
            {
                "**/auth/**", "**/authn/**", "**/authz/**", "**/login/**",
                "**/session*/**", "**/sso/**", "**/oauth/**",
            },
            ["payments"] = new[]
            {
                "**/payment*/**", "**/billing/**", "**/stripe/**",
                "**/invoice*/**", "**/checkout/**",
            },
            ["database_migration"] = new[] { "**/migrations/**", "**/migrate/**", "**/alembic/**" },
            ["infrastructure"] = new[]
            {
                "infra/**", "**/terraform/**", "**/helm/**", "**/k8s/**",
                "**/.github/workflows/**", "**/Dockerfile*",
            },
            ["customer_data"] = new[] { "**/customers/**", "**/customer_data/**" },
        };

    // --- storage (secret: env) ---
    public string DatabaseUrl { get; init; } = "sqlite:///:memory:";

    // --- webhook signing secrets (secret: env) ---
    public string LinearWebhookSecret { get; init; } = "";
    public string? GithubWebhookSecret { get; init; }

    // --- outbound API tokens (secret: env); null => connector not wired live ---
    public string? LinearApiToken { get; init; }
    public string? GithubApiToken { get; init; }

    // --- Jira tracker (base url: yaml or env; credentials + secret: env) ---
    public string? JiraWebhookSecret { get; init; }
    public string? JiraBaseUrl { get; init; }
    public string? JiraEmail { get; init; }
    public string? JiraApiToken { get; init; }

    // --- GitLab SCM (secret: env); null => endpoint disabled ---
    public string? GitlabWebhookSecret { get; init; }

    // --- API auth (secret: env); null => mutating API endpoints are disabled ---
    public string? ApiToken { get; init; }

    // --- issue tracker (behaviour: yaml) ---
    public string TrackerProvider { get; init; } = "linear";

    // --- coding agent (behaviour: yaml; tokens: env) ---
    public string AgentProvider { get; init; } = "manual";
    public string? CursorApiToken { get; init; }
    public string ClaudeWorkflowFile { get; init; } = "foundry-claude-code.yml";
    public string? AgentWebhookUrl { get; init; }
    public string? AgentWebhookSecret { get; init; }

    // --- intelligence (behaviour: yaml) ---
    public bool UseOpenaiAnalyzer { get; init; }
    public string OpenaiModel { get; init; } = "gpt-5.5";

    // --- policy / safety knobs (behaviour: yaml) ---
    public int RepoConfidenceThreshold { get; init; } = 70;
    public int MaxFilesChanged { get; init; } = 12;
    public IReadOnlyList<string> ForbiddenGlobs { get; init; } = DefaultForbiddenGlobs;
    public IReadOnlyDictionary<string, IReadOnlyList<string>> SensitivePathGlobs { get; init; } =
        DefaultSensitivePathGlobs;

    // --- remediation / feedback loop (behaviour: yaml) ---
    public int MaxAgentRetries { get; init; } = 2;
    public IReadOnlyList<string> RetryOn { get; init; } = new[] { "ci_failed", "changes_requested" };
    public double? MaxCostPerRun { get; init; }

    // --- triggers (behaviour: yaml) ---
    public string TriggerLabel { get; init; } = DefaultTriggerLabel;
    public string TriggerStatus { get; init; } = DefaultTriggerStatus;

    // --- approval (behaviour: yaml) ---
    // Who may approve runs. Role grants are configured per user, never
    // asserted by the API caller. A user with no roles can approve ordinary
    // work but cannot satisfy sensitive-area approval requirements.
    public IReadOnlyList<(string Email, IReadOnlyList<string> Roles)> Approvers { get; init; } =
        Array.Empty<(string, IReadOnlyList<string>)>();

    // --- durable execution (behaviour: yaml; address often env) ---
    public string TemporalAddress { get; init; } = "localhost:7233";
    public string TaskQueue { get; init; } = "foundry-ticket-to-pr";

    // ----------------------------------------------------------------- loaders

    /// <summary>Build settings from defaults, then YAML, then environment overrides.</summary>
    public static Settings Load(string? path = null, IReadOnlyDictionary<string, string>? env = null)
    {
        var settings = new Settings();
        if (path is not null && File.Exists(path))
        {
            settings = FromYaml(settings, File.ReadAllText(path), path);
        }
        settings = ApplyEnv(settings, env ?? EnvironmentVariables());
        settings.Validate();
        return settings;
    }

    /// <summary>Defaults overlaid with environment only (no YAML).</summary>
    public static Settings FromEnv(IReadOnlyDictionary<string, string>? env = null) =>
        Load(path: null, env: env);

    private static IReadOnlyDictionary<string, string> EnvironmentVariables() =>
        Environment.GetEnvironmentVariables()
            .Cast<System.Collections.DictionaryEntry>()
            .ToDictionary(e => (string)e.Key, e => (string?)e.Value ?? "");

    private void Validate()
    {
        if (RepoConfidenceThreshold is < 0 or > 100)
        {
            throw new ArgumentException(
                $"repo_confidence_threshold must be 0-100, got {RepoConfidenceThreshold}");
        }
        if (MaxFilesChanged < 1)
        {
            throw new ArgumentException($"max_files_changed must be >= 1, got {MaxFilesChanged}");
        }
        if (MaxAgentRetries < 0)
        {
            throw new ArgumentException($"max_agent_retries must be >= 0, got {MaxAgentRetries}");
        }
        var unknown = RetryOn.Except(new[] { "ci_failed", "changes_requested" }).ToList();
        if (unknown.Count > 0)
        {
            throw new ArgumentException(
                $"unknown retry_on triggers: [{string.Join(", ", unknown.OrderBy(t => t, StringComparer.Ordinal))}]");
        }
        if (MaxCostPerRun is not null && MaxCostPerRun <= 0)
        {
            throw new ArgumentException($"max_cost_per_run must be positive, got {MaxCostPerRun}");
        }
    }

    // ------------------------------------------------------------- accessors

    public IReadOnlySet<string> ApproverEmails =>
        Approvers.Select(a => a.Email).ToHashSet();

    /// <summary>Roles configured for <paramref name="user"/>; empty when unknown or role-less.</summary>
    public IReadOnlySet<string> RolesFor(string user)
    {
        foreach (var (email, roles) in Approvers)
        {
            if (email == user)
            {
                return roles.ToHashSet();
            }
        }
        return new HashSet<string>();
    }

    public IReadOnlyDictionary<string, IReadOnlyList<string>> SensitiveGlobsMap => SensitivePathGlobs;

    // -------------------------------------------------------------- yaml/env

    private static Settings FromYaml(Settings settings, string yamlText, string path)
    {
        var deserializer = new DeserializerBuilder().Build();
        object? raw = deserializer.Deserialize<object?>(yamlText);
        if (raw is null)
        {
            return settings;
        }
        if (raw is not Dictionary<object, object?> data)
        {
            throw new ArgumentException($"{path} must contain a YAML mapping at the top level");
        }

        var database = Section(data, "database");
        if (TryGet(database, "url", out var dbUrl))
        {
            settings = settings with { DatabaseUrl = Str(dbUrl) };
        }

        var analyzer = Section(data, "analyzer");
        if (TryGet(analyzer, "provider", out var analyzerProvider))
        {
            settings = settings with { UseOpenaiAnalyzer = Str(analyzerProvider) == "openai" };
        }
        if (TryGet(analyzer, "model", out var model))
        {
            settings = settings with { OpenaiModel = Str(model) };
        }

        var agent = Section(data, "agent");
        if (TryGet(agent, "provider", out var agentProvider))
        {
            settings = settings with { AgentProvider = Str(agentProvider) };
        }
        if (TryGet(agent, "claude_workflow_file", out var workflowFile))
        {
            settings = settings with { ClaudeWorkflowFile = Str(workflowFile) };
        }

        var tracker = Section(data, "tracker");
        if (TryGet(tracker, "provider", out var trackerProvider))
        {
            settings = settings with { TrackerProvider = Str(trackerProvider) };
        }
        if (TryGet(tracker, "jira_base_url", out var jiraBaseUrl))
        {
            settings = settings with { JiraBaseUrl = Str(jiraBaseUrl) };
        }

        var policy = Section(data, "policy");
        if (TryGet(policy, "repo_confidence_threshold", out var threshold))
        {
            settings = settings with { RepoConfidenceThreshold = Int(threshold) };
        }
        if (TryGet(policy, "max_files_changed", out var maxFiles))
        {
            settings = settings with { MaxFilesChanged = Int(maxFiles) };
        }
        if (TryGet(policy, "forbidden_globs", out var forbidden))
        {
            settings = settings with { ForbiddenGlobs = StrList(forbidden) };
        }
        if (TryGet(policy, "sensitive_path_globs", out var sensitive))
        {
            var map = new Dictionary<string, IReadOnlyList<string>>();
            if (sensitive is Dictionary<object, object?> sensitiveMap)
            {
                foreach (var (area, globs) in sensitiveMap)
                {
                    map[Str(area)] = StrList(globs);
                }
            }
            settings = settings with { SensitivePathGlobs = map };
        }

        var remediation = Section(data, "remediation");
        if (TryGet(remediation, "max_agent_retries", out var maxRetries))
        {
            settings = settings with { MaxAgentRetries = Int(maxRetries) };
        }
        if (TryGet(remediation, "retry_on", out var retryOn))
        {
            settings = settings with { RetryOn = StrList(retryOn) };
        }

        var budget = Section(data, "budget");
        if (TryGet(budget, "max_cost_per_run", out var maxCost))
        {
            settings = settings with
            {
                MaxCostPerRun = maxCost is null
                    ? null
                    : double.Parse(Str(maxCost), CultureInfo.InvariantCulture),
            };
        }

        var triggers = Section(data, "triggers");
        if (TryGet(triggers, "label", out var label))
        {
            settings = settings with { TriggerLabel = Str(label) };
        }
        if (TryGet(triggers, "status", out var status))
        {
            settings = settings with { TriggerStatus = Str(status) };
        }

        var approval = Section(data, "approval");
        if (TryGet(approval, "approvers", out var approvers))
        {
            var parsed = new List<(string, IReadOnlyList<string>)>();
            if (approvers is List<object?> entries)
            {
                foreach (var entry in entries)
                {
                    if (entry is Dictionary<object, object?> record)
                    {
                        var email = TryGet(record, "email", out var emailValue) ? Str(emailValue) : "";
                        var roles = TryGet(record, "roles", out var rolesValue)
                            ? StrList(rolesValue)
                            : Array.Empty<string>();
                        parsed.Add((email, roles));
                    }
                }
            }
            settings = settings with { Approvers = parsed };
        }
        else if (TryGet(approval, "authorised_approvers", out var legacy))
        {
            // Legacy form: a flat list of emails, no role grants.
            settings = settings with
            {
                Approvers = StrList(legacy)
                    .Select(email => (email, (IReadOnlyList<string>)Array.Empty<string>()))
                    .ToList(),
            };
        }

        var temporal = Section(data, "temporal");
        if (TryGet(temporal, "address", out var address))
        {
            settings = settings with { TemporalAddress = Str(address) };
        }
        if (TryGet(temporal, "task_queue", out var taskQueue))
        {
            settings = settings with { TaskQueue = Str(taskQueue) };
        }

        return settings;
    }

    private static Dictionary<object, object?> Section(Dictionary<object, object?> data, string key) =>
        data.TryGetValue(key, out var value) && value is Dictionary<object, object?> section
            ? section
            : new Dictionary<object, object?>();

    private static bool TryGet(Dictionary<object, object?> section, string key, out object? value) =>
        section.TryGetValue(key, out value);

    private static string Str(object? value) => value?.ToString() ?? "";

    private static int Int(object? value) => int.Parse(Str(value), CultureInfo.InvariantCulture);

    private static IReadOnlyList<string> StrList(object? value) =>
        value is List<object?> list
            ? list.Select(Str).ToList()
            : Array.Empty<string>();

    /// <summary>Only keys actually present in the environment, so we never clobber YAML.</summary>
    private static Settings ApplyEnv(Settings settings, IReadOnlyDictionary<string, string> env)
    {
        Settings Apply(string key, Func<Settings, string, Settings> setter) =>
            env.TryGetValue(key, out var value) ? setter(settings, value) : settings;

        settings = Apply("FOUNDRY_DATABASE_URL", (s, v) => s with { DatabaseUrl = v });
        settings = Apply("FOUNDRY_LINEAR_WEBHOOK_SECRET", (s, v) => s with { LinearWebhookSecret = v });
        settings = Apply("FOUNDRY_GITHUB_WEBHOOK_SECRET", (s, v) => s with { GithubWebhookSecret = v });
        settings = Apply("FOUNDRY_LINEAR_API_TOKEN", (s, v) => s with { LinearApiToken = v });
        settings = Apply("FOUNDRY_GITHUB_API_TOKEN", (s, v) => s with { GithubApiToken = v });
        settings = Apply("FOUNDRY_JIRA_WEBHOOK_SECRET", (s, v) => s with { JiraWebhookSecret = v });
        settings = Apply("FOUNDRY_JIRA_BASE_URL", (s, v) => s with { JiraBaseUrl = v });
        settings = Apply("FOUNDRY_JIRA_EMAIL", (s, v) => s with { JiraEmail = v });
        settings = Apply("FOUNDRY_JIRA_API_TOKEN", (s, v) => s with { JiraApiToken = v });
        settings = Apply("FOUNDRY_GITLAB_WEBHOOK_SECRET", (s, v) => s with { GitlabWebhookSecret = v });
        settings = Apply("FOUNDRY_API_TOKEN", (s, v) => s with { ApiToken = v });
        settings = Apply("FOUNDRY_AGENT_PROVIDER", (s, v) => s with { AgentProvider = v });
        settings = Apply("FOUNDRY_TRACKER_PROVIDER", (s, v) => s with { TrackerProvider = v });
        settings = Apply("FOUNDRY_CURSOR_API_TOKEN", (s, v) => s with { CursorApiToken = v });
        settings = Apply("FOUNDRY_AGENT_WEBHOOK_URL", (s, v) => s with { AgentWebhookUrl = v });
        settings = Apply("FOUNDRY_AGENT_WEBHOOK_SECRET", (s, v) => s with { AgentWebhookSecret = v });
        settings = Apply("FOUNDRY_OPENAI_MODEL", (s, v) => s with { OpenaiModel = v });
        settings = Apply("TEMPORAL_ADDRESS", (s, v) => s with { TemporalAddress = v });
        settings = Apply("FOUNDRY_TASK_QUEUE", (s, v) => s with { TaskQueue = v });
        if (env.TryGetValue("FOUNDRY_USE_OPENAI_ANALYZER", out var useOpenai))
        {
            settings = settings with
            {
                UseOpenaiAnalyzer = TrueValues.Contains(useOpenai.Trim().ToLowerInvariant()),
            };
        }
        return settings;
    }
}
