"""FastAPI skeleton for Foundry.

Exposes the three surfaces from the build plan:

- ``POST /webhooks/linear`` - signed intake that creates a single run per event.
- ``POST /runs/{run_id}/approval`` - authorised approval commands.
- ``GET  /runs`` and ``GET /runs/{run_id}`` - run status.

The heavy lifting (Temporal workflow, agents, connectors) is deliberately out of
scope for this foundation; intake creates a :class:`RunRecord` in an in-memory
:class:`RunStore`. Everything here is wired through ``app.state`` so a real store
and workflow dispatcher can be injected without touching the routes.
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, Response

from foundry.audit.events import new_id

from .security import (
    is_authorised_approver,
    parse_command,
    verify_signature,
)
from .store import RunRecord, RunStore

# Trigger conditions: a run starts only for an explicit opt-in, never for every
# new issue (that would create noise).
_TRIGGER_LABEL = "foundry:candidate"
_TRIGGER_STATUS = "Ready for AI Analysis"


def _get_store(request: Request) -> RunStore:
    return request.app.state.store


def _extract_event_id(payload: dict[str, Any], delivery_header: str | None) -> str | None:
    """Prefer an explicit delivery id; fall back to a payload-provided id."""
    return delivery_header or payload.get("deliveryId") or payload.get("id")


def _is_trigger(payload: dict[str, Any]) -> bool:
    """Decide whether this Linear event should start a run."""
    data = payload.get("data", {})
    labels = {label.get("name") for label in data.get("labels", []) if isinstance(label, dict)}
    if _TRIGGER_LABEL in labels:
        return True
    state = (data.get("state") or {}).get("name")
    if state == _TRIGGER_STATUS:
        return True
    # Comment commands: /foundry analyse | start
    body = data.get("body") or payload.get("body") or ""
    command = parse_command(body)
    if command and command.command in {"start"}:
        return True
    if body.strip().startswith("/foundry analyse"):
        return True
    return False


def create_app(
    *,
    webhook_secret: str,
    store: RunStore | None = None,
    authorised_approvers: set[str] | None = None,
) -> FastAPI:
    app = FastAPI(title="Project Foundry", version="0.1.0")
    app.state.store = store or RunStore()
    app.state.webhook_secret = webhook_secret
    app.state.authorised_approvers = authorised_approvers or set()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/linear", status_code=202)
    async def linear_webhook(
        request: Request,
        linear_signature: str | None = Header(default=None, alias="Linear-Signature"),
        delivery_id: str | None = Header(default=None, alias="Linear-Delivery"),
        store: RunStore = Depends(_get_store),
    ) -> dict[str, Any]:
        raw = await request.body()
        if not verify_signature(app.state.webhook_secret, raw, linear_signature):
            # Reject unauthorised webhooks; no workflow starts.
            raise HTTPException(status_code=401, detail="invalid webhook signature")

        payload = await request.json()
        event_id = _extract_event_id(payload, delivery_id)
        if event_id and store.has_processed_event(event_id):
            existing = _existing_run_summary(store, payload)
            return {"status": "duplicate", "run": existing}

        if not _is_trigger(payload):
            if event_id:
                store.mark_event_processed(event_id)
            return {"status": "ignored", "reason": "no trigger condition matched"}

        data = payload.get("data", {})
        issue_id = str(data.get("issueId") or data.get("id") or "")
        issue_key = str(data.get("identifier") or data.get("issueKey") or "")
        if not issue_id:
            raise HTTPException(status_code=400, detail="missing issue id in payload")

        # Deduplicate by issue too: one in-flight run per issue.
        existing = store.get_run_by_issue(issue_id)
        if existing is not None:
            if event_id:
                store.mark_event_processed(event_id)
            return {"status": "exists", "run": existing.model_dump(mode="json")}

        record = RunRecord(
            id=new_id("run"),
            linear_issue_id=issue_id,
            linear_issue_key=issue_key,
            trigger_type=_trigger_type(payload),
            created_by=(data.get("actor") or {}).get("name"),
        )
        store.create_run(record)
        if event_id:
            store.mark_event_processed(event_id)
        return {"status": "started", "run": record.model_dump(mode="json")}

    @app.get("/runs")
    def list_runs(store: RunStore = Depends(_get_store)) -> dict[str, Any]:
        return {"runs": [r.model_dump(mode="json") for r in store.list_runs()]}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, store: RunStore = Depends(_get_store)) -> dict[str, Any]:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run.model_dump(mode="json")

    @app.post("/runs/{run_id}/approval")
    def approval(
        run_id: str,
        body: dict[str, Any] = Body(...),
        store: RunStore = Depends(_get_store),
    ) -> dict[str, Any]:
        user = body.get("user")
        text = body.get("text") or body.get("command") or ""
        if not user:
            raise HTTPException(status_code=400, detail="missing user")

        command = parse_command(text) or parse_command(f"/foundry {text}")
        if command is None:
            raise HTTPException(status_code=400, detail="unrecognised command")

        if not is_authorised_approver(user, app.state.authorised_approvers):
            raise HTTPException(status_code=403, detail="user not authorised to approve")

        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")

        updated = store.apply_approval(run_id, command=command.command, user=user)
        return {
            "status": "applied",
            "command": command.command,
            "run": updated.model_dump(mode="json") if updated else None,
        }

    return app


def _trigger_type(payload: dict[str, Any]) -> str:
    data = payload.get("data", {})
    body = data.get("body") or payload.get("body") or ""
    if parse_command(body) or body.strip().startswith("/foundry"):
        return "comment_command"
    state = (data.get("state") or {}).get("name")
    if state == _TRIGGER_STATUS:
        return "status"
    return "label"


def _existing_run_summary(store: RunStore, payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data", {})
    issue_id = str(data.get("issueId") or data.get("id") or "")
    run = store.get_run_by_issue(issue_id) if issue_id else None
    return run.model_dump(mode="json") if run else None
