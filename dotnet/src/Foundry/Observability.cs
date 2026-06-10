// Lightweight tracing via System.Diagnostics.ActivitySource.
//
// Foundry's value rests on explainability, so the run path is instrumented
// with activities (the .NET-native equivalent of OpenTelemetry spans - any
// OTel SDK can subscribe to this source). With no listener attached,
// StartActivity returns null and the instrumentation is effectively free.

using System.Diagnostics;

namespace Foundry;

public static class Observability
{
    public static readonly ActivitySource Source = new("foundry");

    /// <summary>Start an activity if anyone is listening; otherwise a no-op (null).</summary>
    public static Activity? Span(string name) => Source.StartActivity(name);
}
