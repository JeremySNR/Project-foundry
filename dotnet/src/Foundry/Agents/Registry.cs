// Provider registry - resolve a CodingAgentProvider by name.
//
// The registry exists so the rest of the system never news up a concrete
// provider directly; agent.provider in the YAML config selects one by name.
// Providers that need live transports or trackers register a factory that
// receives them; the parameterless ones (manual, fake) work out of the box.

namespace Foundry.Agents;

public static class ProviderRegistry
{
    private static readonly Dictionary<string, Func<CodingAgentProvider>> Factories = new()
    {
        [ManualProvider.ProviderName] = () => new ManualProvider(),
        [InMemoryFakeProvider.ProviderName] = () => new InMemoryFakeProvider(),
        [CursorViaLinearProvider.ProviderName] = () => throw new InvalidOperationException(
            "cursor_via_linear requires an IIssueTracker; construct CursorViaLinearProvider directly or register a factory"),
        [CursorCloudAgentProvider.ProviderName] = () => throw new InvalidOperationException(
            "cursor_cloud requires HTTP transports; construct CursorCloudAgentProvider directly or register a factory"),
        [ClaudeCodeProvider.ProviderName] = () => throw new InvalidOperationException(
            "claude_code requires an HTTP transport; construct ClaudeCodeProvider directly or register a factory"),
        [WebhookProvider.ProviderName] = () => throw new InvalidOperationException(
            "webhook requires a URL and HTTP transport; construct WebhookProvider directly or register a factory"),
    };

    /// <summary>Register (or replace) a provider factory under its name.</summary>
    public static void Register(string name, Func<CodingAgentProvider> factory) =>
        Factories[name] = factory;

    public static IReadOnlyList<string> AvailableProviders() =>
        Factories.Keys.OrderBy(n => n, StringComparer.Ordinal).ToList();

    public static CodingAgentProvider GetProvider(string name)
    {
        if (!Factories.TryGetValue(name, out var factory))
        {
            throw new ArgumentException(
                $"unknown coding-agent provider '{name}'; available: [{string.Join(", ", AvailableProviders())}]");
        }
        return factory();
    }
}
