"""Assemble, verify, and render a run's compliance evidence pack.

``build_evidence_pack`` reads a single run's full chain out of the DB and
returns a JSON-serialisable dict. ``verify_integrity`` recomputes what can be
recomputed - every artifact's content hash, the contiguity of the append-only
audit sequence, and the cross-row hash chain linking the audit events - so an
auditor can confirm the export wasn't tampered with between storage and export.
``render_evidence_html`` produces a zero-build standalone page from that dict.

Honest about what the verification *is*: artifacts are content-addressed
(``sha256(content_json)``), so we recompute and compare those; we check the
per-run audit ``sequence`` is gap-free and strictly increasing; and we recompute
the linked hash chain in which each audit event commits to the previous event's
hash, so dropping, reordering, editing, or inserting a row is detectable
(issue #36). The chain has no external anchor - a wholesale rewrite that
recomputes every downstream hash consistently would still verify - so we don't
oversell it as immutable provenance (see issue #24); it is tamper-*evidence*,
not tamper-proofing.
"""

from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from foundry.audit.events import AUDIT_CHAIN_GENESIS, audit_event_chain_hash
from foundry.db.models import (
    ArtifactType,
    FoundryAgentJob,
    FoundryArtifact,
    FoundryAuditEvent,
    FoundryPolicyDecision,
    FoundryRun,
)

from .controls import ControlMapping

