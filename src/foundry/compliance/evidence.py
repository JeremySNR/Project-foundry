"""Assemble, verify, and render a run's compliance evidence pack.

``build_evidence_pack`` reads a single run's full chain out of the DB and
returns a JSON-serialisable dict. ``verify_integrity`` recomputes what can be
recomputed - every artifact's content hash, and the contiguity of the
append-only audit sequence - so an auditor can confirm the export wasn't
tampered with between storage and export. ``render_evidence_html`` produces a
zero-build standalone page from that dict.

Honest about what the verification *is*: artifacts are content-addressed
(``sha256(content_json)``), so we recompute and compare those, and we check the
per-run audit ``sequence`` is gap-free and strictly increasing. It is not a
blockchain-style linked hash chain across rows - we say so rather than oversell
it (see issue #24 on provenance claims).
"""

from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

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

    return {
        "verified": artifacts_ok and sequence_ok,
        "method": (
            "Recomputed sha256(content_json) for each artifact and compared to "
            "the stored content hash; checked the per-run audit sequence is "
            "exactly 0..N-1 (gap-free, unique, strictly increasing). Not a "
            "cross-row linked hash chain."
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
