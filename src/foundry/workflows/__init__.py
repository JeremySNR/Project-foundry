"""Durable execution layer (Temporal).

``decisions`` is pure and always importable. The Temporal-dependent modules
(``activities``, ``workflow``, ``worker``) require the optional ``workflow``
extra (``pip install -e ".[workflow]"``); importing this package degrades
gracefully if temporalio is absent.
"""

from __future__ import annotations

from .decisions import (
    HumanDecision,
    Phase,
    is_terminal,
    phase_after_dispatch,
    phase_after_intake,
)

__all__ = [
    "Phase",
    "HumanDecision",
    "is_terminal",
    "phase_after_intake",
    "phase_after_dispatch",
]

try:  # Temporal-backed pieces are optional.
    from .activities import FoundryActivities
    from .worker import TASK_QUEUE, run_worker
    from .workflow import TicketToPrWorkflow

    __all__ += ["FoundryActivities", "TicketToPrWorkflow", "run_worker", "TASK_QUEUE"]
except ImportError:  # pragma: no cover - exercised only without temporalio
    pass
