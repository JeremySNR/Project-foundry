// JSON conventions shared by every Foundry artifact.
//
// The wire format is deliberately identical to the Python implementation:
// snake_case property names, snake_case string enums, nulls included. Unknown
// properties are rejected (the equivalent of Pydantic's extra="forbid") so a
// hallucinated field from an LLM cannot slip through deserialisation.

using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace Foundry.Schemas;

/// <summary>Raised when an artifact fails schema validation (Pydantic's ValidationError).</summary>
public class SchemaValidationException : Exception
{
    public SchemaValidationException(string message) : base(message) { }
}

public static class FoundryJson
{
    /// <summary>Options for serialising artifacts: snake_case, string enums, nulls kept.</summary>
    public static readonly JsonSerializerOptions Options = CreateOptions(strict: false);

    /// <summary>Strict options for deserialising: unknown members are rejected.</summary>
    public static readonly JsonSerializerOptions StrictOptions = CreateOptions(strict: true);

    private static JsonSerializerOptions CreateOptions(bool strict)
    {
        var options = new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
            DictionaryKeyPolicy = null,
            DefaultIgnoreCondition = JsonIgnoreCondition.Never,
        };
        options.Converters.Add(new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower));
        if (strict)
        {
            options.UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow;
        }
        return options;
    }

    public static string Serialize<T>(T value) => JsonSerializer.Serialize(value, Options);

    public static T Deserialize<T>(string json)
    {
        T? value;
        try
        {
            value = JsonSerializer.Deserialize<T>(json, StrictOptions);
        }
        catch (JsonException exc) when (exc.InnerException is SchemaValidationException inner)
        {
            throw inner;
        }
        catch (JsonException exc)
        {
            throw new SchemaValidationException(exc.Message);
        }
        if (value is null)
        {
            throw new SchemaValidationException($"JSON deserialised to null for {typeof(T).Name}");
        }
        return value;
    }

    /// <summary>
    /// Deterministic JSON for stable hashing: keys sorted recursively, compact
    /// separators - the same canonical form the Python audit layer produces.
    /// </summary>
    public static string Canonical(object? content)
    {
        var node = JsonSerializer.SerializeToNode(content, Options);
        var sorted = SortNode(node);
        return sorted?.ToJsonString(new JsonSerializerOptions { WriteIndented = false }) ?? "null";
    }

    private static JsonNode? SortNode(JsonNode? node) => node switch
    {
        JsonObject obj => new JsonObject(
            obj.OrderBy(p => p.Key, StringComparer.Ordinal)
                .Select(p => new KeyValuePair<string, JsonNode?>(p.Key, SortNode(p.Value)))),
        JsonArray arr => new JsonArray(arr.Select(SortNode).ToArray()),
        null => null,
        _ => node.DeepClone(),
    };
}

internal static class Guard
{
    public static int Range(int value, int min, int max, string name)
    {
        if (value < min || value > max)
        {
            throw new SchemaValidationException($"{name} must be between {min} and {max}, got {value}");
        }
        return value;
    }

    public static int Min(int value, int min, string name)
    {
        if (value < min)
        {
            throw new SchemaValidationException($"{name} must be >= {min}, got {value}");
        }
        return value;
    }

    public static double MinDouble(double value, double min, string name)
    {
        if (value < min)
        {
            throw new SchemaValidationException($"{name} must be >= {min}, got {value}");
        }
        return value;
    }

    public static double? Positive(double? value, string name)
    {
        if (value is not null && value <= 0)
        {
            throw new SchemaValidationException($"{name} must be positive, got {value}");
        }
        return value;
    }
}
