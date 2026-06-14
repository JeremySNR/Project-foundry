"""FastAPI app for Foundry - now wired to the orchestrator and the data model.

Surfaces:

- ``POST /webhooks/linear`` - signed, idempotent intake that runs
  ``FoundryOrchestrator.intake_and_plan``, plus the *primary* approval surface:
  ``/foundry approve|reject|stop`` comments arrive here already authenticated
  by the webhook signature, with the actor taken from the Linear payload.
- ``POST /webhooks/slack`` - the chat approval surface: a Slack interactivity
  request (approve/reject/stop button) verified against Slack's v0 request
  signature with replay-age protection, then driven through the same policy-gated
  decision path. Fail-closed (no ``FOUNDRY_SLACK_SIGNING_SECRET`` => disabled);
  the actor is the Slack-signed ``user.id`` and roles come from config.
- ``POST /runs/{run_id}/approval`` - the API approval surface. Requires a
  bearer token (``FOUNDRY_API_TOKEN``); disabled entirely when no token is
  configured, because an unauthenticated approval endpoint would let anyone
  bypass the human gate. Approval *roles* are never accepted from the caller -
  they come from the configured approver -> roles mapping.
- ``GET /runs`` and ``GET /runs/{run_id}`` - run status read from the DB.

Everything is injected through ``app.state`` so a Postgres session factory, a
real coding-agent provider, LLM-backed engines, or a Linear connector can be
swapped in without touching the routes.
"""

from __future__ import annotations

import hmac
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func

from foundry.compliance import (
    DEFAULT_CONTROL_MAPPINGS,
    ControlMapping,
    build_evidence_pack,
    render_evidence_html,
)
from foundry.config import Settings
from foundry.connectors.github import GitHubConnector
from foundry.connectors.gitlab import GitLabConnector
from foundry.drivers import InlineDriver, RunDriver
from foundry.connectors.linear import LinearConnector
from foundry.connectors.transport import (
    github_transport,
    gitlab_transport,
    linear_transport,
)
from foundry.db.base import init_schema, make_engine, make_session_factory
from foundry.db.models import (
    FoundryAgentJob,
    FoundryArtifact,
    FoundryAuditEvent,
    FoundryPolicyDecision,
    FoundryRun,
)
from foundry.engines import build_openai_analyzer
from foundry.orchestrator import FoundryOrchestrator, OrchestratorError
from foundry.policy import LocalPolicyEngine, OpaPolicyEngine, PolicyEngine
from foundry.schemas.common import ApprovalRole, RunStatus
from foundry.schemas.ticket import RawTicket

from .dashboard import DASHBOARD_HTML
from .dedup import WebhookDeduplicator, webhook_timestamp_fresh
from .mapping import linear_payload_to_ticket
from .ratelimit import RateLimiter
from .slack import parse_slack_interaction
from .security import (
    is_authorised_approver,
    parse_command,
    verify_signature,
    verify_slack_signature,
)

# Trigger conditions: a run starts only on an explicit opt-in, never for every
# new issue (that would create noise).
_TRIGGER_LABEL = "foundry:candidate"
_TRIGGER_STATUS = "Ready for AI Analysis"

TicketMapper = Callable[[dict[str, Any]], RawTicket]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


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
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
    }


def _extract_event_id(payload: dict[str, Any], delivery_header: str | None) -> str | None:
    return delivery_header or payload.get("deliveryId") or payload.get("id")


def _rate_limit_bucket(path: str) -> str | None:
    """Which rate-limit bucket (if any) a request path falls into.

    ``webhook`` covers every inbound provider delivery; ``api`` covers the run
    reads, metrics and the approval POST. ``/healthz`` (load-balancer polling),
    ``/dashboard`` (static HTML) and the docs are deliberately unthrottled.
    """
    if path.startswith("/webhooks/"):
        return "webhook"
    if path == "/runs" or path.startswith("/runs/") or path.startswith("/metrics"):
        return "api"
    return None


def _is_trigger(payload: dict[str, Any], *, label: str, status: str) -> bool:
    data = payload.get("data", {}) or {}
    labels = {
        lab.get("name") for lab in data.get("labels", []) if isinstance(lab, dict)
    }
    if label in labels:
        return True
    if (data.get("state") or {}).get("name") == status:
        return True
    body = data.get("body") or payload.get("body") or ""
    command = parse_command(body)
    if command and command.command == "start":
        return True
    return body.strip().startswith("/foundry analyse")


def _trigger_type(payload: dict[str, Any], *, status: str) -> str:
    data = payload.get("data", {}) or {}
    body = data.get("body") or payload.get("body") or ""
    if body.strip().startswith("/foundry"):
        return "comment_command"
    if (data.get("state") or {}).get("name") == status:
        return "status"
    return "label"


