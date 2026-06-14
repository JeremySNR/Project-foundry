// EF Core context and a tiny database helper.
//
// The foundation uses SQLite by default so the data model is exercisable in
// tests without Postgres (mirroring the Python SQLAlchemy + SQLite setup).
// Production deployments point the same context at Postgres or SQL Server by
// configuring different DbContextOptions.

using Foundry.Schemas;
using Microsoft.Data.Sqlite;
using Microsoft.EntityFrameworkCore;

namespace Foundry.Db;

public class FoundryDbContext : DbContext
{
    public FoundryDbContext(DbContextOptions<FoundryDbContext> options) : base(options) { }

    public DbSet<FoundryRun> Runs => Set<FoundryRun>();
    public DbSet<FoundryArtifact> Artifacts => Set<FoundryArtifact>();
    public DbSet<FoundryAuditEvent> AuditEvents => Set<FoundryAuditEvent>();
    public DbSet<FoundryPolicyDecision> PolicyDecisions => Set<FoundryPolicyDecision>();
    public DbSet<FoundryAgentJob> AgentJobs => Set<FoundryAgentJob>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        var run = modelBuilder.Entity<FoundryRun>();
        run.ToTable("foundry_runs");
        run.HasKey(r => r.Id);
        run.HasIndex(r => r.LinearIssueId);
        run.HasIndex(r => r.LinearIssueKey);
        run.Property(r => r.Status).HasConversion(v => v.ToWire(), s => Wire.FromWire<RunStatus>(s));
        run.Property(r => r.RiskLevel)
            .HasConversion(v => v == null ? null : v.Value.ToWire(),
                           s => s == null ? null : Wire.FromWire<OverallRisk>(s));
        run.Property(r => r.AgentMode)
            .HasConversion(v => v == null ? null : v.Value.ToWire(),
                           s => s == null ? null : Wire.FromWire<AgentMode>(s));
        run.HasMany(r => r.Artifacts).WithOne(a => a.Run).HasForeignKey(a => a.RunId)
            .OnDelete(DeleteBehavior.Cascade);
        run.HasMany(r => r.AuditEvents).WithOne(e => e.Run).HasForeignKey(e => e.RunId)
            .OnDelete(DeleteBehavior.Cascade);
        run.HasMany(r => r.PolicyDecisions).WithOne(d => d.Run).HasForeignKey(d => d.RunId)
            .OnDelete(DeleteBehavior.Cascade);
        run.HasMany(r => r.AgentJobs).WithOne(j => j.Run).HasForeignKey(j => j.RunId)
            .OnDelete(DeleteBehavior.Cascade);

        var artifact = modelBuilder.Entity<FoundryArtifact>();
        artifact.ToTable("foundry_artifacts");
        artifact.HasKey(a => a.Id);
        artifact.HasIndex(a => a.RunId);
        artifact.HasIndex(a => a.ContentHash);
        artifact.HasIndex(a => new { a.RunId, a.ArtifactType }).HasDatabaseName("idx_artifact_run_type");
        artifact.Property(a => a.ArtifactType)
            .HasConversion(v => v.ToWire(), s => Wire.FromWire<ArtifactType>(s));

        var auditEvent = modelBuilder.Entity<FoundryAuditEvent>();
        auditEvent.ToTable("foundry_audit_events");
        auditEvent.HasKey(e => e.Id);
        auditEvent.HasIndex(e => e.RunId);
        auditEvent.Property(e => e.EventType)
            .HasConversion(v => v.ToWire(), s => AuditEventTypeWire.AuditEventTypeFromWire(s));

        var decision = modelBuilder.Entity<FoundryPolicyDecision>();
        decision.ToTable("foundry_policy_decisions");
        decision.HasKey(d => d.Id);
        decision.HasIndex(d => d.RunId);

        var job = modelBuilder.Entity<FoundryAgentJob>();
        job.ToTable("foundry_agent_jobs");
        job.HasKey(j => j.Id);
        job.HasIndex(j => j.RunId);
        job.Property(j => j.Status)
            .HasConversion(v => v.ToWire(), s => Wire.FromWire<AgentJobStatus>(s));
    }

    public override int SaveChanges()
    {
        AssignAuditSequences();
        TouchUpdatedAt();
        return base.SaveChanges();
    }

    public override Task<int> SaveChangesAsync(CancellationToken cancellationToken = default)
    {
        AssignAuditSequences();
        TouchUpdatedAt();
        return base.SaveChangesAsync(cancellationToken);
    }

    /// <summary>
    /// Give new audit events their monotonic per-run sequence numbers.
    ///
    /// The audit trail promises a guaranteed order independent of timestamp
    /// ties; that only holds if something actually assigns the numbers. Done
    /// here, at save time, so every code path that adds an event gets it for free.
    /// </summary>
    private void AssignAuditSequences()
    {
        var newEvents = ChangeTracker.Entries<FoundryAuditEvent>()
            .Where(e => e.State == EntityState.Added)
            .Select(e => e.Entity)
            .ToList();
        if (newEvents.Count == 0)
        {
            return;
        }
        foreach (var group in newEvents.GroupBy(e => e.RunId))
        {
            var current = AuditEvents.Local
                .Where(e => e.RunId == group.Key && !newEvents.Contains(e))
                .Select(e => (int?)e.Sequence)
                .Concat(AuditEvents.AsNoTracking()
                    .Where(e => e.RunId == group.Key)
                    .Select(e => (int?)e.Sequence)
                    .ToList())
                .Max();
            var nextSequence = current is int max ? max + 1 : 0;
            foreach (var evt in group)
            {
                evt.Sequence = nextSequence;
                nextSequence += 1;
            }
        }
    }

    private void TouchUpdatedAt()
    {
        foreach (var entry in ChangeTracker.Entries<FoundryRun>()
                     .Where(e => e.State == EntityState.Modified))
        {
            entry.Entity.UpdatedAt = DateTime.UtcNow;
        }
    }
}

/// <summary>
/// Owns the database connection and hands out contexts - the C# analogue of
/// the Python make_engine / make_session_factory pair. For SQLite in-memory
/// the single shared connection keeps the database alive between contexts
/// (each new :memory: connection would otherwise be a fresh database).
/// </summary>
public sealed class FoundryDataStore : IDisposable
{
    private readonly SqliteConnection _connection;
    private readonly DbContextOptions<FoundryDbContext> _options;

    private FoundryDataStore(SqliteConnection connection)
    {
        _connection = connection;
        _options = new DbContextOptionsBuilder<FoundryDbContext>()
            .UseSqlite(connection)
            .Options;
    }

    /// <summary>An in-memory SQLite store with the schema created (tests, demos).</summary>
    public static FoundryDataStore InMemory()
    {
        var connection = new SqliteConnection("DataSource=:memory:");
        connection.Open();
        var store = new FoundryDataStore(connection);
        using var context = store.CreateContext();
        context.Database.EnsureCreated();
        return store;
    }

    /// <summary>A file-backed SQLite store, creating the schema when missing.</summary>
    public static FoundryDataStore Sqlite(string path)
    {
        var connection = new SqliteConnection($"DataSource={path}");
        connection.Open();
        var store = new FoundryDataStore(connection);
        using var context = store.CreateContext();
        context.Database.EnsureCreated();
        return store;
    }

    public FoundryDbContext CreateContext() => new(_options);

    public void Dispose() => _connection.Dispose();
}
