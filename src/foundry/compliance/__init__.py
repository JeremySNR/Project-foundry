"""Compliance evidence packs.

The audit trail already exists (content-hashed artifacts, append-only events);
this package is *packaging*. It assembles a single run's full chain - ticket,
plan, approvals with identities, policy decisions, diff-risk checks, agent jobs,
PR - into one export, verifies its integrity, and maps the sections onto named
compliance controls (SOC 2 / ISO 27001 / EU AI Act). Reading only; it never
writes to the trail.
"""

from .controls import (
    DEFAULT_CONTROL_MAPPINGS,
    KNOWN_EVIDENCE_SECTIONS,
    ControlMapping,
)
from .evidence import build_evidence_pack, render_evidence_html, verify_integrity

__all__ = [
    "ControlMapping",
    "DEFAULT_CONTROL_MAPPINGS",
    "KNOWN_EVIDENCE_SECTIONS",
    "build_evidence_pack",
    "render_evidence_html",
    "verify_integrity",
]
