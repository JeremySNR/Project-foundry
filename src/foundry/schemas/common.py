"""Shared enumerations used across Foundry artifact schemas.

These mirror the vocabulary in the Ticket-to-PR build plan. Using string enums
keeps the JSON representation human-readable and stable for golden tests.
"""

from __future__ import annotations

from enum import Enum


class WorkType(str, Enum):
    FEATURE = "feature"
    BUG = "bug"
    TECH_DEBT = "tech_debt"
    INCIDENT = "incident"
    QUESTION = "question"
    UNKNOWN = "unknown"


class ImplementationReadiness(str, Enum):
    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    NOT_SUITABLE = "not_suitable"


class OverallRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


class AgentMode(str, Enum):
    """How much autonomy an agent is permitted for a given run."""

    ANALYSIS_ONLY = "analysis_only"
    DRAFT_PR = "draft_pr"
    HUMAN_ONLY = "human_only"


class ApprovalRole(str, Enum):
    PRODUCT = "product"
    ENGINEERING = "engineering"
    SECURITY = "security"
    QA = "qa"


class RunStatus(str, Enum):
    """Foundry run lifecycle. Maps to the suggested Linear states."""

    ANALYSING = "analysing"
    NEEDS_CLARIFICATION = "needs_clarification"
    PLAN_READY = "plan_ready"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    AGENT_RUNNING = "agent_running"
    PR_OPEN = "pr_open"
    REVIEW_REQUIRED = "review_required"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    EXECUTION_FAILED = "execution_failed"
    REJECTED = "rejected"


class PRStatus(str, Enum):
    DRAFT = "draft"
    OPEN = "open"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    MERGED = "merged"
    CLOSED = "closed"


class CIStatus(str, Enum):
    PENDING = "pending"
    PASSING = "passing"
    FAILING = "failing"
    UNKNOWN = "unknown"


class ReviewStatus(str, Enum):
    NONE = "none"
    BOT_REVIEWED = "bot_reviewed"
    HUMAN_REVIEWED = "human_reviewed"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"


class AgentJobStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PolicyAction(str, Enum):
    """Risky actions that must pass through the policy gate."""

    ANALYSE_TICKET = "analyse_ticket"
    CREATE_PLAN = "create_plan"
    REQUEST_APPROVAL = "request_approval"
    START_AGENT = "start_agent"
    CREATE_BRANCH = "create_branch"
    OPEN_PR = "open_pr"
    REQUEST_CHANGES = "request_changes"
    RETRY_AGENT = "retry_agent"
    MARK_COMPLETE = "mark_complete"
    # Modelled explicitly so "never autonomous" is an enforced decision with an
    # audit row, not an absence of code. Both are denied unconditionally in
    # this version regardless of risk level or approvals.
    AUTO_MERGE = "auto_merge"
    PRODUCTION_DEPLOY = "production_deploy"


# Run states that are still in flight. A ticket with a run in one of these
# states cannot start another run; anything else (clarification, rejection,
# blocked, failed, complete) is restartable by a fresh trigger.
ACTIVE_RUN_STATUSES = frozenset(
    {
        RunStatus.ANALYSING,
        RunStatus.PLAN_READY,
        RunStatus.WAITING_APPROVAL,
        RunStatus.APPROVED,
        RunStatus.AGENT_RUNNING,
        RunStatus.PR_OPEN,
        RunStatus.REVIEW_REQUIRED,
    }
)

# Run states that are finished. A run in one of these states never re-enters an
# active state (a fresh trigger starts a *new* run), which is what makes a
# single recorded outcome per run sound.
TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.NEEDS_CLARIFICATION,
        RunStatus.COMPLETE,
        RunStatus.BLOCKED,
        RunStatus.EXECUTION_FAILED,
        RunStatus.REJECTED,
    }
)

# Confidence threshold (0-100) below which a repository match cannot be trusted
# to start autonomous work. Sourced from the build plan's minimum policy rules.
REPO_CONFIDENCE_THRESHOLD = 70

# Areas considered sensitive enough to require explicit approval / restrict mode.
SENSITIVE_AREA_KEYS = (
    "auth",
    "payments",
    "customer_data",
    "pii",
    "database_migration",
    "infrastructure",
    "production_deploy",
)
