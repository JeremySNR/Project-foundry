// Wire-format names for enums: the snake_case strings used in JSON, the
// database, and human-readable policy reasons - identical to the Python
// string-enum values.

using System.Text;
using System.Text.Json;

namespace Foundry.Schemas;

public static class Wire
{
    /// <summary>The snake_case wire value for an enum member (e.g. NeedsClarification -> "needs_clarification").</summary>
    public static string ToWire<T>(this T value) where T : struct, Enum =>
        JsonNamingPolicy.SnakeCaseLower.ConvertName(value.ToString());

    /// <summary>Parse a snake_case wire value back into an enum member.</summary>
    public static T FromWire<T>(string wire) where T : struct, Enum
    {
        foreach (var value in Enum.GetValues<T>())
        {
            if (value.ToWire() == wire)
            {
                return value;
            }
        }
        throw new SchemaValidationException($"'{wire}' is not a valid {typeof(T).Name}");
    }

    /// <summary>"tech_debt" -> "Tech Debt" (Python's value.replace('_',' ').title()).</summary>
    public static string ToTitle(string wire)
    {
        var builder = new StringBuilder(wire.Length);
        var upperNext = true;
        foreach (var c in wire)
        {
            if (c == '_')
            {
                builder.Append(' ');
                upperNext = true;
            }
            else
            {
                builder.Append(upperNext ? char.ToUpperInvariant(c) : c);
                upperNext = false;
            }
        }
        return builder.ToString();
    }
}
