"""Render compliance evidence packs as PDF (issue #36).

PDF is the auditor-facing *rendered* format alongside HTML. Every renderer here
builds from the **same** evidence-pack dicts the HTML renderers in
``evidence.py`` consume - ``build_evidence_pack`` / ``build_evidence_archive`` /
``build_epic_evidence_pack`` - so a run's PDF, HTML and JSON exports agree by
construction (there is no second data path to drift). This is packaging only: it
reads nothing the JSON/HTML packs don't already carry and touches no gate,
policy, audit write, or schema.

``fpdf2`` is an optional, lazily-imported dependency (the ``[pdf]`` extra), kept
off the core/offline path exactly like ``[oidc]`` (pyjwt) and ``[crypto]``
(cryptography): the JSON and HTML exports - and the offline core test suite -
never import it. A PDF export requested without the extra installed fails loud
with an install hint (:class:`PdfRenderingUnavailable`) rather than silently
degrading or raising an opaque ``ModuleNotFoundError``.

The library uses only the built-in core fonts (Helvetica), so no font files,
binaries, or network access are required - the whole path runs offline. Text is
sanitised to the core fonts' latin-1 range (with replacement) so an arbitrary
ticket title can never crash the renderer.
"""

from __future__ import annotations

from typing import Any

_FONT = "Helvetica"


class PdfRenderingUnavailable(RuntimeError):
    """Raised when a PDF export is requested but the ``[pdf]`` extra is absent."""


def _load_fpdf() -> Any:
    """Import ``fpdf2`` lazily, raising a clear, actionable error if it's absent.

    Keeping the import here (not at module top) means importing this module - and
    so the JSON/HTML export paths and the offline core suite - never needs the
    optional dependency.
    """
    try:
        from fpdf import FPDF  # type: ignore import-not-found
    except ImportError as exc:
        raise PdfRenderingUnavailable(
            "PDF rendering requires the optional 'pdf' extra. Install it with: "
            "pip install 'project-foundry[pdf]'"
        ) from exc
    return FPDF


def _safe(value: Any) -> str:
    """Coerce ``value`` to a string the built-in core fonts can render.

    The core PDF fonts are latin-1 only; arbitrary ticket text may contain other
    code points. Encoding with ``errors="replace"`` keeps the renderer robust
    (a stray emoji becomes ``?`` rather than raising) without pulling in a
    Unicode font file.
    """
    if value is None:
        return ""
    return str(value).encode("latin-1", "replace").decode("latin-1")


# --------------------------------------------------------------------------- doc


def _new_doc(title: str) -> Any:
    FPDF = _load_fpdf()
    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_title(_safe(title))
    pdf.set_author("Project Foundry")
    pdf.add_page()
    return pdf


def _h1(pdf: Any, text: str) -> None:
    pdf.set_text_color(0)
    pdf.set_font(_FONT, "B", 18)
    pdf.multi_cell(pdf.epw, 9, _safe(text), new_x="LMARGIN", new_y="NEXT")


