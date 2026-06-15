"""Compliance evidence packs.

The audit trail already exists (content-hashed artifacts, append-only events);
this package is *packaging*. It assembles a single run's full chain - ticket,
plan, approvals with identities, policy decisions, diff-risk checks, agent jobs,
PR - into one export, verifies its integrity, and maps the sections onto named
compliance controls (SOC 2 / ISO 27001 / EU AI Act). ``build_evidence_archive``
rolls every run in a date range into one org-wide export with a coverage rollup.
Reading only; it never writes to the trail.
"""

from .controls import (
    DEFAULT_CONTROL_MAPPINGS,
    KNOWN_EVIDENCE_SECTIONS,
    ControlMapping,
)
from .evidence import (
    build_epic_evidence_pack,
    build_evidence_archive,
    build_evidence_pack,
    render_archive_html,
    render_epic_evidence_html,
    render_evidence_html,
    verify_integrity,
)
from .pdf import (
    PdfRenderingUnavailable,
    render_archive_pdf,
    render_epic_evidence_pdf,
    render_evidence_pdf,
)

__all__ = [
    "ControlMapping",
    "DEFAULT_CONTROL_MAPPINGS",
    "KNOWN_EVIDENCE_SECTIONS",
    "PdfRenderingUnavailable",
    "build_epic_evidence_pack",
    "build_evidence_archive",
    "build_evidence_pack",
    "render_archive_html",
    "render_archive_pdf",
    "render_epic_evidence_html",
    "render_epic_evidence_pdf",
    "render_evidence_html",
    "render_evidence_pdf",
    "verify_integrity",
]