# Artifact types that exist at most once per run (latest version wins) and the
# evidence-pack section each maps to. APPROVAL_RECORD is handled separately
# because a run can have several.
_SINGLETON_SECTIONS: dict[ArtifactType, str] = {
    ArtifactType.TICKET_SNAPSHOT: "ticket",
    ArtifactType.TICKET_ANALYSIS: "analysis",
    ArtifactType.CONTEXT_BUNDLE: "context",
    ArtifactType.DELIVERY_PLAN: "plan",
    ArtifactType.RISK_ASSESSMENT: "risk_assessment",
    ArtifactType.PR_STATE: "pr",
    ArtifactType.FINAL_SUMMARY: "final_summary",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _run_section(run: FoundryRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "parent_run_id": run.parent_run_id,
        "linear_issue_id": run.linear_issue_id,
        "linear_issue_key": run.linear_issue_key,
        "status": run.status.value,
        "trigger_type": run.trigger_type,
        "current_step": run.current_step,
        "risk_level": run.risk_level.value if run.risk_level else None,
        "agent_mode": run.agent_mode.value if run.agent_mode else None,
        "approved_by": run.approved_by,
        "approved_at": _iso(run.approved_at),
        "created_by": run.created_by,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
    }


def verify_integrity(
    artifacts: list[FoundryArtifact], events: list[FoundryAuditEvent]
) -> dict[str, Any]:
    """Recompute artifact content hashes and check audit-sequence continuity.

    Returns a structured result with a top-level ``verified`` flag plus the
    detail an auditor needs to see *what* was checked and what (if anything)
    failed.
    """
    artifact_results: list[dict[str, Any]] = []
    artifacts_ok = True
    for art in artifacts:
        recomputed = hashlib.sha256(art.content_json.encode("utf-8")).hexdigest()
        ok = recomputed == art.content_hash
        artifacts_ok = artifacts_ok and ok
        artifact_results.append(
            {
                "id": art.id,
                "artifact_type": art.artifact_type.value,
                "version": art.version,
                "stored_hash": art.content_hash,
                "recomputed_hash": recomputed,
                "ok": ok,
            }
        )

    sequences = [e.sequence for e in events]
    ordered = sequences == sorted(sequences)
    unique = len(sequences) == len(set(sequences))
    # Per-run sequences are assigned contiguously starting at 0 (see db/base.py),
    # so the full trail must be exactly 0..N-1. A missing value - including a
    # removed first event - therefore breaks contiguity.
    contiguous = sorted(sequences) == list(range(len(sequences)))
    sequence_ok = ordered and unique and contiguous

    chain = _verify_audit_chain(events)

    return {
        "verified": artifacts_ok and sequence_ok and chain["ok"],
        "method": (
            "Recomputed sha256(content_json) for each artifact and compared to "
            "the stored content hash; checked the per-run audit sequence is "
            "exactly 0..N-1 (gap-free, unique, strictly increasing); and "
            "recomputed the cross-row hash chain in which each audit event "
            "commits to the previous event's hash, so a dropped, reordered, "
            "edited or inserted row breaks the chain. Events written before the "
            "chain column existed (no stored hash) are reported as un-chained "
            "rather than failing verification."
        ),
        "artifacts": {
            "checked": len(artifact_results),
            "ok": artifacts_ok,
            "failed": [r["id"] for r in artifact_results if not r["ok"]],
            "details": artifact_results,
        },
        "audit_sequence": {
            "ok": sequence_ok,
            "count": len(sequences),
            "ordered": ordered,
            "unique": unique,
            "contiguous": contiguous,
        },
        "audit_chain": chain,
    }


def _verify_audit_chain(events: list[FoundryAuditEvent]) -> dict[str, Any]:
    """Recompute the cross-row linked hash chain over a run's audit events.

    Walks the events in ``sequence`` order, recomputing each event's chain hash
    from the previous event's *stored* hash (so a single tampered row is flagged
    locally rather than cascading) and comparing to the stored value. Returns:

    - ``present`` - whether a chain exists to check at all. A run with no events,
      or one whose events predate the chain column (any ``content_hash`` is
      ``NULL``), has no chain; such a run is reported un-chained and does **not**
      fail verification, so enabling the chain on an existing DB is safe.
    - ``ok`` - every present link recomputed to its stored hash (vacuously true
      when there is no chain to check).
    - ``checked`` - number of links verified.
    - ``broken_at`` - the sequence numbers whose stored hash didn't match.
    """
    present = bool(events) and all(e.content_hash is not None for e in events)
    if not present:
        return {"ok": True, "present": False, "checked": 0, "broken_at": []}

    broken_at: list[int] = []
    prev = AUDIT_CHAIN_GENESIS
    for event in sorted(events, key=lambda e: e.sequence):
        expected = audit_event_chain_hash(prev, event)
        if expected != event.content_hash:
            broken_at.append(event.sequence)
        # Advance on the stored hash so one altered row doesn't cascade into
        # every later link; a re-hashed forgery still breaks the next link.
        prev = event.content_hash
    return {
        "ok": not broken_at,
        "present": True,
        "checked": len(events),
        "broken_at": broken_at,
    }


def build_evidence_pack(
    session: Session,
    run: FoundryRun,
    *,
    control_mappings: tuple[ControlMapping, ...] = (),
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the full evidence pack for ``run`` as a JSON-serialisable dict."""
    run_id = run.id
    artifacts = (
        session.query(FoundryArtifact)
        .filter_by(run_id=run_id)
        .order_by(FoundryArtifact.created_at, FoundryArtifact.version)
        .all()
    )
    events = (
        session.query(FoundryAuditEvent)
        .filter_by(run_id=run_id)
        .order_by(FoundryAuditEvent.sequence)
        .all()
    )
    decisions = (
        session.query(FoundryPolicyDecision)
        .filter_by(run_id=run_id)
        .order_by(FoundryPolicyDecision.created_at)
        .all()
    )
    jobs = (
        session.query(FoundryAgentJob)
        .filter_by(run_id=run_id)
        .order_by(FoundryAgentJob.started_at)
        .all()
    )

    # Singleton sections: ordered by (created_at, version), so the last seen of
    # each type is the latest.
    singletons: dict[str, dict[str, Any]] = {}
    approvals: list[dict[str, Any]] = []
    for art in artifacts:
        section = _SINGLETON_SECTIONS.get(art.artifact_type)
        record = {
            "artifact_id": art.id,
            "version": art.version,
            "content_hash": art.content_hash,
            "created_at": _iso(art.created_at),
            "content": json.loads(art.content_json),
        }
        if section is not None:
            singletons[section] = record
        elif art.artifact_type is ArtifactType.APPROVAL_RECORD:
            content = record["content"]
            approvals.append(
                {
                    "approver": content.get("user") if isinstance(content, dict) else None,
                    "granted_roles": (
                        content.get("granted_roles", [])
                        if isinstance(content, dict)
                        else []
                    ),
                    "recorded_at": record["created_at"],
                    "artifact_id": art.id,
                    "content_hash": art.content_hash,
                }
            )

    policy_decisions = [
        {
            "decision_id": d.id,
            "policy_name": d.policy_name,
            "allowed": d.allowed,
            "reason": d.reason,
            "decision": json.loads(d.decision_json),
            "created_at": _iso(d.created_at),
        }
        for d in decisions
    ]
    agent_jobs = [
        {
            "id": j.id,
            "provider": j.provider,
            "provider_job_id": j.provider_job_id,
            "status": j.status.value,
            "repo": j.repo,
            "branch": j.branch,
            "pr_url": j.pr_url,
            "cost_usd": j.cost_usd,
            "started_at": _iso(j.started_at),
            "completed_at": _iso(j.completed_at),
        }
        for j in jobs
    ]
    audit_trail = [
        {
            "sequence": e.sequence,
            "event_type": e.event_type.value,
            "actor_type": e.actor_type,
            "actor_id": e.actor_id,
            "input_hash": e.input_hash,
            "output_hash": e.output_hash,
            "content_hash": e.content_hash,
            "metadata": json.loads(e.metadata_json) if e.metadata_json else None,
            "created_at": _iso(e.created_at),
        }
        for e in events
    ]

    integrity = verify_integrity(artifacts, events)

    # Which sections actually carry evidence for this run.
    present: set[str] = set(singletons)
    if approvals:
        present.add("approvals")
    if policy_decisions:
        present.add("policy_decisions")
    if agent_jobs:
        present.add("agent_jobs")
    if audit_trail:
        present.add("audit_trail")
    present.add("integrity")  # always produced

    controls = []
    for mapping in control_mappings:
        missing = [s for s in mapping.evidence if s not in present]
        controls.append(
            {
                **mapping.to_dict(),
                "satisfied": not missing,
                "missing_evidence": missing,
            }
        )

    pack: dict[str, Any] = {
        "generated_at": _iso(generated_at or datetime.now(timezone.utc)),
        "run": _run_section(run),
        "ticket": singletons.get("ticket"),
        "analysis": singletons.get("analysis"),
        "context": singletons.get("context"),
        "plan": singletons.get("plan"),
        "risk_assessment": singletons.get("risk_assessment"),
        "approvals": approvals,
        "policy_decisions": policy_decisions,
        "agent_jobs": agent_jobs,
        "pr": singletons.get("pr"),
        "final_summary": singletons.get("final_summary"),
        "audit_trail": audit_trail,
        "integrity": integrity,
        "control_mappings": controls,
    }
    return pack


def build_evidence_archive(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    control_mappings: tuple[ControlMapping, ...] = (),
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Org-wide evidence export: every run in a date range as one archive.

    Each run's full pack (the same one ``build_evidence_pack`` produces) is
    included, plus a rollup ``summary`` over the whole range: aggregate
    integrity, a status breakdown, and per-control coverage (how many runs in
    the range satisfy each configured control). ``since``/``until`` bound
    ``FoundryRun.created_at`` - ``since`` inclusive, ``until`` exclusive - and
    either may be ``None`` for an open bound.
    """
    stamp = generated_at or datetime.now(timezone.utc)
    query = session.query(FoundryRun)
    if since is not None:
        query = query.filter(FoundryRun.created_at >= since)
    if until is not None:
        query = query.filter(FoundryRun.created_at < until)
    runs = query.order_by(FoundryRun.created_at, FoundryRun.id).all()

    packs = [
        build_evidence_pack(
            session, run, control_mappings=control_mappings, generated_at=stamp
        )
        for run in runs
    ]

    return {
        "generated_at": _iso(stamp),
        "range": {"from": _iso(since), "to": _iso(until)},
        "run_count": len(packs),
        "summary": _summarise_packs(packs),
        "runs": packs,
    }


def _summarise_packs(packs: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll a list of per-run evidence packs up into one summary.

    Shared by the date-range archive and the per-epic export: aggregate
    integrity (every run's chain verified), a status breakdown, and per-control
    coverage (how many of the packs satisfy each configured control). An empty
    list summarises as vacuously verified with no coverage rows.
    """
    status_breakdown: dict[str, int] = {}
    failed_integrity: list[str] = []
    for pack in packs:
        status = pack["run"]["status"]
        status_breakdown[status] = status_breakdown.get(status, 0) + 1
        if not pack["integrity"]["verified"]:
            failed_integrity.append(pack["run"]["id"])

    # Per-control coverage, keyed by (framework, control_id) and emitted in
    # first-seen order so the rollup is stable for a fixed config.
    coverage: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for pack in packs:
        for c in pack["control_mappings"]:
            key = (c["framework"], c["control_id"])
            row = coverage.get(key)
            if row is None:
                row = {
                    "framework": c["framework"],
                    "control_id": c["control_id"],
                    "title": c["title"],
                    "satisfied_runs": 0,
                    "total_runs": 0,
                }
                coverage[key] = row
                order.append(key)
            row["total_runs"] += 1
            if c["satisfied"]:
                row["satisfied_runs"] += 1
    control_coverage = [
        {
            **coverage[key],
            "fully_satisfied": coverage[key]["satisfied_runs"]
            == coverage[key]["total_runs"],
        }
        for key in order
    ]

    return {
        "verified": not failed_integrity,
        "runs_verified": len(packs) - len(failed_integrity),
        "runs_failed_integrity": failed_integrity,
        "status_breakdown": status_breakdown,
        "control_coverage": control_coverage,
    }


def build_epic_evidence_pack(
    session: Session,
    root: FoundryRun,
    children: list[FoundryRun],
    *,
    control_mappings: tuple[ControlMapping, ...] = (),
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Cross-run evidence export: an epic's full chain as one document (#35).

    ``root`` is the epic's parent run and ``children`` its decomposed child runs
    (one per repo / scope). The export bundles the root's evidence pack plus
    each child's pack - the same ``build_evidence_pack`` produces - so the whole
    epic's chain (every ticket, plan, approval, policy decision and audit trail
    across all its repos) is one auditable artifact.

    Alongside the per-run packs it carries:

    - ``epic.rollup`` - the :func:`epics.compute_epic_rollup` status over the
      children (so this export and ``GET /runs/{id}/epic`` agree);
    - ``epic.root_run_id`` / ``epic.child_run_ids`` - the explicit cross-run
      linkage (each child's own pack also carries its ``parent_run_id``);
    - ``summary`` - aggregate integrity, status breakdown and per-control
      coverage across the root *and* every child (the same rollup the date-range
      archive produces).

    Caller resolves the epic root (a child resolves to its parent) before
    calling, mirroring ``GET /runs/{id}/epic``. A run with no children exports
    as a degenerate one-run epic: the rollup is ``empty`` and only the root pack
    is present.
    """
    # Imported here, not at module top, to keep this packaging module free of a
    # hard dependency on the lifecycle/rollup module for the single-run path.
    from foundry.epics import compute_epic_rollup

    stamp = generated_at or datetime.now(timezone.utc)
    root_pack = build_evidence_pack(
        session, root, control_mappings=control_mappings, generated_at=stamp
    )
    child_packs = [
        build_evidence_pack(
            session, child, control_mappings=control_mappings, generated_at=stamp
        )
        for child in children
    ]
    rollup = compute_epic_rollup(child.status for child in children)

    return {
        "generated_at": _iso(stamp),
        "epic": {
            "root_run_id": root.id,
            "linear_issue_key": root.linear_issue_key,
            "rollup": rollup,
            "child_run_ids": [child.id for child in children],
        },
        "run_count": 1 + len(child_packs),
        "summary": _summarise_packs([root_pack, *child_packs]),
        "root": root_pack,
        "children": child_packs,
    }


# --------------------------------------------------------------------------- HTML


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _json_block(value: Any) -> str:
    return (
        '<pre class="json">'
        + html.escape(json.dumps(value, indent=2, sort_keys=True))
        + "</pre>"
    )


def render_evidence_html(pack: dict[str, Any]) -> str:
    """Render an evidence pack as a standalone, zero-build HTML page."""
    run = pack["run"]
    integrity = pack["integrity"]
    verified = integrity["verified"]
    banner_cls = "ok" if verified else "fail"
    banner_text = (
        "INTEGRITY VERIFIED" if verified else "INTEGRITY CHECK FAILED"
    )

    rows = []
    for c in pack["control_mappings"]:
        status = "satisfied" if c["satisfied"] else "missing"
        missing = (
            ""
            if c["satisfied"]
            else f" <span class=\"miss\">missing: {_esc(', '.join(c['missing_evidence']))}</span>"
        )
        rows.append(
            "<tr>"
            f"<td>{_esc(c['framework'])}</td>"
            f"<td><code>{_esc(c['control_id'])}</code></td>"
            f"<td>{_esc(c['title'])}</td>"
            f"<td class=\"{status}\">{status}{missing}</td>"
            "</tr>"
        )
    controls_table = (
        "<table><thead><tr><th>Framework</th><th>Control</th><th>Title</th>"
        "<th>Status</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )

    approvals_html = "".join(
        f"<li><strong>{_esc(a['approver'])}</strong> "
        f"(roles: {_esc(', '.join(a['granted_roles']) or 'none')}) "
        f"&middot; {_esc(a['recorded_at'])}</li>"
        for a in pack["approvals"]
    ) or "<li><em>none recorded</em></li>"

    audit_rows = "".join(
        "<tr>"
        f"<td>#{_esc(e['sequence'])}</td>"
        f"<td><code>{_esc(e['event_type'])}</code></td>"
        f"<td>{_esc(e['actor_type'])}{('/' + _esc(e['actor_id'])) if e['actor_id'] else ''}</td>"
        f"<td>{_esc(e['created_at'])}</td>"
        "</tr>"
        for e in pack["audit_trail"]
    )

    def _section(title: str, body: str) -> str:
        return f"<section><h2>{_esc(title)}</h2>{body}</section>"

    sections = [
        _section("Run", _json_block(run)),
        _section("Controls", controls_table),
        _section(
            "Integrity",
            f'<p class="method">{_esc(integrity["method"])}</p>'
            + _json_block(
                {
                    "artifacts": integrity["artifacts"]["checked"],
                    "artifacts_ok": integrity["artifacts"]["ok"],
                    "failed_artifacts": integrity["artifacts"]["failed"],
                    "audit_sequence": integrity["audit_sequence"],
                    "audit_chain": integrity["audit_chain"],
                }
            ),
        ),
        _section("Ticket", _json_block(pack["ticket"])),
        _section("Plan", _json_block(pack["plan"])),
        _section("Risk assessment", _json_block(pack["risk_assessment"])),
        _section("Approvals", f"<ul>{approvals_html}</ul>"),
        _section("Policy decisions", _json_block(pack["policy_decisions"])),
        _section("Agent jobs", _json_block(pack["agent_jobs"])),
        _section("PR state", _json_block(pack["pr"])),
        _section(
            "Audit trail",
            "<table><thead><tr><th>Seq</th><th>Event</th><th>Actor</th>"
            f"<th>When</th></tr></thead><tbody>{audit_rows}</tbody></table>",
        ),
    ]

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Foundry evidence pack &mdash; {_esc(run['linear_issue_key'])}</title>
<style>
 body {{ font: 14px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 960px; padding: 2rem; color: #1a1a1a; }}
 h1 {{ margin: 0 0 .25rem; }}
 .sub {{ color: #666; margin: 0 0 1.5rem; }}
 .banner {{ padding: .75rem 1rem; border-radius: 6px; font-weight: 600; margin: 0 0 1.5rem; }}
 .banner.ok {{ background: #e6f5ea; color: #1b6b34; border: 1px solid #b6e0c2; }}
 .banner.fail {{ background: #fbe7e7; color: #a11; border: 1px solid #f0b5b5; }}
 section {{ margin: 0 0 1.5rem; }}
 h2 {{ font-size: 1.05rem; border-bottom: 1px solid #eee; padding-bottom: .25rem; }}
 table {{ border-collapse: collapse; width: 100%; }}
 th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #eee; vertical-align: top; }}
 th {{ color: #666; font-weight: 600; }}
 td.satisfied {{ color: #1b6b34; }}
 td.missing {{ color: #a11; }}
 .miss {{ color: #a11; font-weight: 400; }}
 pre.json {{ background: #f7f7f8; padding: .75rem; border-radius: 6px; overflow-x: auto; font-size: 12px; }}
 .method {{ color: #666; font-style: italic; }}
 code {{ background: #f0f0f2; padding: 0 .25rem; border-radius: 3px; }}
</style></head><body>
<h1>Compliance evidence pack</h1>
<p class="sub">Run <code>{_esc(run['id'])}</code> &middot; {_esc(run['linear_issue_key'])}
 &middot; status <strong>{_esc(run['status'])}</strong>
 &middot; generated {_esc(pack['generated_at'])}</p>
<div class="banner {banner_cls}">{banner_text}</div>
{''.join(sections)}
</body></html>"""


# Shared page CSS for the multi-run (archive / epic) rollup pages.
_ROLLUP_CSS = """
 body { font: 14px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 960px; padding: 2rem; color: #1a1a1a; }
 h1 { margin: 0 0 .25rem; }
 .sub { color: #666; margin: 0 0 1.5rem; }
 .banner { padding: .75rem 1rem; border-radius: 6px; font-weight: 600; margin: 0 0 1.5rem; }
 .banner.ok { background: #e6f5ea; color: #1b6b34; border: 1px solid #b6e0c2; }
 .banner.fail { background: #fbe7e7; color: #a11; border: 1px solid #f0b5b5; }
 section { margin: 0 0 1.5rem; }
 h2 { font-size: 1.05rem; border-bottom: 1px solid #eee; padding-bottom: .25rem; }
 table { border-collapse: collapse; width: 100%; }
 th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #eee; vertical-align: top; }
 th { color: #666; font-weight: 600; }
 td.satisfied { color: #1b6b34; }
 td.missing { color: #a11; }
 code { background: #f0f0f2; padding: 0 .25rem; border-radius: 3px; }
"""


def _coverage_table(control_coverage: list[dict[str, Any]]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{_esc(c['framework'])}</td>"
        f"<td><code>{_esc(c['control_id'])}</code></td>"
        f"<td>{_esc(c['title'])}</td>"
        f"<td class=\"{'satisfied' if c['fully_satisfied'] else 'missing'}\">"
        f"{_esc(c['satisfied_runs'])} / {_esc(c['total_runs'])}</td>"
        "</tr>"
        for c in control_coverage
    ) or "<tr><td colspan=\"4\"><em>no controls configured</em></td></tr>"
    return (
        "<table><thead><tr><th>Framework</th><th>Control</th><th>Title</th>"
        "<th>Runs satisfying</th></tr></thead><tbody>" + rows + "</tbody></table>"
    )


def _status_table(status_breakdown: dict[str, int]) -> str:
    rows = "".join(
        f"<tr><td><code>{_esc(status)}</code></td><td>{_esc(count)}</td></tr>"
        for status, count in sorted(status_breakdown.items())
    ) or "<tr><td colspan=\"2\"><em>none</em></td></tr>"
    return (
        "<table><thead><tr><th>Status</th><th>Runs</th></tr></thead><tbody>"
        + rows
        + "</tbody></table>"
    )


def _runs_table(packs: list[dict[str, Any]]) -> str:
    rows = []
    for pack in packs:
        run = pack["run"]
        controls = pack["control_mappings"]
        sat = sum(1 for c in controls if c["satisfied"])
        ok = pack["integrity"]["verified"]
        role = "root" if run["parent_run_id"] is None else "child"
        rows.append(
            "<tr>"
            f"<td><code>{_esc(run['id'])}</code></td>"
            f"<td>{_esc(role)}</td>"
            f"<td>{_esc(run['linear_issue_key'])}</td>"
            f"<td>{_esc(run['status'])}</td>"
            f"<td class=\"{'satisfied' if ok else 'missing'}\">"
            f"{'verified' if ok else 'FAILED'}</td>"
            f"<td>{_esc(sat)} / {_esc(len(controls))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Run</th><th>Role</th><th>Issue</th><th>Status</th>"
        "<th>Integrity</th><th>Controls</th></tr></thead><tbody>"
        + ("".join(rows) or "<tr><td colspan=\"6\"><em>no runs</em></td></tr>")
        + "</tbody></table>"
    )


def render_archive_html(archive: dict[str, Any]) -> str:
    """Render an org-wide evidence archive as a standalone, zero-build page."""
    summary = archive["summary"]
    verified = summary["verified"]
    banner_cls = "ok" if verified else "fail"
    run_count = archive["run_count"]
    failed = summary["runs_failed_integrity"]
    if not run_count:
        banner_text = "NO RUNS IN RANGE"
    elif verified:
        banner_text = f"INTEGRITY VERIFIED &middot; {run_count} run(s)"
    else:
        banner_text = (
            f"INTEGRITY CHECK FAILED &middot; {len(failed)} of {run_count} run(s)"
        )

    rng = archive["range"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Foundry evidence archive</title>
<style>{_ROLLUP_CSS}</style></head><body>
<h1>Compliance evidence archive</h1>
<p class="sub">Range <strong>{_esc(rng['from'] or 'beginning')}</strong> &rarr;
 <strong>{_esc(rng['to'] or 'now')}</strong>
 &middot; {_esc(run_count)} run(s)
 &middot; generated {_esc(archive['generated_at'])}</p>
<div class="banner {banner_cls}">{banner_text}</div>
<section><h2>Control coverage</h2>{_coverage_table(summary['control_coverage'])}</section>
<section><h2>Run statuses</h2>{_status_table(summary['status_breakdown'])}</section>
<section><h2>Runs</h2>{_runs_table(archive['runs'])}</section>
</body></html>"""


def render_epic_evidence_html(pack: dict[str, Any]) -> str:
    """Render an epic evidence export as a standalone, zero-build page (#35)."""
    epic = pack["epic"]
    summary = pack["summary"]
    verified = summary["verified"]
    banner_cls = "ok" if verified else "fail"
    run_count = pack["run_count"]
    failed = summary["runs_failed_integrity"]
    if verified:
        banner_text = f"INTEGRITY VERIFIED &middot; {run_count} run(s)"
    else:
        banner_text = (
            f"INTEGRITY CHECK FAILED &middot; {len(failed)} of {run_count} run(s)"
        )

    rollup = epic["rollup"]
    all_packs = [pack["root"], *pack["children"]]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Foundry epic evidence &mdash; {_esc(epic['linear_issue_key'])}</title>
<style>{_ROLLUP_CSS}</style></head><body>
<h1>Epic evidence pack</h1>
<p class="sub">Epic root <code>{_esc(epic['root_run_id'])}</code>
 &middot; {_esc(epic['linear_issue_key'])}
 &middot; rollup <strong>{_esc(rollup['status'])}</strong>
 &middot; {_esc(len(epic['child_run_ids']))} child run(s)
 &middot; generated {_esc(pack['generated_at'])}</p>
<div class="banner {banner_cls}">{banner_text}</div>
<section><h2>Epic rollup</h2>{_json_block(rollup)}</section>
<section><h2>Control coverage</h2>{_coverage_table(summary['control_coverage'])}</section>
<section><h2>Run statuses</h2>{_status_table(summary['status_breakdown'])}</section>
<section><h2>Runs</h2>{_runs_table(all_packs)}</section>
</body></html>"""