def _sub(pdf: Any, text: str) -> None:
    pdf.set_font(_FONT, "", 9)
    pdf.set_text_color(110)
    pdf.multi_cell(pdf.epw, 5, _safe(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0)
    pdf.ln(2)


def _banner(pdf: Any, text: str, ok: bool) -> None:
    pdf.set_font(_FONT, "B", 11)
    if ok:
        pdf.set_fill_color(230, 245, 234)
        pdf.set_text_color(27, 107, 52)
    else:
        pdf.set_fill_color(251, 231, 231)
        pdf.set_text_color(170, 17, 17)
    pdf.multi_cell(
        pdf.epw, 8, _safe(text), new_x="LMARGIN", new_y="NEXT", fill=True, align="C"
    )
    pdf.set_text_color(0)
    pdf.ln(3)


def _h2(pdf: Any, text: str) -> None:
    pdf.ln(1)
    pdf.set_font(_FONT, "B", 12)
    pdf.set_text_color(0)
    pdf.multi_cell(pdf.epw, 7, _safe(text), new_x="LMARGIN", new_y="NEXT")
    y = pdf.get_y()
    pdf.set_draw_color(220)
    pdf.line(pdf.l_margin, y, pdf.l_margin + pdf.epw, y)
    pdf.ln(1.5)


def _para(pdf: Any, text: str, *, italic: bool = False) -> None:
    pdf.set_font(_FONT, "I" if italic else "", 9)
    pdf.set_text_color(90 if italic else 0)
    pdf.multi_cell(pdf.epw, 5, _safe(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0)


def _kv(pdf: Any, label: str, value: Any) -> None:
    """One ``label: value`` line, the label in bold."""
    pdf.set_text_color(0)
    pdf.set_font(_FONT, "B", 9)
    pdf.cell(45, 5, _safe(label))
    pdf.set_font(_FONT, "", 9)
    pdf.multi_cell(pdf.epw - 45, 5, _safe(value), new_x="LMARGIN", new_y="NEXT")


def _table(
    pdf: Any,
    headings: list[str],
    rows: list[list[Any]],
    col_widths: tuple[int, ...],
    *,
    empty_note: str = "none",
) -> None:
    if not rows:
        _para(pdf, empty_note, italic=True)
        return
    pdf.set_font(_FONT, "", 8)
    pdf.set_text_color(0)
    with pdf.table(col_widths=col_widths, line_height=5) as table:
        head = table.row()
        for h in headings:
            head.cell(_safe(h))
        for r in rows:
            row = table.row()
            for cell in r:
                row.cell(_safe(cell))
    pdf.ln(2)


def _bytes(pdf: Any) -> bytes:
    # ``output()`` returns a bytearray on modern fpdf2; normalise to ``bytes``.
    return bytes(pdf.output())


# ----------------------------------------------------------------------- per-run


def _actor(event: dict[str, Any]) -> str:
    actor = _safe(event.get("actor_type"))
    if event.get("actor_id"):
        actor = f"{actor}/{_safe(event['actor_id'])}"
    return actor


def render_evidence_pdf(pack: dict[str, Any]) -> bytes:
    """Render a single run's evidence pack as a PDF document (bytes)."""
    run = pack["run"]
    integrity = pack["integrity"]
    verified = integrity["verified"]

    pdf = _new_doc(f"Foundry evidence pack - {run.get('linear_issue_key')}")
    _h1(pdf, "Compliance evidence pack")
    _sub(
        pdf,
        f"Run {run['id']} - {run.get('linear_issue_key')} - "
        f"status {run['status']} - generated {pack['generated_at']}",
    )
    _banner(
        pdf,
        "INTEGRITY VERIFIED" if verified else "INTEGRITY CHECK FAILED",
        verified,
    )

    _h2(pdf, "Run")
    _kv(pdf, "Run id", run["id"])
    _kv(pdf, "Issue", run.get("linear_issue_key"))
    _kv(pdf, "Status", run["status"])
    _kv(pdf, "Risk level", run.get("risk_level"))
    _kv(pdf, "Approved by", run.get("approved_by"))
    _kv(pdf, "Created at", run.get("created_at"))

    _h2(pdf, "Controls")
    control_rows = [
        [
            c["framework"],
            c["control_id"],
            c["title"],
            "satisfied"
            if c["satisfied"]
            else "missing: " + ", ".join(c["missing_evidence"]),
        ]
        for c in pack["control_mappings"]
    ]
    _table(
        pdf,
        ["Framework", "Control", "Title", "Status"],
        control_rows,
        (22, 22, 40, 26),
        empty_note="no controls configured",
    )

    _h2(pdf, "Integrity")
    art = integrity["artifacts"]
    seq = integrity["audit_sequence"]
    chain = integrity["audit_chain"]
    _kv(pdf, "Artifacts checked", f"{art['checked']} (ok={art['ok']})")
    if art["failed"]:
        _kv(pdf, "Failed artifacts", ", ".join(art["failed"]))
    _kv(
        pdf,
        "Audit sequence",
        f"ok={seq['ok']} count={seq['count']} contiguous={seq['contiguous']}",
    )
    _kv(
        pdf,
        "Audit hash chain",
        f"ok={chain['ok']} present={chain['present']} checked={chain['checked']}"
        + (f" broken_at={chain['broken_at']}" if chain["broken_at"] else ""),
    )
    _para(pdf, integrity["method"], italic=True)

    _h2(pdf, "Approvals")
    approval_rows = [
        [
            a["approver"],
            ", ".join(a["granted_roles"]) or "none",
            a["recorded_at"],
        ]
        for a in pack["approvals"]
    ]
    _table(
        pdf,
        ["Approver", "Granted roles", "Recorded at"],
        approval_rows,
        (35, 30, 40),
        empty_note="none recorded",
    )

    _h2(pdf, "Policy decisions")
    decision_rows = [
        [d["policy_name"], "allow" if d["allowed"] else "deny", d["reason"]]
        for d in pack["policy_decisions"]
    ]
    _table(
        pdf,
        ["Policy", "Decision", "Reason"],
        decision_rows,
        (28, 18, 50),
        empty_note="none recorded",
    )

    _h2(pdf, "Agent jobs")
    job_rows = [
        [j["provider"], j["status"], j.get("repo"), j.get("pr_url")]
        for j in pack["agent_jobs"]
    ]
    _table(
        pdf,
        ["Provider", "Status", "Repo", "PR"],
        job_rows,
        (22, 20, 26, 40),
        empty_note="none",
    )

    _h2(pdf, "Audit trail")
    audit_rows = [
        [f"#{e['sequence']}", e["event_type"], _actor(e), e["created_at"]]
        for e in pack["audit_trail"]
    ]
    _table(
        pdf,
        ["Seq", "Event", "Actor", "When"],
        audit_rows,
        (12, 40, 30, 36),
        empty_note="no audit events",
    )

    return _bytes(pdf)


# ----------------------------------------------------------------- multi-run


def _summary_sections(pdf: Any, summary: dict[str, Any]) -> None:
    _h2(pdf, "Control coverage")
    coverage_rows = [
        [
            c["framework"],
            c["control_id"],
            c["title"],
            f"{c['satisfied_runs']} / {c['total_runs']}",
        ]
        for c in summary["control_coverage"]
    ]
    _table(
        pdf,
        ["Framework", "Control", "Title", "Runs satisfying"],
        coverage_rows,
        (22, 22, 40, 26),
        empty_note="no controls configured",
    )

    _h2(pdf, "Run statuses")
    status_rows = [
        [status, count]
        for status, count in sorted(summary["status_breakdown"].items())
    ]
    _table(pdf, ["Status", "Runs"], status_rows, (40, 20), empty_note="none")


def _runs_table(pdf: Any, packs: list[dict[str, Any]]) -> None:
    _h2(pdf, "Runs")
    rows = []
    for pack in packs:
        run = pack["run"]
        controls = pack["control_mappings"]
        sat = sum(1 for c in controls if c["satisfied"])
        ok = pack["integrity"]["verified"]
        role = "root" if run["parent_run_id"] is None else "child"
        rows.append(
            [
                run["id"],
                role,
                run.get("linear_issue_key"),
                run["status"],
                "verified" if ok else "FAILED",
                f"{sat} / {len(controls)}",
            ]
        )
    _table(
        pdf,
        ["Run", "Role", "Issue", "Status", "Integrity", "Controls"],
        rows,
        (28, 14, 26, 24, 22, 20),
        empty_note="no runs",
    )


def render_archive_pdf(archive: dict[str, Any]) -> bytes:
    """Render an org-wide evidence archive as a PDF document (bytes)."""
    summary = archive["summary"]
    verified = summary["verified"]
    run_count = archive["run_count"]
    failed = summary["runs_failed_integrity"]
    rng = archive["range"]

    pdf = _new_doc("Foundry evidence archive")
    _h1(pdf, "Compliance evidence archive")
    _sub(
        pdf,
        f"Range {rng['from'] or 'beginning'} -> {rng['to'] or 'now'} - "
        f"{run_count} run(s) - generated {archive['generated_at']}",
    )
    if not run_count:
        _banner(pdf, "NO RUNS IN RANGE", False)
    elif verified:
        _banner(pdf, f"INTEGRITY VERIFIED - {run_count} run(s)", True)
    else:
        _banner(
            pdf,
            f"INTEGRITY CHECK FAILED - {len(failed)} of {run_count} run(s)",
            False,
        )

    _summary_sections(pdf, summary)
    _runs_table(pdf, archive["runs"])
    return _bytes(pdf)


def render_epic_evidence_pdf(pack: dict[str, Any]) -> bytes:
    """Render an epic's cross-run evidence export as a PDF document (bytes)."""
    epic = pack["epic"]
    summary = pack["summary"]
    verified = summary["verified"]
    run_count = pack["run_count"]
    failed = summary["runs_failed_integrity"]
    rollup = epic["rollup"]

    pdf = _new_doc(f"Foundry epic evidence - {epic.get('linear_issue_key')}")
    _h1(pdf, "Epic evidence pack")
    _sub(
        pdf,
        f"Epic root {epic['root_run_id']} - {epic.get('linear_issue_key')} - "
        f"rollup {rollup['status']} - {len(epic['child_run_ids'])} child run(s) - "
        f"generated {pack['generated_at']}",
    )
    if verified:
        _banner(pdf, f"INTEGRITY VERIFIED - {run_count} run(s)", True)
    else:
        _banner(
            pdf,
            f"INTEGRITY CHECK FAILED - {len(failed)} of {run_count} run(s)",
            False,
        )

    _h2(pdf, "Epic rollup")
    _kv(pdf, "Status", rollup["status"])
    for bucket, count in rollup.get("counts", {}).items():
        _kv(pdf, bucket, count)

    _summary_sections(pdf, summary)
    _runs_table(pdf, [pack["root"], *pack["children"]])
    return _bytes(pdf)
