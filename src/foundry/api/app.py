"""FastAPI app for Foundry - now wired to the orchestrator and the data model.

Surfaces:

- ``POST /webhooks/linear`` - signed, idempotent intake that runs
  ``FoundryOrchestrator.intake_and_plan`` and persists a real run.
- ``POST /runs/{run_id}/approval`` - authorised ``/foundry approve|reject|stop``
  commands that drive the orchestrator (approve also attempts agent dispatch).
- ``GET /runs`` and ``GET /runs/{run_id}`` - run status read from the DB.

Everything is injected through ``app.state`` so a Postgres session factory, a
real coding-agent provider, LLM-backed engines, or a Linear connector can be
swapped in without touching the routes.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Body, FastAPI, Header, HTTPException, Request

from foundry.db.base import create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRun
from foundry.orchestrator import FoundryOrchestrator, OrchestratorError
from foundry.schemas.common import ApprovalRole
from foundry.schemas.ticket import RawTicket

from .mapping import linear_payload_to_ticket
from .security import is_authorised_approver, parse_command, verify_signature

# Trigger conditions: a run starts only on an explicit opt-in, never for every
# new issue (that would create noise).
_TRIGGER_LABEL = "foundry:candidate"
_TRIGGER_STATUS = "Ready for AI Analysis"

TicketMapper = Callable[[dict[str, Any]], RawTicket]


def _run_to_dict(run: FoundryRun) -> dict[str, Any]:
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
        "created_by": run.created_by,
    }


def _extract_event_id(payload: dict[str, Any], delivery_header: str | None) -> str | None:
    return delivery_header or payload.get("deliveryId") or payload.get("id")


def _is_trigger(payload: dict[str, Any]) -> bool:
    data = payload.get("data", {}) or {}
    labels = {
        lab.get("name") for lab in data.get("labels", []) if isinstance(lab, dict)
    }
    if _TRIGGER_LABEL in labels:
        return True
    if (data.get("state") or {}).get("name") == _TRIGGER_STATUS:
        return True
    body = data.get("body") or payload.get("body") or ""
    command = parse_command(body)
    if command and command.command == "start":
        return True
    return body.strip().startswith("/foundry analyse")


def _trigger_type(payload: dict[str, Any]) -> str:
    data = payload.get("data", {}) or {}
    body = data.get("body") or payload.get("body") or ""
    if body.strip().startswith("/foundry"):
        return "comment_command"
    if (data.get("state") or {}).get("name") == _TRIGGER_STATUS:
        return "status"
    return "label"


def create_app(
    *,
    webhook_secret: str,
    session_factory=None,
    orchestrator: FoundryOrchestrator | None = None,
    authorised_approvers: set[str] | None = None,
    ticket_mapper: TicketMapper | None = None,
) -> FastAPI:
    if session_factory is None:
        engine = make_engine()
        create_all(engine)
        session_factory = make_session_factory(engine)

    app = FastAPI(title="Project Foundry", version="0.1.0")
    app.state.orchestrator = orchestrator or FoundryOrchestrator(session_factory)
    app.state.webhook_secret = webhook_secret
    app.state.authorised_approvers = authorised_approvers or set()
    app.state.ticket_mapper = ticket_mapper or linear_payload_to_ticket
    # In-memory fast-path dedup; the durable guarantee is one run per issue (DB).
    app.state.processed_events = set()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/linear", status_code=202)
    async def linear_webhook(
        request: Request,
        linear_signature: str | None = Header(default=None, alias="Linear-Signature"),
        delivery_id: str | None = Header(default=None, alias="Linear-Delivery"),
    ) -> dict[str, Any]:
        raw = await request.body()
        if not verify_signature(app.state.webhook_secret, raw, linear_signature):
            # Reject unauthorised webhooks; no workflow starts.
            raise HTTPException(status_code=401, detail="invalid webhook signature")

        payload = await request.json()
        orch: FoundryOrchestrator = app.state.orchestrator
        event_id = _extract_event_id(payload, delivery_id)

        ticket = app.state.ticket_mapper(payload)

        if event_id and event_id in app.state.processed_events:
            return {"status": "duplicate", "run": _existing_run(orch, ticket.issue_id)}

        if not _is_trigger(payload):
            if event_id:
                app.state.processed_events.add(event_id)
            return {"status": "ignored", "reason": "no trigger condition matched"}

        if not ticket.issue_id:
            raise HTTPException(status_code=400, detail="missing issue id in payload")

        # Durable, idempotent guarantee: one run per Linear issue.
        existing_id = orch.find_run_id_for_issue(ticket.issue_id)
        if existing_id is not None:
            if event_id:
                app.state.processed_events.add(event_id)
            return {"status": "exists", "run": _run_to_dict(orch.get_run(existing_id))}

        data = payload.get("data", {}) or {}
        run_id = orch.intake_and_plan(
            ticket,
            trigger_type=_trigger_type(payload),
            created_by=(data.get("actor") or {}).get("name"),
        )
        if event_id:
            app.state.processed_events.add(event_id)
        return {"status": "started", "run": _run_to_dict(orch.get_run(run_id))}

    @app.get("/runs")
    def list_runs() -> dict[str, Any]:
        orch: FoundryOrchestrator = app.state.orchestrator
        return {"runs": [_run_to_dict(r) for r in orch.list_runs()]}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        orch: FoundryOrchestrator = app.state.orchestrator
        run = orch.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return _run_to_dict(run)

    @app.post("/runs/{run_id}/approval")
    def approval(run_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        orch: FoundryOrchestrator = app.state.orchestrator
        user = body.get("user")
        text = body.get("text") or body.get("command") or ""
        if not user:
            raise HTTPException(status_code=400, detail="missing user")

        command = parse_command(text) or parse_command(f"/foundry {text}")
        if command is None:
            raise HTTPException(status_code=400, detail="unrecognised command")

        if not is_authorised_approver(user, app.state.authorised_approvers):
            raise HTTPException(status_code=403, detail="user not authorised to approve")

        if orch.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")

        result: dict[str, Any] = {"command": command.command}
        try:
            if command.command == "approve":
                roles = {ApprovalRole(r) for r in body.get("roles", [])}
                orch.approve(run_id, user=user, granted_roles=roles)
                # Approval immediately attempts dispatch; the policy gate may still
                # keep the work human-only (e.g. auth changes) -> run is blocked.
                try:
                    orch.dispatch_agent(run_id)
                    result["dispatched"] = True
                except OrchestratorError as exc:
                    result["dispatched"] = False
                    result["dispatch_detail"] = str(exc)
            elif command.command == "reject":
                orch.reject(run_id, user=user)
            elif command.command == "stop":
                orch.stop(run_id, user=user)
            else:
                raise HTTPException(
                    status_code=422,
                    detail=f"command '{command.command}' is not supported yet",
                )
        except OrchestratorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        result["status"] = "applied"
        result["run"] = _run_to_dict(orch.get_run(run_id))
        return result

    return app


def _existing_run(orch: FoundryOrchestrator, issue_id: str) -> dict[str, Any] | None:
    if not issue_id:
        return None
    run_id = orch.find_run_id_for_issue(issue_id)
    return _run_to_dict(orch.get_run(run_id)) if run_id else None