def create_app(
    *,
    webhook_secret: str,
    session_factory=None,
    orchestrator: FoundryOrchestrator | None = None,
    approvers: Mapping[str, Iterable[str]] | None = None,
    api_token: str | None = None,
    ticket_mapper: TicketMapper | None = None,
    github_webhook_secret: str | None = None,
    github_connector: GitHubConnector | None = None,
    jira_webhook_secret: str | None = None,
    jira_allow_query_token: bool = False,
    gitlab_webhook_secret: str | None = None,
    gitlab_connector: GitLabConnector | None = None,
    slack_signing_secret: str | None = None,
    driver: RunDriver | None = None,
    trigger_label: str = _TRIGGER_LABEL,
    trigger_status: str = _TRIGGER_STATUS,
    control_mappings: tuple[ControlMapping, ...] | None = None,
    webhook_dedup_ttl_seconds: int | None = 86_400,
    webhook_replay_max_age_seconds: int | None = None,
    clock: Callable[[], datetime] | None = None,
    rate_limit_enabled: bool = True,
    rate_limit_webhook_per_minute: int = 120,
    rate_limit_api_per_minute: int = 60,
) -> FastAPI:
    if session_factory is None:
        engine = make_engine()
        init_schema(engine)
        session_factory = make_session_factory(engine)

    app = FastAPI(title="Project Foundry", version="1.1.0")
    orch = orchestrator or FoundryOrchestrator(session_factory)
    # Reads use the orchestrator/DB directly; mutations go through the driver
    # seam (inline today, durable Temporal later) so there is one execution path.
    app.state.orchestrator = orch
    app.state.session_factory = session_factory
    app.state.driver = driver or InlineDriver(orch)
    app.state.webhook_secret = webhook_secret
    # user -> roles that user's approval actually grants. Roles are config,
    # never request payload: a caller cannot self-assert "security".
    app.state.approvers = {
        user: {ApprovalRole(r) for r in roles}
        for user, roles in (approvers or {}).items()
    }
    app.state.api_token = api_token
    app.state.ticket_mapper = ticket_mapper or linear_payload_to_ticket
    # GitHub PR webhooks default to the same signing secret unless given one.
    app.state.github_webhook_secret = github_webhook_secret or webhook_secret
    app.state.github_connector = github_connector or GitHubConnector()
    # Jira/GitLab/Slack endpoints are fail-closed: no secret => endpoint disabled.
    app.state.jira_webhook_secret = jira_webhook_secret
    # The Jira token is an approver-level credential; accept it from the
    # request header only unless query delivery is explicitly opted in.
    app.state.jira_allow_query_token = jira_allow_query_token
    app.state.gitlab_webhook_secret = gitlab_webhook_secret
    app.state.slack_signing_secret = slack_signing_secret
    # Without a transport the connector is diff-blind (file gates skipped),
    # mirroring GitHubConnector with no transport.
    app.state.gitlab_connector = gitlab_connector or GitLabConnector()
    app.state.trigger_label = trigger_label
    app.state.trigger_status = trigger_status
    # Control mappings for compliance evidence packs (config, never payload).
    app.state.control_mappings = (
        DEFAULT_CONTROL_MAPPINGS if control_mappings is None else tuple(control_mappings)
    )
    # Replay defences. Dedup is durable (DB-backed, atomic across workers,
    # TTL-pruned) - it replaces the old per-process set that was lost on
    # restart and unbounded. Replay-age validation is opt-in: only providers
    # that actually carry a timestamp (Linear's webhookTimestamp) can use it.
    app.state.clock = clock or (lambda: datetime.now(timezone.utc))
    app.state.deduplicator = WebhookDeduplicator(
        session_factory,
        ttl_seconds=webhook_dedup_ttl_seconds,
        clock=app.state.clock,
    )
    app.state.webhook_replay_max_age_seconds = webhook_replay_max_age_seconds

    # Per-client request caps on the network surfaces. Two buckets so a flood on
    # one surface cannot starve the other. Per-process (like the dedup set);
    # see api/ratelimit.py for the scope caveats. Disabled => no limiters wired.
    if rate_limit_enabled:
        app.state.rate_limiters = {
            "webhook": RateLimiter(limit=rate_limit_webhook_per_minute),
            "api": RateLimiter(limit=rate_limit_api_per_minute),
        }
    else:
        app.state.rate_limiters = {}

    @app.middleware("http")
    async def _rate_limit(request: Request, call_next):
        bucket = _rate_limit_bucket(request.url.path)
        limiter = app.state.rate_limiters.get(bucket) if bucket else None
        if limiter is None:
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        result = limiter.check(f"{bucket}:{client}")
        if not result.allowed:
            # 429 before the handler runs: no signature check, no DB hit, no
            # workflow side effect for a throttled request.
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded; retry later"},
                headers={
                    "Retry-After": str(result.retry_after),
                    "X-RateLimit-Limit": str(result.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        return response

    def _submit_decision(run_id: str, *, command: str, user: str) -> None:
        """Drive an approve/reject/stop decision with config-derived roles."""
        roles = app.state.approvers.get(user, set())
        app.state.driver.submit_decision(
            run_id, decision=command, user=user, roles=set(roles)
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> HTMLResponse:
        """Read-only run dashboard (static page, no build step).

        Disabled when no API token is configured - the page is useless without
        the token-gated timeline endpoint, and serving it would advertise the
        API surface for free. Same fail-closed posture as approvals.
        """
        if not app.state.api_token:
            raise HTTPException(
                status_code=403,
                detail="dashboard disabled: configure FOUNDRY_API_TOKEN",
            )
        return HTMLResponse(DASHBOARD_HTML)

    @app.post("/webhooks/linear", status_code=202)
    async def linear_webhook(
        request: Request,
        linear_signature: str | None = Header(default=None, alias="Linear-Signature"),
        delivery_id: str | None = Header(default=None, alias="Linear-Delivery"),
    ) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "application/json")
        if content_type and "application/json" not in content_type:
            raise HTTPException(status_code=400, detail="content-type must be application/json")
        raw = await request.body()
        if not verify_signature(app.state.webhook_secret, raw, linear_signature):
            # Reject unauthorised webhooks; no workflow starts.
            raise HTTPException(status_code=401, detail="invalid webhook signature")

        payload = await request.json()
        orch: FoundryOrchestrator = app.state.orchestrator

        # Replay-age guard (opt-in): a captured, validly-signed delivery still
        # fails once it is older than the window. Linear sends webhookTimestamp
        # (epoch ms); fail-closed when the check is on but no timestamp is sent.
        max_age = app.state.webhook_replay_max_age_seconds
        if max_age is not None and not webhook_timestamp_fresh(
            payload.get("webhookTimestamp"),
            now=app.state.clock(),
            max_age_seconds=max_age,
        ):
            raise HTTPException(
                status_code=401,
                detail="stale or missing webhook timestamp (possible replay)",
            )

        event_id = _extract_event_id(payload, delivery_id)
        ticket = app.state.ticket_mapper(payload)

        # Durable dedup: marks the delivery processed and tells us if it was
        # already seen (across workers and restarts), then we branch on intent.
        if app.state.deduplicator.seen("linear", event_id):
            return {"status": "duplicate", "run": _existing_run(orch, ticket.issue_id)}

        # Approval commands arrive as Linear comments. The webhook signature
        # authenticates the payload, and the actor identity comes from Linear
        # itself - this is the primary, already-authenticated approval surface.
        data = payload.get("data", {}) or {}
        body_text = data.get("body") or payload.get("body") or ""
        command = parse_command(body_text)
        if command and command.command in {"approve", "reject", "stop"}:
            return _handle_linear_decision(
                app, orch, ticket.issue_id, command.command, payload
            )

        if not _is_trigger(
            payload, label=app.state.trigger_label, status=app.state.trigger_status
        ):
            return {"status": "ignored", "reason": "no trigger condition matched"}

        if not ticket.issue_id:
            raise HTTPException(status_code=400, detail="missing issue id in payload")

        # At most one *active* run per issue. A finished, blocked, rejected or
        # needs-clarification run does not pin the issue forever: an updated
        # ticket can be re-analysed with a fresh trigger.
        active_id = orch.find_active_run_id_for_issue(ticket.issue_id)
        if active_id is not None:
            return {"status": "exists", "run": _run_to_dict(orch.get_run(active_id))}

        run_id = app.state.driver.start(
            ticket,
            trigger_type=_trigger_type(payload, status=app.state.trigger_status),
            created_by=(data.get("actor") or {}).get("name"),
        )
        return {"status": "started", "run": _run_to_dict(orch.get_run(run_id))}

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(
        request: Request,
        signature: str | None = Header(default=None, alias="X-Hub-Signature-256"),
        event: str | None = Header(default=None, alias="X-GitHub-Event"),
        delivery_id: str | None = Header(default=None, alias="X-GitHub-Delivery"),
    ) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "application/json")
        if content_type and "application/json" not in content_type:
            raise HTTPException(status_code=400, detail="content-type must be application/json")
        raw = await request.body()
        if not verify_signature(app.state.github_webhook_secret, raw, signature):
            raise HTTPException(status_code=401, detail="invalid webhook signature")

        payload = await request.json()

        # Durable dedup spans every GitHub delivery, not just intake: a
        # replayed pull_request / check_run event re-drives real state
        # (re-dispatch, status flips), so dedup before any of those paths.
        # GitHub carries no timestamp, only the X-GitHub-Delivery UUID, so this
        # delivery-id dedup *is* GitHub's replay protection.
        if app.state.deduplicator.seen("github", delivery_id):
            return {"status": "duplicate"}

        # GitHub Issues as the tracker: the issue is the ticket, the label is
        # the trigger, and /foundry commands work in issue comments.
        if event in {"issues", "issue_comment"}:
            return _handle_github_issue_event(app, event, payload)

        # Best-effort: keep catalog staleness detection live between sweeps.
        _nudge_catalog_pushed_at(app, payload)

        connector: GitHubConnector = app.state.github_connector
        pr_state = connector.pr_state_from_event(event or "", payload)
        if pr_state is None:
            return {"status": "ignored", "reason": f"event '{event}' not handled"}
        return _observe_pr(pr_state)

    def _observe_pr(pr_state) -> dict[str, Any]:
        """Correlate an observed PR/MR state to a run and record it.

        Shared by the GitHub and GitLab webhook paths - the orchestrator does
        not care which SCM produced the observation.
        """
        orch: FoundryOrchestrator = app.state.orchestrator
        # Branch match first; falls back to the issue key embedded in the
        # branch or PR title (delegated agents choose their own branch names).
        run_id = orch.correlate_pr(pr_state)
        if run_id is None:
            # A PR Foundry did not initiate.
            return {"status": "ignored", "reason": "no run matches this PR"}

        try:
            app.state.driver.observe_pr(run_id, pr_state)
        except OrchestratorError as exc:
            # E.g. an event for a run already blocked/complete. Not an error
            # worth a retry storm from the SCM - acknowledge and move on.
            return {"status": "ignored", "reason": str(exc), "run_id": run_id}
        run = orch.get_run(run_id)
        return {"status": "recorded", "run_status": run.status.value, "run_id": run_id}

    @app.post("/webhooks/gitlab", status_code=202)
    async def gitlab_webhook(
        request: Request,
        token: str | None = Header(default=None, alias="X-Gitlab-Token"),
        event: str | None = Header(default=None, alias="X-Gitlab-Event"),
    ) -> dict[str, Any]:
        """GitLab MR/pipeline events. GitLab sends the shared secret verbatim
        in X-Gitlab-Token (no HMAC); compared in constant time, fail-closed
        when unconfigured."""
        if not app.state.gitlab_webhook_secret:
            raise HTTPException(
                status_code=403,
                detail="gitlab webhook disabled: configure FOUNDRY_GITLAB_WEBHOOK_SECRET",
            )
        if not hmac.compare_digest(app.state.gitlab_webhook_secret, token or ""):
            raise HTTPException(status_code=401, detail="invalid webhook token")

        connector: GitLabConnector = app.state.gitlab_connector
        payload = await request.json()
        pr_state = connector.pr_state_from_event(event or "", payload)
        if pr_state is None:
            return {"status": "ignored", "reason": f"event '{event}' not handled"}
        return _observe_pr(pr_state)

    @app.post("/webhooks/jira", status_code=202)
    async def jira_webhook(
        request: Request,
        token: str | None = Header(default=None, alias="X-Foundry-Webhook-Token"),
    ) -> dict[str, Any]:
        """Jira issue/comment events. Jira webhooks carry no HMAC signature;
        the shared secret travels as a token in the X-Foundry-Webhook-Token
        header. The token is effectively an approver-level credential, so it
        is header-only by default — query delivery (?token=, which leaks into
        access logs/proxies/link history) must be opted in with
        ``tracker.jira_allow_query_token``. Fail-closed when unconfigured."""
        if not app.state.jira_webhook_secret:
            raise HTTPException(
                status_code=403,
                detail="jira webhook disabled: configure FOUNDRY_JIRA_WEBHOOK_SECRET",
            )
        query_token = (
            request.query_params.get("token")
            if app.state.jira_allow_query_token
            else None
        )
        supplied = token or query_token or ""
        if not hmac.compare_digest(app.state.jira_webhook_secret, supplied):
            raise HTTPException(status_code=401, detail="invalid webhook token")
        payload = await request.json()
        return _handle_jira_event(app, payload)

    @app.post("/webhooks/slack", status_code=200)
    async def slack_webhook(
        request: Request,
        signature: str | None = Header(default=None, alias="X-Slack-Signature"),
        timestamp: str | None = Header(
            default=None, alias="X-Slack-Request-Timestamp"
        ),
    ) -> dict[str, Any]:
        """Slack interactivity (approve/reject/stop buttons).

        The approval surface for chat: a Slack button click is verified against
        Slack's v0 request signature (HMAC over ``v0:{timestamp}:{body}``) with
        replay-age protection, then driven through the same policy-gated decision
        path as every other surface. Fail-closed: no signing secret => disabled.

        The acting identity is the Slack ``user.id`` Slack signs into the payload,
        so it cannot be forged without the signing secret. Configure approvers by
        Slack user id (as GitHub Issues approvers are keyed by login). Roles come
        from config, never the payload.
        """
        if not app.state.slack_signing_secret:
            raise HTTPException(
                status_code=403,
                detail="slack webhook disabled: configure FOUNDRY_SLACK_SIGNING_SECRET",
            )
        raw = await request.body()
        if not verify_slack_signature(
            app.state.slack_signing_secret, raw, timestamp, signature
        ):
            raise HTTPException(status_code=401, detail="invalid slack signature")

        # Slack delivers interactivity as a urlencoded body with a single JSON
        # ``payload`` field. Signature is over the raw body above; parse it here
        # directly (no multipart dependency) now that it is authenticated.
        fields = urllib.parse.parse_qs(raw.decode("utf-8"))
        raw_payload = (fields.get("payload") or [None])[0]
        if not raw_payload:
            return {"status": "ignored", "reason": "no interaction payload"}
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            return {"status": "ignored", "reason": "unparseable interaction payload"}

        interaction = parse_slack_interaction(payload)
        if interaction is None:
            return {"status": "ignored", "reason": "no actionable foundry decision"}
        return _apply_decision(
            app,
            app.state.orchestrator,
            interaction.issue_id,
            interaction.command,
            interaction.user,
        )

    @app.get("/runs")
    def list_runs(skip: int = 0, limit: int = 100) -> dict[str, Any]:
        orch: FoundryOrchestrator = app.state.orchestrator
        all_runs = orch.list_runs()
        with app.state.session_factory() as session:
            costs = dict(
                session.query(
                    FoundryAgentJob.run_id, func.sum(FoundryAgentJob.cost_usd)
                )
                .filter(FoundryAgentJob.cost_usd.isnot(None))
                .group_by(FoundryAgentJob.run_id)
                .all()
            )
        return {
            "runs": [
                {**_run_to_dict(r), "cost_usd": costs.get(r.id)}
                for r in all_runs[skip : skip + limit]
            ],
            "total": len(all_runs),
            "skip": skip,
            "limit": limit,
        }

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        orch: FoundryOrchestrator = app.state.orchestrator
        run = orch.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return _run_to_dict(run)

    @app.get("/runs/{run_id}/timeline")
    def run_timeline(run_id: str, request: Request) -> dict[str, Any]:
        """The full decision record for a run: every artifact, audit event and
        policy decision, in order. This is the "why did the agent do that?"
        endpoint. Token-gated: artifacts contain ticket content and plans,
        which are more sensitive than the bare statuses on ``GET /runs``.
        """
        _require_api_token(app, request)
        orch: FoundryOrchestrator = app.state.orchestrator
        run = orch.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        with app.state.session_factory() as session:
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
            return {
                "run": _run_to_dict(run),
                "budget": orch.budget_snapshot(run_id),
                "artifacts": [
                    {
                        "id": a.id,
                        "artifact_type": a.artifact_type.value,
                        "version": a.version,
                        "content_hash": a.content_hash,
                        "created_at": _iso(a.created_at),
                        "content": json.loads(a.content_json),
                    }
                    for a in artifacts
                ],
                "audit_events": [
                    {
                        "sequence": e.sequence,
                        "event_type": e.event_type.value,
                        "actor_type": e.actor_type,
                        "actor_id": e.actor_id,
                        "metadata": json.loads(e.metadata_json)
                        if e.metadata_json
                        else None,
                        "created_at": _iso(e.created_at),
                    }
                    for e in events
                ],
                "policy_decisions": [
                    {
                        "policy_name": d.policy_name,
                        "allowed": d.allowed,
                        "reason": d.reason,
                        "input": json.loads(d.input_json),
                        "decision": json.loads(d.decision_json),
                        "created_at": _iso(d.created_at),
                    }
                    for d in decisions
                ],
                "agent_jobs": [
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
                        "error": j.error,
                    }
                    for j in jobs
                ],
            }

    @app.get("/runs/{run_id}/evidence")
    def run_evidence(
        run_id: str, request: Request, format: str = "json"
    ) -> Any:
        """One-click compliance evidence pack for a run: the full chain (ticket,
        plan, approvals with identities, policy decisions, diff-risk checks, PR),
        an integrity verification (recomputed artifact hashes + audit-sequence
        continuity), and the run's evidence mapped onto configured controls
        (SOC 2 / ISO 27001 / EU AI Act).

        ``?format=html`` renders a standalone page; the default is JSON. Token-
        gated like the timeline - the pack contains everything the timeline does.
        """
        _require_api_token(app, request)
        if format not in ("json", "html"):
            raise HTTPException(
                status_code=422, detail="format must be 'json' or 'html'"
            )
        with app.state.session_factory() as session:
            run = session.get(FoundryRun, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="run not found")
            pack = build_evidence_pack(
                session, run, control_mappings=app.state.control_mappings
            )
        if format == "html":
            return HTMLResponse(render_evidence_html(pack))
        return pack

    @app.get("/metrics/delivery")
    def metrics_delivery(request: Request, days: int = 90) -> dict[str, Any]:
        """Delivery-memory aggregates: PRs shipped, blocks, time-to-merge,
        cost, and routing precision by confidence band. Token-gated like the
        timeline - per-team cost and routing history are more sensitive than
        the bare statuses on ``GET /runs``.
        """
        from foundry.memory.metrics import delivery_metrics

        _require_api_token(app, request)
        if days < 1:
            raise HTTPException(status_code=422, detail="days must be >= 1")
        since = datetime.now(timezone.utc) - timedelta(days=days)
        with app.state.session_factory() as session:
            return {"days": days, **delivery_metrics(session, since=since)}

    @app.get("/metrics/delivery/trends")
    def metrics_delivery_trends(
        request: Request, days: int = 90, bucket: str = "week"
    ) -> dict[str, Any]:
        """Delivery outcomes bucketed over time (PRs shipped, blocks, spend per
        period) - the trend the single-window ``/metrics/delivery`` can't show.
        Token-gated and fail-closed like the other metrics endpoints.
        """
        from foundry.memory.metrics import TREND_BUCKETS, delivery_trends

        _require_api_token(app, request)
        if days < 1:
            raise HTTPException(status_code=422, detail="days must be >= 1")
        if bucket not in TREND_BUCKETS:
            raise HTTPException(
                status_code=422,
                detail=f"bucket must be one of {list(TREND_BUCKETS)}",
            )
        since = datetime.now(timezone.utc) - timedelta(days=days)
        with app.state.session_factory() as session:
            return {
                "days": days,
                **delivery_trends(session, since=since, bucket=bucket),
            }

    @app.get("/metrics/agents")
    def metrics_agents(request: Request, days: int = 90) -> dict[str, Any]:
        """Per-provider agent scorecards: merge rate, retries and spend, broken
        down by work type and repo. Token-gated like ``/metrics/delivery`` -
        per-agent performance and cost are competitively sensitive. Reporting
        only; acting on it (``agent.provider: auto``) is a separate gated change.
        """
        from foundry.memory.scorecards import agent_scorecards

        _require_api_token(app, request)
        if days < 1:
            raise HTTPException(status_code=422, detail="days must be >= 1")
        since = datetime.now(timezone.utc) - timedelta(days=days)
        with app.state.session_factory() as session:
            return {"days": days, **agent_scorecards(session, since=since)}

    @app.get("/metrics/fleet")
    def metrics_fleet(request: Request) -> dict[str, Any]:
        """Live fleet snapshot: every run's *current* state across the org -
        runs in flight, the human-approval queue depth, agents running, PRs
        open, and spend committed by runs not yet finished. The "what are the
        agents doing right now" view the historical ``/metrics/delivery``
        endpoints (finished runs only) can't give; there is no time window
        because it is a snapshot of now. Token-gated and fail-closed like the
        other metrics endpoints.
        """
        from foundry.memory.metrics import fleet_status

        _require_api_token(app, request)
        with app.state.session_factory() as session:
            return fleet_status(session)

    @app.post("/runs/{run_id}/approval")
    def approval(
        run_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        _require_api_token(app, request)
        orch: FoundryOrchestrator = app.state.orchestrator
        user = body.get("user")
        text = body.get("text") or body.get("command") or ""
        if not user:
            raise HTTPException(status_code=400, detail="missing user")

        command = parse_command(text) or parse_command(f"/foundry {text}")
        if command is None:
            raise HTTPException(status_code=400, detail="unrecognised command")

        if not is_authorised_approver(user, set(app.state.approvers)):
            raise HTTPException(status_code=403, detail="user not authorised to approve")

        if command.command not in {"approve", "reject", "stop"}:
            raise HTTPException(
                status_code=422,
                detail=f"command '{command.command}' is not supported yet",
            )

        if orch.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")

        # Roles deliberately ignored from the body: they are configuration.
        try:
            _submit_decision(run_id, command=command.command, user=user)
        except OrchestratorError as exc:
            msg = str(exc)
            # Authorisation refusals (policy block at dispatch, or the approver
            # lacking a role this run requires) → 403. Wrong state for the
            # operation (e.g. already approved) → 409.
            if "policy gate blocked" in msg or "approval refused" in msg:
                raise HTTPException(status_code=403, detail=msg) from exc
            raise HTTPException(status_code=409, detail=msg) from exc

        run = orch.get_run(run_id)
        # On approve, the driver also attempts dispatch; the run reaching
        # agent_running tells us the policy gate allowed autonomous work.
        result: dict[str, Any] = {
            "command": command.command,
            "status": "applied",
            "run": _run_to_dict(run),
        }
        if command.command == "approve":
            result["dispatched"] = run.status is RunStatus.AGENT_RUNNING
        return result

    return app


def _nudge_catalog_pushed_at(app: FastAPI, payload: dict[str, Any]) -> None:
    """Best-effort: bump pushed_at on the catalog row when a GitHub push arrives.

    This keeps staleness detection live between sync sweeps so an active repo
    won't be confidently served from stale metadata.  A catalog hiccup must
    never fail webhook processing.
    """
    try:
        from datetime import timezone

        from foundry.db.models import FoundryRepoCatalogEntry

        repo_name = (payload.get("repository") or {}).get("full_name")
        if not repo_name:
            return
        now = datetime.now(timezone.utc)
        with app.state.session_factory() as session:
            entry = session.get(FoundryRepoCatalogEntry, repo_name)
            if entry is not None:
                entry.pushed_at = now
                session.commit()
    except Exception:
        import logging
        logging.getLogger(__name__).debug(
            "catalog pushed_at nudge failed", exc_info=True
        )


def _existing_run(orch: FoundryOrchestrator, issue_id: str) -> dict[str, Any] | None:
    if not issue_id:
        return None
    run_id = orch.find_run_id_for_issue(issue_id)
    return _run_to_dict(orch.get_run(run_id)) if run_id else None


def _require_api_token(app: FastAPI, request: Request) -> None:
    """Enforce bearer-token auth on mutating API endpoints.

    Fail closed: with no token configured the endpoint is disabled outright,
    because an unauthenticated approval surface defeats the human gate. The
    signed Linear webhook remains available for approvals either way.
    """
    expected = app.state.api_token
    if not expected:
        raise HTTPException(
            status_code=403,
            detail=(
                "the approval API is disabled: no FOUNDRY_API_TOKEN is "
                "configured; approve via a signed Linear comment instead"
            ),
        )
    provided = request.headers.get("authorization", "")
    if not hmac.compare_digest(provided, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def _actor_identity(payload: dict[str, Any]) -> str | None:
    """The acting user from a Linear webhook payload (email preferred)."""
    data = payload.get("data", {}) or {}
    for actor in (data.get("actor"), payload.get("actor")):
        if isinstance(actor, dict):
            identity = actor.get("email") or actor.get("name")
            if identity:
                return str(identity)
    return None


def _handle_linear_decision(
    app: FastAPI,
    orch: FoundryOrchestrator,
    issue_id: str,
    command: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Apply an approve/reject/stop comment from a signed Linear webhook."""
    return _apply_decision(app, orch, issue_id, command, _actor_identity(payload))


def _handle_github_issue_event(
    app: FastAPI, event: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Intake + approvals when the tracker is GitHub Issues.

    Mirrors the Linear webhook semantics: the webhook signature authenticates
    the payload, the actor identity comes from GitHub itself (the sender's
    login - configure approvers by login when using GitHub Issues), the
    trigger label starts runs, and ``/foundry`` comments drive decisions.
    """
    from .mapping import github_issue_payload_to_ticket

    orch: FoundryOrchestrator = app.state.orchestrator
    ticket = github_issue_payload_to_ticket(payload)
    if not ticket.issue_id:
        return {"status": "ignored", "reason": "missing issue in payload"}

    comment_body = (payload.get("comment") or {}).get("body") or ""
    sender = (
        ((payload.get("comment") or {}).get("user") or {}).get("login")
        or (payload.get("sender") or {}).get("login")
        or ""
    )
    command = parse_command(comment_body)
    if command and command.command in {"approve", "reject", "stop"}:
        return _apply_decision(app, orch, ticket.issue_id, command.command, sender)

    # Label triggers only apply to issue events; a stray comment on a labelled
    # issue must not restart a finished run. Comment triggers are explicit.
    label_trigger = event == "issues" and app.state.trigger_label in ticket.labels
    comment_trigger = bool(command and command.command == "start") or (
        comment_body.strip().startswith("/foundry analyse")
    )
    if not (label_trigger or comment_trigger):
        return {"status": "ignored", "reason": "no trigger condition matched"}

    active_id = orch.find_active_run_id_for_issue(ticket.issue_id)
    if active_id is not None:
        return {"status": "exists", "run": _run_to_dict(orch.get_run(active_id))}

    run_id = app.state.driver.start(
        ticket,
        trigger_type="comment_command" if comment_trigger else "label",
        created_by=sender or None,
    )
    return {"status": "started", "run": _run_to_dict(orch.get_run(run_id))}


def _handle_jira_event(app: FastAPI, payload: dict[str, Any]) -> dict[str, Any]:
    """Intake + approvals when the tracker is Jira.

    Same semantics as the Linear webhook: the trigger label starts runs,
    ``/foundry`` comments drive decisions, and the actor identity is the
    comment author's email as reported by Jira (configure approvers by email).
    """
    from .mapping import jira_payload_to_ticket

    orch: FoundryOrchestrator = app.state.orchestrator
    ticket = jira_payload_to_ticket(payload)
    if not ticket.issue_id:
        return {"status": "ignored", "reason": "missing issue in payload"}

    comment = payload.get("comment") or {}
    body = comment.get("body") or ""
    author = (comment.get("author") or {}).get("emailAddress") or ""
    command = parse_command(body)
    if command and command.command in {"approve", "reject", "stop"}:
        return _apply_decision(app, orch, ticket.issue_id, command.command, author)

    # Label triggers only apply to issue events; chatter on a labelled issue
    # must not restart a finished run.
    event_name = str(payload.get("webhookEvent") or "")
    label_trigger = (
        event_name.startswith("jira:issue")
        and app.state.trigger_label in ticket.labels
    )
    comment_trigger = bool(command and command.command == "start") or (
        body.strip().startswith("/foundry analyse")
    )
    if not (label_trigger or comment_trigger):
        return {"status": "ignored", "reason": "no trigger condition matched"}

    active_id = orch.find_active_run_id_for_issue(ticket.issue_id)
    if active_id is not None:
        return {"status": "exists", "run": _run_to_dict(orch.get_run(active_id))}

    creator = (payload.get("user") or {}).get("emailAddress") or author or None
    run_id = app.state.driver.start(
        ticket,
        trigger_type="comment_command" if comment_trigger else "label",
        created_by=creator,
    )
    return {"status": "started", "run": _run_to_dict(orch.get_run(run_id))}


def _apply_decision(
    app: FastAPI,
    orch: FoundryOrchestrator,
    issue_id: str,
    command: str,
    user: str | None,
) -> dict[str, Any]:
    """Apply an approve/reject/stop decision from a signed tracker webhook.

    Always returns 2xx payloads (never raises): a 4xx/5xx here would make the
    tracker retry-deliver a comment that was processed and refused.
    """
    if not issue_id:
        return {"status": "ignored", "reason": "missing issue id in payload"}
    if not user:
        return {"status": "ignored", "reason": "no actor identity in payload"}
    if not is_authorised_approver(user, set(app.state.approvers)):
        return {
            "status": "ignored",
            "reason": f"'{user}' is not an authorised approver",
        }
    run_id = orch.find_active_run_id_for_issue(issue_id)
    if run_id is None:
        return {"status": "ignored", "reason": "no active run for issue"}
    roles = app.state.approvers.get(user, set())
    try:
        app.state.driver.submit_decision(
            run_id, decision=command, user=user, roles=set(roles)
        )
    except OrchestratorError as exc:
        return {"status": "refused", "reason": str(exc), "run_id": run_id}
    run = orch.get_run(run_id)
    result: dict[str, Any] = {
        "status": "applied",
        "command": command,
        "run": _run_to_dict(run),
    }
    if command == "approve":
        result["dispatched"] = run.status is RunStatus.AGENT_RUNNING
    return result


# -- deployment entrypoints ---------------------------------------------------


def build_provider(settings: Settings, tracker=None):
    """Resolve the configured coding-agent provider (``agent.provider``).

    Fail-closed: a provider that is selected but missing its credentials is a
    configuration error at startup, not a silent fallback to another agent.
    """
    from foundry.agents import (
        ClaudeCodeProvider,
        CursorCloudAgentProvider,
        CursorViaLinearProvider,
        InMemoryFakeProvider,
        ManualProvider,
        WebhookProvider,
    )
    from foundry.connectors.transport import (
        json_get_transport,
        json_post_transport,
        raw_post_transport,
    )

    name = settings.agent_provider
    if name == "manual":
        return ManualProvider()
    if name == "fake":
        return InMemoryFakeProvider()
    if name == "cursor_via_linear":
        if tracker is None:
            raise ValueError(
                "agent.provider=cursor_via_linear requires FOUNDRY_LINEAR_API_TOKEN"
            )
        return CursorViaLinearProvider(tracker)
    if name == "cursor_cloud":
        if not settings.cursor_api_token:
            raise ValueError(
                "agent.provider=cursor_cloud requires FOUNDRY_CURSOR_API_TOKEN"
            )
        headers = {"Authorization": f"Bearer {settings.cursor_api_token}"}
        return CursorCloudAgentProvider(
            http_post=json_post_transport(headers),
            http_get=json_get_transport(headers),
        )
    if name == "claude_code":
        if not settings.github_api_token:
            raise ValueError(
                "agent.provider=claude_code requires FOUNDRY_GITHUB_API_TOKEN "
                "(to fire workflow_dispatch)"
            )
        return ClaudeCodeProvider(
            http_post=json_post_transport(
                {
                    "Authorization": f"Bearer {settings.github_api_token}",
                    "Accept": "application/vnd.github+json",
                }
            ),
            workflow_file=settings.claude_workflow_file,
        )
    if name == "webhook":
        if not settings.agent_webhook_url:
            raise ValueError(
                "agent.provider=webhook requires FOUNDRY_AGENT_WEBHOOK_URL"
            )
        return WebhookProvider(
            url=settings.agent_webhook_url,
            http_post=raw_post_transport(),
            signing_secret=settings.agent_webhook_secret,
        )
    raise ValueError(f"unknown agent.provider: {name!r}")


def build_policy_engine(settings: Settings) -> PolicyEngine:
    """Select the policy backend from config.

    ``local`` (default) is the in-process Python engine; ``opa`` delegates to an
    OPA server running the foundry.rego bundle. Both enforce the same rules - the
    configured confidence threshold is passed to whichever backend is chosen, so
    the threshold can never diverge between them.
    """
    if settings.policy_provider == "opa":
        return OpaPolicyEngine(
            base_url=settings.policy_opa_url,
            repo_confidence_threshold=settings.repo_confidence_threshold,
        )
    return LocalPolicyEngine(
        repo_confidence_threshold=settings.repo_confidence_threshold
    )


def build_orchestrator(settings: Settings, session_factory) -> FoundryOrchestrator:
    """Assemble an orchestrator from settings: analyzer, policy thresholds, Linear."""
    from foundry.engines.enrichment import CatalogContextEnricher, StaticContextEnricher

    analyzer = (
        build_openai_analyzer(model=settings.openai_model)
        if settings.use_openai_analyzer
        else None
    )
    risk_classifier = None
    diff_risk_classifier = None
    if settings.risk_provider == "llm":
        from foundry.engines.llm import OpenAIStructuredLLM
        from foundry.engines.llm_risk import LlmDiffRiskClassifier, LlmRiskClassifier

        risk_llm = OpenAIStructuredLLM(model=settings.risk_model)
        risk_classifier = LlmRiskClassifier(risk_llm)
        diff_risk_classifier = LlmDiffRiskClassifier(
            risk_llm, settings.sensitive_globs_map
        )
    planner = None
    if settings.planner_provider == "llm":
        from foundry.engines.llm_planner import build_llm_planner

        planner = build_llm_planner(model=settings.planner_model)
    tracker = None
    if settings.tracker_provider == "github_issues":
        if not settings.github_api_token:
            raise ValueError(
                "tracker.provider=github_issues requires FOUNDRY_GITHUB_API_TOKEN"
            )
        from foundry.connectors.github_issues import GitHubIssuesConnector

        tracker = GitHubIssuesConnector(
            transport=github_transport(settings.github_api_token)
        )
    elif settings.tracker_provider == "jira":
        if not (settings.jira_base_url and settings.jira_email and settings.jira_api_token):
            raise ValueError(
                "tracker.provider=jira requires FOUNDRY_JIRA_BASE_URL, "
                "FOUNDRY_JIRA_EMAIL and FOUNDRY_JIRA_API_TOKEN"
            )
        from foundry.connectors.jira import JiraConnector
        from foundry.connectors.transport import jira_transport

        tracker = JiraConnector(
            transport=jira_transport(
                settings.jira_base_url, settings.jira_email, settings.jira_api_token
            )
        )
    elif settings.tracker_provider == "linear":
        if settings.linear_api_token:
            tracker = LinearConnector(
                transport=linear_transport(settings.linear_api_token)
            )
    else:
        raise ValueError(f"unknown tracker.provider: {settings.tracker_provider!r}")

    # Outbound Slack notifications are fail-closed: wired only when BOTH the bot
    # token and a channel are configured, mirroring the no-token-=>-no-connector
    # posture of the tracker. Either missing => the orchestrator simply has no
    # notifier and runs as before.
    notifier = None
    if settings.slack_bot_token and settings.slack_channel:
        from foundry.connectors.slack import SlackNotifier
        from foundry.connectors.transport import slack_transport

        notifier = SlackNotifier(
            transport=slack_transport(settings.slack_bot_token, settings.slack_channel)
        )

    repo_keywords = {repo: list(kws) for repo, kws in settings.context_repo_keywords}
    if settings.context_provider in ("catalog", "code"):
        priors = None
        if settings.memory_priors_enabled:
            from foundry.memory.priors import DeliveryMemoryPriors

            priors = DeliveryMemoryPriors(
                session_factory,
                min_samples=settings.memory_min_samples,
                confidence_cap=settings.memory_confidence_cap,
            )
        if settings.context_provider == "code":
            from foundry.engines.code_context import CodeContextEnricher

            enricher_cls = CodeContextEnricher
        else:
            enricher_cls = CatalogContextEnricher
        enricher = enricher_cls(
            session_factory,
            repo_keywords=repo_keywords,
            max_catalog_age_days=settings.context_max_catalog_age_days,
            priors=priors,
        )
    else:
        enricher = StaticContextEnricher(repo_catalog=repo_keywords)

    return FoundryOrchestrator(
        session_factory,
        analyzer=analyzer,
        risk_classifier=risk_classifier,
        diff_risk_classifier=diff_risk_classifier,
        planner=planner,
        enricher=enricher,
        issue_tracker=tracker,
        notifier=notifier,
        provider=build_provider(settings, tracker),
        policy_engine=build_policy_engine(settings),
        max_files_changed=settings.max_files_changed,
        forbidden_globs=settings.forbidden_globs,
        sensitive_path_globs=settings.sensitive_globs_map,
        max_agent_retries=settings.max_agent_retries,
        retry_on=settings.retry_on,
        max_cost_per_run=settings.max_cost_per_run,
        estimated_cost_per_dispatch=settings.estimated_cost_per_dispatch,
    )


def app_from_settings(settings: Settings) -> FastAPI:
    engine = make_engine(settings.database_url)
    init_schema(engine)
    session_factory = make_session_factory(engine)
    github_connector = (
        GitHubConnector(transport=github_transport(settings.github_api_token))
        if settings.github_api_token
        else None
    )
    gitlab_connector = (
        GitLabConnector(
            transport=gitlab_transport(
                settings.gitlab_api_token, base=settings.gitlab_api_base
            )
        )
        if settings.gitlab_api_token
        else None
    )
    return create_app(
        webhook_secret=settings.linear_webhook_secret,
        session_factory=session_factory,
        orchestrator=build_orchestrator(settings, session_factory),
        approvers={email: roles for email, roles in settings.approvers},
        api_token=settings.api_token,
        github_webhook_secret=settings.github_webhook_secret,
        github_connector=github_connector,
        jira_webhook_secret=settings.jira_webhook_secret,
        jira_allow_query_token=settings.jira_allow_query_token,
        gitlab_webhook_secret=settings.gitlab_webhook_secret,
        gitlab_connector=gitlab_connector,
        slack_signing_secret=settings.slack_signing_secret,
        trigger_label=settings.trigger_label,
        trigger_status=settings.trigger_status,
        control_mappings=settings.compliance_control_mappings,
        webhook_dedup_ttl_seconds=settings.webhook_dedup_ttl_seconds,
        webhook_replay_max_age_seconds=settings.webhook_replay_max_age_seconds,
        rate_limit_enabled=settings.rate_limit_enabled,
        rate_limit_webhook_per_minute=settings.rate_limit_webhook_per_minute,
        rate_limit_api_per_minute=settings.rate_limit_api_per_minute,
    )


def app_from_env() -> FastAPI:
    """Uvicorn factory entrypoint: ``uvicorn foundry.api.app:app_from_env --factory``.

    Reads ``FOUNDRY_CONFIG`` (path to a YAML file) plus environment overrides.
    """
    import os

    return app_from_settings(Settings.load(os.environ.get("FOUNDRY_CONFIG"), env=os.environ))
