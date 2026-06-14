"""Project Foundry - the engineering control plane.

Module 1: Ticket-to-PR. Turns a well-formed Linear ticket into a structured,
governed, AI-assisted pull request.

This package contains the *testable core foundation*:

- ``foundry.schemas``  - Pydantic contracts for every artifact in a run.
- ``foundry.db``       - SQLAlchemy data model (runs, artifacts, audit, policy).
- ``foundry.policy``   - Risk/permission decisions (pure-Python evaluator + Rego).
- ``foundry.agents``   - The ``CodingAgentProvider`` abstraction + adapters.
- ``foundry.api``      - FastAPI skeleton (webhooks, approvals, run status).
- ``foundry.audit``    - Structured audit event helpers.
"""

__version__ = "1.4.0"
