"""Control mappings - which evidence sections satisfy which compliance control.

This is **config, not code**: the defaults below are overridable wholesale via
``compliance.control_mappings`` in ``foundry.yaml`` (parsed in ``config.py``).
A control names the evidence *sections* of an evidence pack that, taken
together, demonstrate the control; a run satisfies the control when every named
section is present in that run's pack. The section vocabulary is fixed
(``KNOWN_EVIDENCE_SECTIONS``) so a config typo fails loud at load time rather
than silently mapping a control onto evidence that can never exist.
"""

from __future__ import annotations

from dataclasses import dataclass

# The evidence sections an evidence pack can contain. A control may only
# reference these; config that names anything else is rejected in
# ``Settings._validate`` so mappings can never drift from what the packer emits.
KNOWN_EVIDENCE_SECTIONS: frozenset[str] = frozenset(
    {
        "ticket",            # the original change request (ticket snapshot)
        "analysis",          # readiness / acceptance-criteria analysis
        "context",           # repo routing / context bundle
        "plan",              # the delivery plan that was approved
        "risk_assessment",   # classified risk + required approvals
        "approvals",         # human sign-offs, with identities and granted roles
        "policy_decisions",  # every policy-gate decision
        "agent_jobs",        # dispatched coding-agent jobs
        "pr",                # the resulting pull/merge request state
        "final_summary",     # terminal run summary
        "audit_trail",       # the full append-only event log
        "integrity",         # hash/sequence verification result
    }
)


@dataclass(frozen=True)
class ControlMapping:
    """One compliance control and the evidence sections that demonstrate it.

    Frozen + tuple-typed so it stays hashable, matching the rest of
    ``Settings`` (which is a frozen dataclass).
    """

    framework: str
    control_id: str
    title: str
    evidence: tuple[str, ...]
    description: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "framework": self.framework,
            "control_id": self.control_id,
            "title": self.title,
            "description": self.description,
            "evidence": list(self.evidence),
        }


# Sensible defaults covering the three frameworks named in the roadmap. These
# are a starting point, not gospel - a deployment maps controls to its own
# audit scope via YAML. Section names are validated against
# KNOWN_EVIDENCE_SECTIONS at config-load time.
DEFAULT_CONTROL_MAPPINGS: tuple[ControlMapping, ...] = (
    ControlMapping(
        framework="SOC 2",
        control_id="CC8.1",
        title="Change management",
        description=(
            "The entity authorizes, designs, develops, configures, documents, "
            "tests, approves, and implements changes. Foundry evidences the "
            "request, the plan, the human authorization, the policy gate, and "
            "the implemented PR."
        ),
        evidence=(
            "ticket",
            "plan",
            "risk_assessment",
            "approvals",
            "policy_decisions",
            "pr",
            "audit_trail",
        ),
    ),
    ControlMapping(
        framework="ISO/IEC 27001:2022",
        control_id="A.8.32",
        title="Change management",
        description=(
            "Changes to information processing facilities and systems are "
            "subject to change-management procedures. Evidenced by the recorded "
            "request, plan, authorization, and the append-only trail."
        ),
        evidence=(
            "ticket",
            "plan",
            "approvals",
            "policy_decisions",
            "audit_trail",
        ),
    ),
    ControlMapping(
        framework="EU AI Act",
        control_id="Article 14",
        title="Human oversight",
        description=(
            "High-risk AI systems are designed so they can be effectively "
            "overseen by humans. Foundry evidences that the AI agent's work was "
            "risk-assessed, gated, and approved by an identified human before "
            "any autonomous action."
        ),
        evidence=(
            "risk_assessment",
            "approvals",
            "policy_decisions",
        ),
    ),
)


def mappings_from_config(raw: object) -> tuple[ControlMapping, ...]:
    """Build control mappings from parsed YAML (a list of mapping dicts).

    Kept here, next to the schema it produces, so ``config.py`` only has to call
    one function. Validation of section names is deferred to ``Settings`` so the
    error surfaces alongside every other config error.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("compliance.control_mappings must be a list")
    out: list[ControlMapping] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("each compliance.control_mappings entry must be a mapping")
        try:
            out.append(
                ControlMapping(
                    framework=str(entry["framework"]),
                    control_id=str(entry["control_id"]),
                    title=str(entry.get("title", "")),
                    evidence=tuple(str(e) for e in (entry.get("evidence") or [])),
                    description=str(entry.get("description", "")),
                )
            )
        except KeyError as exc:
            raise ValueError(
                f"compliance.control_mappings entry missing required key: {exc}"
            ) from exc
    return tuple(out)
