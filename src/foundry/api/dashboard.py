"""The Foundry dashboard: one static page, zero build step, zero new deps.

Read-only visibility over the audit data that already exists: a live fleet
strip (runs in flight / approval queue / execution queue / review queue / spend
in flight, from the current run states), an approval-queue panel, an
execution-queue panel (in-flight agent runs with run-time age + SLA, issue #37)
and a review-queue panel (open PRs with review-latency age + a "stale since last
push" age, each with its own SLA, issue #37), a failure/triage panel (recently
blocked or execution-failed runs with how long ago they failed and why, newest
first, issue #37),
the delivery metrics
strip, a delivery-trend-over-time table, the agent scorecards, a per-agent
merge-confidence trend (is each agent improving?), a delivery-by-repo table
(where work ships, stalls, and spends) with a per-repo trend sparkline strip
(is each repo speeding up or stalling?), an epic board (multi-repo
runs rolled up, issue #35), a policy-gate panel (the effective gate this
deployment enforces - the in-app twin of ``foundry-policy explain``, issue #31),
the run list (with an approval-queue filter) and,
per run, the full decision timeline (artifacts, audit events, policy decisions,
agent jobs). All data comes from ``GET /runs``, ``GET /metrics/fleet``,
``GET /metrics/approvals``, ``GET /metrics/executions``, ``GET /metrics/reviews``,
``GET /metrics/failures``,
``GET /metrics/delivery``, ``GET /metrics/delivery/trends``,
``GET /metrics/delivery/by-repo``, ``GET /metrics/delivery/by-repo/trends``,
``GET /metrics/agents``,
``GET /metrics/agents/trends``, ``GET /metrics/policy``, ``GET /epics`` and
``GET /runs/{id}/timeline``; the calls carry the bearer token the user pastes
once (kept in localStorage, never sent anywhere but this API).

Served only when an API token is configured - same fail-closed posture as the
approval endpoint.
"""

from __future__ import annotations

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Foundry &mdash; Runs</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --border: #30363d; --text: #e6edf3;
    --muted: #8b949e; --accent: #58a6ff; --green: #3fb950; --red: #f85149;
    --amber: #d29922; --purple: #bc8cff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  header {
    display: flex; align-items: center; gap: 12px; padding: 14px 24px;
    border-bottom: 1px solid var(--border); background: var(--panel);
    position: sticky; top: 0; z-index: 2;
  }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header h1 span { color: var(--accent); }
  header .spacer { flex: 1; }
  input[type=password] {
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 6px 10px; width: 260px;
  }
  button {
    background: #21262d; border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 6px 12px; cursor: pointer;
  }
  button:hover { border-color: var(--muted); }
  a.btn-link {
    background: #21262d; border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 6px 12px; text-decoration: none;
  }
  a.btn-link:hover { border-color: var(--muted); }
  #sso-session { color: var(--muted); font-size: 13px; }
  main { display: grid; grid-template-columns: 380px 1fr; gap: 0; min-height: calc(100vh - 57px); }
  #runs { border-right: 1px solid var(--border); overflow-y: auto; }
  .run {
    padding: 12px 16px; border-bottom: 1px solid var(--border); cursor: pointer;
  }
  .run:hover, .run.active { background: var(--panel); }
  .run .key { font-weight: 600; }
  .run .meta { color: var(--muted); font-size: 12px; margin-top: 2px; }
  .badge {
    display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px;
    border: 1px solid; margin-left: 8px; vertical-align: middle;
  }
  .b-green { color: var(--green); border-color: var(--green); }
  .b-red { color: var(--red); border-color: var(--red); }
  .b-amber { color: var(--amber); border-color: var(--amber); }
  .b-blue { color: var(--accent); border-color: var(--accent); }
  .b-purple { color: var(--purple); border-color: var(--purple); }
  .b-muted { color: var(--muted); border-color: var(--muted); }
  #detail { padding: 20px 28px; overflow-y: auto; }
  #detail .empty { color: var(--muted); margin-top: 40px; text-align: center; }
  h2 { font-size: 15px; margin: 22px 0 10px; color: var(--accent); }
  .event {
    display: flex; gap: 12px; padding: 8px 0; border-bottom: 1px dashed var(--border);
  }
  .event .when { color: var(--muted); font-size: 12px; white-space: nowrap; width: 150px; }
  .event .what { flex: 1; }
  .event .type { font-weight: 600; }
  .event .actor { color: var(--muted); font-size: 12px; }
  .decision { border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; margin: 8px 0; }
  .decision .head { display: flex; gap: 10px; align-items: baseline; }
  .decision .reasons { color: var(--muted); margin: 4px 0 0; padding-left: 18px; }
  details { margin-top: 6px; }
  details summary { cursor: pointer; color: var(--muted); font-size: 12px; }
  pre {
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 10px; overflow-x: auto; font-size: 12px; max-height: 320px;
  }
  .error { color: var(--red); padding: 16px 24px; }
  .kv { color: var(--muted); font-size: 12px; }
  .kv b { color: var(--text); font-weight: 600; }
  #fleet, #metrics, #agents, #trends, #agent-trends, #epics, #queue, #exec-queue, #review-queue {
    display: none; padding: 10px 24px; border-bottom: 1px solid var(--border);
    background: var(--panel); font-size: 13px;
  }
  #fleet { background: #11151c; }
  #fleet .label { color: var(--muted); margin-right: 18px; font-weight: 600; }
  #metrics .stat, #fleet .stat { margin-right: 18px; white-space: nowrap; }
  #metrics .stat b, #fleet .stat b { font-size: 15px; }
  #metrics .stat.good b, #fleet .stat.good b { color: var(--green); }
  #metrics .stat.bad b, #fleet .stat.bad b { color: var(--red); }
  #metrics table, #agents table {
    border-collapse: collapse; margin: 8px 0 2px; font-size: 12px;
  }
  #metrics th, #metrics td, #agents th, #agents td,
  #trends th, #trends td {
    border: 1px solid var(--border); padding: 3px 10px; text-align: left;
    color: var(--muted);
  }
  #metrics th, #agents th, #trends th { color: var(--text); }
  #agents summary, #trends summary, #agent-trends summary {
    color: var(--text); cursor: pointer;
  }
  #trends td.num { text-align: right; font-variant-numeric: tabular-nums; }
  #trends .bar {
    display: inline-block; height: 8px; background: var(--green);
    border-radius: 2px; vertical-align: middle; min-width: 1px;
  }
  #trends .bar.blocked { background: var(--red); }
  #agent-trends .prov {
    display: flex; align-items: center; gap: 12px; padding: 4px 0;
  }
  #agent-trends .prov .name { min-width: 220px; }
  #agent-trends .spark {
    display: inline-flex; align-items: flex-end; gap: 2px; height: 24px;
  }
  #agent-trends .spark i {
    display: inline-block; width: 6px; min-height: 1px;
    background: var(--green); border-radius: 1px;
  }
  #agent-trends .spark i.empty {
    height: 2px; background: var(--border);
  }
  #repo-trends .prov {
    display: flex; align-items: center; gap: 12px; padding: 4px 0;
  }
  #repo-trends .prov .name { min-width: 220px; }
  #repo-trends .spark {
    display: inline-flex; align-items: flex-end; gap: 2px; height: 24px;
  }
  #repo-trends .spark i {
    display: inline-block; width: 6px; min-height: 1px;
    background: var(--green); border-radius: 1px;
  }
  #repo-trends .spark i.empty { height: 2px; background: var(--border); }
  #epics summary { color: var(--text); cursor: pointer; }
  .epic { padding: 8px 0; border-bottom: 1px dashed var(--border); }
  .epic:last-child { border-bottom: none; }
  .epic-head { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px; }
  .epic-head .key { font-weight: 600; cursor: pointer; }
  .epic-head .key:hover { color: var(--accent); }
  .epic-kids { margin: 6px 0 0 12px; display: flex; flex-wrap: wrap; gap: 10px; }
  .epic-child { color: var(--muted); cursor: pointer; white-space: nowrap; }
  .epic-child:hover { color: var(--text); }
  .queue-filter {
    display: flex; gap: 6px; padding: 8px 12px; border-bottom: 1px solid var(--border);
    background: var(--panel); position: sticky; top: 0; z-index: 1;
  }
  .queue-filter button {
    padding: 3px 10px; font-size: 12px;
  }
  .queue-filter button.active {
    border-color: var(--accent); color: var(--accent);
  }
  .queue-filter .count { color: var(--muted); }
  #queue summary, #exec-queue summary, #review-queue summary { color: var(--text); cursor: pointer; }
  #queue .run, #exec-queue .run, #review-queue .run {
    padding: 6px 0; border-bottom: 1px dashed var(--border); cursor: pointer;
  }
  #queue .run:last-child, #exec-queue .run:last-child, #review-queue .run:last-child { border-bottom: none; }
  #queue .run:hover .key, #exec-queue .run:hover .key, #review-queue .run:hover .key { color: var(--accent); }
  #queue .run.breach, #exec-queue .run.breach, #review-queue .run.breach { border-left: 3px solid var(--red); padding-left: 8px; }
  #queue .age, #exec-queue .age, #review-queue .age { margin-left: 8px; font-variant-numeric: tabular-nums; }
  #queue .age.bad, #exec-queue .age.bad, #review-queue .age.bad { color: var(--red); font-weight: 600; }
</style>
</head>
<body>
<header>
  <h1><span>Foundry</span> run dashboard</h1>
  <div class="spacer"></div>
  <a id="sso-login" class="btn-link" href="/dashboard/login" style="display:none">Sign in with SSO</a>
  <span id="sso-session" style="display:none"></span>
  <a id="sso-logout" class="btn-link" href="/dashboard/logout" style="display:none">Sign out</a>
  <input id="token" type="password" placeholder="API token (stored locally)">
  <button id="save">Connect</button>
</header>
<div id="fleet"></div>
<div id="queue"></div>
<div id="exec-queue"></div>
<div id="review-queue"></div>
<div id="failure-queue"></div>
<div id="metrics"></div>
<div id="trends"></div>
<div id="agents"></div>
<div id="agent-trends"></div>
<div id="repo-delivery"></div>
<div id="repo-trends"></div>
<div id="epics"></div>
<div id="policy"></div>
<div id="policy-check"></div>
<main>
  <div id="runs"></div>
  <div id="detail"><div class="empty">Select a run to see its full decision timeline.</div></div>
</main>
<script>
// Injected at serve time: whether SSO login is available, and whether this
// request already carries a valid session cookie (issue #34).
window.__FOUNDRY_OIDC_LOGIN__ = %%OIDC_LOGIN%%;
window.__FOUNDRY_SESSION__ = %%SESSION%%;
</script>
<script>
"use strict";
const $ = (s) => document.querySelector(s);
const tokenInput = $("#token");
tokenInput.value = localStorage.getItem("foundry_token") || "";

// Authenticated when a token is pasted (static/JWT) OR a browser SSO session
// cookie is present. The cookie is HttpOnly, so the page only learns of it via
// the injected flag; either way same-origin fetches carry it automatically.
function hasAuth() {
  return Boolean(localStorage.getItem("foundry_token")) || Boolean(window.__FOUNDRY_SESSION__);
}

function initSso() {
  const loggedIn = Boolean(window.__FOUNDRY_SESSION__);
  if (window.__FOUNDRY_OIDC_LOGIN__ && !loggedIn) {
    $("#sso-login").style.display = "";
  }
  if (loggedIn) {
    $("#sso-session").textContent = "Signed in via SSO";
    $("#sso-session").style.display = "";
    $("#sso-logout").style.display = "";
    // A session covers auth already; the pasted-token box is redundant noise.
    tokenInput.style.display = "none";
    $("#save").style.display = "none";
  }
}

const STATUS_BADGE = {
  complete: "b-green", pr_open: "b-blue", agent_running: "b-purple",
  waiting_approval: "b-amber", review_required: "b-amber",
  needs_clarification: "b-amber", approved: "b-blue", plan_ready: "b-blue",
  analysing: "b-muted", blocked: "b-red", rejected: "b-red",
  execution_failed: "b-red",
};

// Rolled-up epic status -> badge class (mirrors EpicStatus in epics.py).
const EPIC_BADGE = {
  complete: "b-green", in_progress: "b-purple", partial: "b-amber",
  failed: "b-red", empty: "b-muted",
};

function authHeaders() {
  const t = localStorage.getItem("foundry_token");
  return t ? { Authorization: "Bearer " + t } : {};
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function badge(status) {
  return `<span class="badge ${STATUS_BADGE[status] || "b-muted"}">${esc(status)}</span>`;
}

function when(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

function fmtBudget(b) {
  if (!b) return "-";
  const consumed = "$" + Number(b.consumed_usd || 0).toFixed(2);
  if (b.cap_usd == null) return consumed + " (no cap)";
  return consumed + " / $" + Number(b.cap_usd).toFixed(2) + " cap";
}

// Statuses that mean "waiting on a human" - the approval queue.
const AWAITING = new Set(["waiting_approval", "review_required", "needs_clarification"]);
let allRuns = [];
let queueOnly = false;

async function loadRuns() {
  const el = $("#runs");
  try {
    const resp = await fetch("runs", { headers: authHeaders() });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    allRuns = data.runs || [];
    renderRuns();
  } catch (err) {
    el.innerHTML = `<div class="error">Could not load runs: ${esc(err.message)}</div>`;
  }
}

function renderRuns() {
  const el = $("#runs");
  const awaiting = allRuns.filter((r) => AWAITING.has(r.status));
  const shown = (queueOnly ? awaiting : allRuns)
    .slice()
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const filterBar = `
    <div class="queue-filter">
      <button data-q="all" class="${queueOnly ? "" : "active"}">All runs</button>
      <button data-q="queue" class="${queueOnly ? "active" : ""}">Approval queue
        <span class="count">(${awaiting.length})</span></button>
    </div>`;
  const list = shown.length
    ? shown.map((r) => `
        <div class="run" data-id="${esc(r.id)}">
          <span class="key">${esc(r.linear_issue_key)}</span>${badge(r.status)}
          <div class="meta">${esc(r.id)} &middot; ${esc(r.risk_level || "unclassified")} risk
            &middot; ${when(r.created_at)}</div>
        </div>`).join("")
    : `<div class="run"><span class="meta">${queueOnly ? "Nothing awaiting approval." : "No runs yet."}</span></div>`;
  el.innerHTML = filterBar + list;
  el.querySelectorAll(".queue-filter button[data-q]").forEach((node) => {
    node.addEventListener("click", () => {
      queueOnly = node.dataset.q === "queue";
      renderRuns();
    });
  });
  el.querySelectorAll(".run[data-id]").forEach((node) => {
    node.addEventListener("click", () => {
      el.querySelectorAll(".run").forEach((n) => n.classList.remove("active"));
      node.classList.add("active");
      loadTimeline(node.dataset.id);
    });
  });
}

async function loadTimeline(runId) {
  const el = $("#detail");
  el.innerHTML = '<div class="empty">Loading&hellip;</div>';
  let data;
  try {
    const resp = await fetch(`runs/${encodeURIComponent(runId)}/timeline`, {
      headers: authHeaders(),
    });
    if (resp.status === 401 || resp.status === 403) {
      el.innerHTML = '<div class="error">Unauthorised. Sign in with SSO, or paste the API token above and press Connect.</div>';
      return;
    }
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    data = await resp.json();
  } catch (err) {
    el.innerHTML = `<div class="error">Could not load timeline: ${esc(err.message)}</div>`;
    return;
  }

  const r = data.run;
  const decisions = data.policy_decisions.map((d) => `
    <div class="decision">
      <div class="head">
        <span class="type">${esc((d.input && d.input.action) || d.policy_name)}</span>
        <span class="badge ${d.allowed ? "b-green" : "b-red"}">${d.allowed ? "allowed" : "denied"}</span>
        <span class="kv">${when(d.created_at)}</span>
      </div>
      ${renderReasons(d)}
      <details><summary>policy input / decision</summary>
        <pre>${esc(JSON.stringify({ input: d.input, decision: d.decision }, null, 2))}</pre>
      </details>
    </div>`).join("") || '<div class="kv">No policy decisions recorded.</div>';

  const events = data.audit_events.map((e) => `
    <div class="event">
      <div class="when">#${e.sequence} &middot; ${when(e.created_at)}</div>
      <div class="what">
        <span class="type">${esc(e.event_type)}</span>
        <span class="actor">${esc(e.actor_type)}${e.actor_id ? " / " + esc(e.actor_id) : ""}</span>
        ${e.metadata ? `<details><summary>metadata</summary><pre>${esc(JSON.stringify(e.metadata, null, 2))}</pre></details>` : ""}
      </div>
    </div>`).join("") || '<div class="kv">No audit events.</div>';

  const artifacts = data.artifacts.map((a) => `
    <details>
      <summary>${esc(a.artifact_type)} v${a.version} &middot; ${esc(a.content_hash.slice(0, 12))} &middot; ${when(a.created_at)}</summary>
      <pre>${esc(JSON.stringify(a.content, null, 2))}</pre>
    </details>`).join("") || '<div class="kv">No artifacts.</div>';

  const jobs = data.agent_jobs.map((j) => `
    <div class="decision">
      <div class="head">
        <span class="type">${esc(j.provider)}</span>
        ${badge(j.status)}
        <span class="kv">${when(j.started_at)}</span>
      </div>
      <div class="kv"><b>branch</b> ${esc(j.branch || "-")}
        ${j.pr_url ? `&middot; <a href="${esc(j.pr_url)}" style="color:var(--accent)">PR</a>` : ""}
        ${j.error ? `&middot; <span style="color:var(--red)">${esc(j.error)}</span>` : ""}</div>
    </div>`).join("") || '<div class="kv">No agent jobs dispatched.</div>';

  el.innerHTML = `
    <div>
      <span class="key" style="font-size:18px;font-weight:600">${esc(r.linear_issue_key)}</span>
      ${badge(r.status)}
      <div class="kv" style="margin-top:6px">
        <b>run</b> ${esc(r.id)} &middot; <b>risk</b> ${esc(r.risk_level || "unclassified")}
        &middot; <b>mode</b> ${esc(r.agent_mode || "-")}
        &middot; <b>approved by</b> ${esc(r.approved_by || "-")}
        &middot; <b>spend</b> ${fmtBudget(data.budget)}
      </div>
    </div>
    <h2>Policy decisions</h2>${decisions}
    <h2>Agent jobs</h2>${jobs}
    <h2>Audit trail</h2>${events}
    <h2>Artifacts</h2>${artifacts}`;
}

function renderReasons(d) {
  const reasons = (d.decision && d.decision.reasons) || (d.reason ? [d.reason] : []);
  if (!reasons.length) return "";
  return `<ul class="reasons">${reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
}

function dur(seconds) {
  if (seconds == null) return "-";
  if (seconds < 3600) return Math.round(seconds / 60) + "m";
  if (seconds < 86400) return (seconds / 3600).toFixed(1) + "h";
  return (seconds / 86400).toFixed(1) + "d";
}

async function loadFleet() {
  const el = $("#fleet");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/fleet", { headers: authHeaders() });
    if (!resp.ok) { el.style.display = "none"; return; }
    const f = await resp.json();
    const spend = f.active_cost_usd == null ? "-" : "$" + f.active_cost_usd;
    // Oldest wait + SLA breaches turn the bare "awaiting a human" count into an
    // actionable queue signal (issue #37). Both are omitted when nothing waits.
    const oldest = f.awaiting_human
      ? `<span class="stat"><b>${dur(f.oldest_wait_seconds)}</b> oldest wait</span>`
      : "";
    const breaching = f.approvals_breaching_sla
      ? `<span class="stat bad"><b>${f.approvals_breaching_sla}</b> over SLA</span>`
      : "";
    // The machine-side equivalent: oldest in-flight agent run + executions over
    // the execution SLA (issue #37). Both omitted when no agent is running.
    const oldestExec = f.agents_running
      ? `<span class="stat"><b>${dur(f.oldest_execution_seconds)}</b> oldest run</span>`
      : "";
    const execBreaching = f.executions_breaching_sla
      ? `<span class="stat bad"><b>${f.executions_breaching_sla}</b> over SLA</span>`
      : "";
    // The review-side equivalent: oldest open PR awaiting review + PRs over the
    // review SLA (issue #37). Both omitted when no PR is open.
    const oldestReview = f.prs_open
      ? `<span class="stat"><b>${dur(f.oldest_review_seconds)}</b> oldest review</span>`
      : "";
    const reviewBreaching = f.reviews_breaching_sla
      ? `<span class="stat bad"><b>${f.reviews_breaching_sla}</b> over SLA</span>`
      : "";
    // The "stale since last push" cut of the same open PRs: most-idle PR + how
    // many have gone untouched past the staleness SLA (issue #37). Both omitted
    // when no PR is open or the count is zero.
    const reviewsStale = f.reviews_stale
      ? `<span class="stat bad"><b>${f.reviews_stale}</b> stale</span>`
      : "";
    el.innerHTML = `
      <span class="label">Fleet now</span>
      <span class="stat"><b>${f.runs_active}</b> in flight</span>
      <span class="stat"><b>${f.agents_running}</b> agents running</span>
      ${oldestExec}
      ${execBreaching}
      <span class="stat ${f.awaiting_human ? "bad" : ""}"><b>${f.awaiting_human}</b> awaiting a human</span>
      ${oldest}
      ${breaching}
      <span class="stat"><b>${f.prs_open}</b> PRs open</span>
      ${oldestReview}
      ${reviewBreaching}
      ${reviewsStale}
      <span class="stat"><b>${spend}</b> spend in flight</span>
      <span class="stat"><b>${f.total_runs}</b> total runs</span>`;
    el.style.display = "block";
  } catch (err) {
    el.style.display = "none";
  }
}

async function loadApprovals() {
  const el = $("#queue");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/approvals", { headers: authHeaders() });
    if (!resp.ok) { el.style.display = "none"; return; }
    const q = await resp.json();
    const runs = q.runs || [];
    if (!runs.length) { el.style.display = "none"; return; }  // empty queue: hide
    const rows = runs.map((r) => `
      <div class="run ${r.sla_breached ? "breach" : ""}" data-id="${esc(r.run_id)}" title="open timeline">
        <span class="key">${esc(r.linear_issue_key)}</span>${badge(r.status)}
        <span class="age ${r.sla_breached ? "bad" : ""}">${dur(r.waiting_seconds)} waiting</span>
        <div class="meta">${esc(r.run_id)} &middot; ${esc(r.risk_level || "unclassified")} risk
          &middot; ${esc(r.current_step || "-")}</div>
      </div>`).join("");
    const sla = q.sla_seconds
      ? ` &middot; ${q.sla_breaches} of ${q.count} over the ${dur(q.sla_seconds)} SLA`
      : "";
    el.innerHTML = `<details open><summary>approval queue &mdash; ${q.count} parked on a human, oldest first${sla}</summary>${rows}</details>`;
    el.style.display = "block";
    el.querySelectorAll(".run[data-id]").forEach((node) => {
      node.addEventListener("click", () => loadTimeline(node.dataset.id));
    });
  } catch (err) {
    el.style.display = "none";
  }
}

async function loadExecutions() {
  const el = $("#exec-queue");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/executions", { headers: authHeaders() });
    if (!resp.ok) { el.style.display = "none"; return; }
    const q = await resp.json();
    const runs = q.runs || [];
    if (!runs.length) { el.style.display = "none"; return; }  // nothing running: hide
    const rows = runs.map((r) => `
      <div class="run ${r.sla_breached ? "breach" : ""}" data-id="${esc(r.run_id)}" title="open timeline">
        <span class="key">${esc(r.linear_issue_key)}</span>${badge(r.status)}
        <span class="age ${r.sla_breached ? "bad" : ""}">${dur(r.running_seconds)} running</span>
        <div class="meta">${esc(r.run_id)} &middot; ${esc(r.risk_level || "unclassified")} risk
          &middot; ${esc(r.current_step || "-")}</div>
      </div>`).join("");
    const sla = q.sla_seconds
      ? ` &middot; ${q.sla_breaches} of ${q.count} over the ${dur(q.sla_seconds)} SLA`
      : "";
    el.innerHTML = `<details open><summary>execution queue &mdash; ${q.count} agent${q.count === 1 ? "" : "s"} running, oldest first${sla}</summary>${rows}</details>`;
    el.style.display = "block";
    el.querySelectorAll(".run[data-id]").forEach((node) => {
      node.addEventListener("click", () => loadTimeline(node.dataset.id));
    });
  } catch (err) {
    el.style.display = "none";
  }
}

async function loadReviews() {
  const el = $("#review-queue");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/reviews", { headers: authHeaders() });
    if (!resp.ok) { el.style.display = "none"; return; }
    const q = await resp.json();
    const runs = q.runs || [];
    if (!runs.length) { el.style.display = "none"; return; }  // no open PRs: hide
    const rows = runs.map((r) => `
      <div class="run ${r.sla_breached || r.stale_breached ? "breach" : ""}" data-id="${esc(r.run_id)}" title="open timeline">
        <span class="key">${esc(r.linear_issue_key)}</span>${badge(r.status)}
        <span class="age ${r.sla_breached ? "bad" : ""}">${dur(r.unreviewed_seconds)} unreviewed</span>
        <span class="age ${r.stale_breached ? "bad" : ""}">${dur(r.inactive_seconds)} since last push</span>
        <div class="meta">${esc(r.run_id)} &middot; ${esc(r.risk_level || "unclassified")} risk
          &middot; ${esc(r.current_step || "-")}</div>
      </div>`).join("");
    const slaParts = [];
    if (q.sla_seconds) slaParts.push(`${q.sla_breaches} of ${q.count} over the ${dur(q.sla_seconds)} review SLA`);
    if (q.stale_sla_seconds) slaParts.push(`${q.stale_breaches} idle over the ${dur(q.stale_sla_seconds)} staleness SLA`);
    const sla = slaParts.length ? ` &middot; ${slaParts.join(" &middot; ")}` : "";
    el.innerHTML = `<details open><summary>review queue &mdash; ${q.count} PR${q.count === 1 ? "" : "s"} open, oldest first${sla}</summary>${rows}</details>`;
    el.style.display = "block";
    el.querySelectorAll(".run[data-id]").forEach((node) => {
      node.addEventListener("click", () => loadTimeline(node.dataset.id));
    });
  } catch (err) {
    el.style.display = "none";
  }
}

async function loadFailures() {
  const el = $("#failure-queue");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/failures?days=7", { headers: authHeaders() });
    if (!resp.ok) { el.style.display = "none"; return; }
    const q = await resp.json();
    const runs = q.runs || [];
    if (!runs.length) { el.style.display = "none"; return; }  // nothing failed recently: hide
    const rows = runs.map((r) => `
      <div class="run breach" data-id="${esc(r.run_id)}" title="open timeline">
        <span class="key">${esc(r.linear_issue_key)}</span>${badge(r.status)}
        <span class="age bad">${dur(r.failed_seconds)} ago</span>
        <div class="meta">${esc(r.run_id)} &middot; ${esc(r.reason || "no reason recorded")}
          &middot; ${esc(r.risk_level || "unclassified")} risk</div>
      </div>`).join("");
    el.innerHTML = `<details open><summary>needs triage &mdash; ${q.count} run${q.count === 1 ? "" : "s"} failed in the last ${q.days}d (${q.blocked} blocked, ${q.failed} execution-failed), newest first</summary>${rows}</details>`;
    el.style.display = "block";
    el.querySelectorAll(".run[data-id]").forEach((node) => {
      node.addEventListener("click", () => loadTimeline(node.dataset.id));
    });
  } catch (err) {
    el.style.display = "none";
  }
}

async function loadMetrics() {
  const el = $("#metrics");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/delivery?days=90", { headers: authHeaders() });
    if (!resp.ok) { el.style.display = "none"; return; }
    const m = await resp.json();
    const ttm = m.time_to_merge_seconds || {};
    const bands = (m.precision_by_confidence_band || []).map((b) => `
      <tr><td>${esc(b.band)}</td><td>${b.routed}</td><td>${b.merged}</td>
        <td>${Math.round(b.precision * 100)}%</td></tr>`).join("");
    const priors = (m.top_priors || []).map((p) => `
      <tr><td>${esc(p.issue_key_prefix)}</td><td>${esc(p.work_type || "-")}</td>
        <td>${esc(p.repo)}</td><td>${p.merged} of ${p.routed} merged</td></tr>`).join("");
    el.innerHTML = `
      <span class="stat good"><b>${m.prs_shipped}</b> PRs shipped (${m.days}d)</span>
      <span class="stat bad"><b>${m.blocked}</b> blocked</span>
      <span class="stat"><b>${m.retries_consumed}</b> retries</span>
      <span class="stat"><b>${m.escalations}</b> escalations</span>
      <span class="stat"><b>${dur(ttm.median)}</b> median to merge</span>
      <span class="stat"><b>${dur(ttm.p90)}</b> p90</span>
      <span class="stat"><b>${m.total_cost_usd == null ? "-" : "$" + m.total_cost_usd}</b> agent spend</span>
      ${bands || priors ? `<details><summary>routing accuracy &amp; delivery memory</summary>
        ${bands ? `<table><tr><th>confidence band</th><th>routed</th><th>merged</th><th>precision</th></tr>${bands}</table>` : ""}
        ${priors ? `<table><tr><th>team</th><th>work type</th><th>repository</th><th>history</th></tr>${priors}</table>` : ""}
      </details>` : ""}`;
    el.style.display = "block";
  } catch (err) {
    el.style.display = "none";
  }
}

async function loadAgents() {
  const el = $("#agents");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/agents?days=90", { headers: authHeaders() });
    if (!resp.ok) { el.style.display = "none"; return; }
    const m = await resp.json();
    const providers = m.providers || [];
    if (!providers.length) { el.style.display = "none"; return; }
    const rows = providers.map((p) => {
      const cost = p.total_cost_usd == null ? "-" : "$" + p.total_cost_usd;
      const thin = p.meets_min_samples ? "" : " *";
      return `<tr><td>${esc(p.provider)}${thin}</td>
        <td>${p.merged} of ${p.runs} merged</td><td>${p.smoothed_success}</td>
        <td>${p.retries_consumed}</td><td>${cost}</td></tr>`;
    }).join("");
    el.innerHTML = `<details><summary>agent scorecards (90d) &mdash; which agent ships, by the receipts</summary>
      <table><tr><th>provider</th><th>merge rate</th><th>confidence</th>
        <th>retries</th><th>spend</th></tr>${rows}</table>
      <div class="kv">* below the ${m.min_samples}-run minimum sample floor</div>
    </details>`;
    el.style.display = "block";
  } catch (err) {
    el.style.display = "none";
  }
}

function fmtPeriod(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric", month: "short", day: "numeric",
  });
}

async function loadTrends() {
  const el = $("#trends");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/delivery/trends?days=90&bucket=week", {
      headers: authHeaders(),
    });
    if (!resp.ok) { el.style.display = "none"; return; }
    const m = await resp.json();
    const periods = m.periods || [];
    if (!periods.length) { el.style.display = "none"; return; }
    const maxFinished = Math.max(1, ...periods.map((p) => p.runs_finished));
    const rows = periods.map((p) => {
      const shipW = Math.round((p.prs_shipped / maxFinished) * 120);
      const blockW = Math.round((p.blocked / maxFinished) * 120);
      const cost = p.total_cost_usd == null ? "-" : "$" + p.total_cost_usd;
      return `<tr>
        <td>${esc(fmtPeriod(p.period_start))}</td>
        <td class="num">${p.prs_shipped}</td>
        <td class="num">${p.blocked}</td>
        <td class="num">${p.runs_finished}</td>
        <td class="num">${p.retries_consumed}</td>
        <td class="num">${cost}</td>
        <td><span class="bar" style="width:${shipW}px"></span><span class="bar blocked" style="width:${blockW}px"></span></td>
      </tr>`;
    }).join("");
    el.innerHTML = `<details open><summary>delivery trend &mdash; PRs shipped vs blocked, by week (90d)</summary>
      <table><tr><th>week of</th><th>shipped</th><th>blocked</th><th>finished</th>
        <th>retries</th><th>spend</th><th></th></tr>${rows}</table>
    </details>`;
    el.style.display = "block";
  } catch (err) {
    el.style.display = "none";
  }
}

async function loadRepoDelivery() {
  const el = $("#repo-delivery");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/delivery/by-repo?days=90", {
      headers: authHeaders(),
    });
    if (!resp.ok) { el.style.display = "none"; return; }
    const m = await resp.json();
    const repos = m.repos || [];
    if (!repos.length) { el.style.display = "none"; return; }
    const rows = repos.map((r) => {
      const cost = r.total_cost_usd == null ? "-" : "$" + r.total_cost_usd;
      const ttm = r.time_to_merge_seconds || {};
      return `<tr>
        <td>${esc(r.repo)}</td>
        <td class="num">${r.prs_shipped}</td>
        <td class="num">${r.blocked}</td>
        <td class="num">${r.runs_finished}</td>
        <td class="num">${Math.round(r.merge_rate * 100)}%</td>
        <td class="num">${r.retries_consumed}</td>
        <td class="num">${dur(ttm.median)}</td>
        <td class="num">${cost}</td>
      </tr>`;
    }).join("");
    el.innerHTML = `<details><summary>delivery by repo (90d) &mdash; where work ships, stalls, and spends</summary>
      <table><tr><th>repository</th><th>shipped</th><th>blocked</th><th>finished</th>
        <th>merge rate</th><th>retries</th><th>median to merge</th><th>spend</th></tr>${rows}</table>
    </details>`;
    el.style.display = "block";
  } catch (err) {
    el.style.display = "none";
  }
}

async function loadRepoTrends() {
  const el = $("#repo-trends");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/delivery/by-repo/trends?days=90&bucket=week", {
      headers: authHeaders(),
    });
    if (!resp.ok) { el.style.display = "none"; return; }
    const m = await resp.json();
    const repos = m.repos || [];
    if (!repos.length) { el.style.display = "none"; return; }
    // Scale every repo's bars to one shared max so the sparklines are
    // comparable across repos, not each normalised to its own peak.
    const maxShipped = Math.max(1, ...repos.flatMap(
      (r) => (r.series || []).map((c) => c.prs_shipped)));
    const rows = repos.map((r) => {
      const bars = (r.series || []).map((c) => {
        const when = fmtPeriod(c.period_start);
        if (!c.runs_finished) {
          return `<i class="empty" title="${esc(when)}: no runs"></i>`;
        }
        const h = Math.max(2, Math.round((c.prs_shipped / maxShipped) * 24));
        const cost = c.total_cost_usd == null ? "" : `, $${c.total_cost_usd}`;
        return `<i style="height:${h}px" title="${esc(when)}: ${c.prs_shipped} shipped of ${c.runs_finished} finished${cost}"></i>`;
      }).join("");
      const cost = r.total_cost_usd == null ? "-" : "$" + r.total_cost_usd;
      return `<div class="prov">
        <span class="name">${esc(r.repo)}
          <span class="kv">${r.prs_shipped} of ${r.runs_finished} &middot; ${Math.round(r.merge_rate * 100)}% &middot; ${cost}</span></span>
        <span class="spark">${bars}</span></div>`;
    }).join("");
    el.innerHTML = `<details><summary>delivery by repo, trend &mdash; PRs shipped by week (90d), is each repo speeding up or stalling?</summary>
      ${rows}
      <div class="kv">each bar = one week; height = PRs shipped (shared scale across repos)</div>
    </details>`;
    el.style.display = "block";
  } catch (err) {
    el.style.display = "none";
  }
}

async function loadAgentTrends() {
  const el = $("#agent-trends");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/agents/trends?days=90&bucket=week", {
      headers: authHeaders(),
    });
    if (!resp.ok) { el.style.display = "none"; return; }
    const m = await resp.json();
    const providers = m.providers || [];
    if (!providers.length) { el.style.display = "none"; return; }
    const rows = providers.map((p) => {
      const bars = (p.series || []).map((c) => {
        const when = fmtPeriod(c.period_start);
        if (!c.runs) {
          return `<i class="empty" title="${esc(when)}: no runs"></i>`;
        }
        const h = Math.max(2, Math.round((c.smoothed_success / 100) * 24));
        return `<i style="height:${h}px" title="${esc(when)}: ${c.merged} of ${c.runs} merged (conf ${c.smoothed_success})"></i>`;
      }).join("");
      const thin = p.meets_min_samples ? "" : " *";
      return `<div class="prov">
        <span class="name">${esc(p.provider)}${thin}
          <span class="kv">${p.merged} of ${p.runs} &middot; conf ${p.smoothed_success}</span></span>
        <span class="spark">${bars}</span></div>`;
    }).join("");
    el.innerHTML = `<details><summary>agent trend &mdash; merge confidence by week (90d), is each agent improving?</summary>
      ${rows}
      <div class="kv">each bar = one week; height = smoothed merge rate &middot; * below the ${m.min_samples}-run minimum</div>
    </details>`;
    el.style.display = "block";
  } catch (err) {
    el.style.display = "none";
  }
}

async function loadEpics() {
  const el = $("#epics");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("epics", { headers: authHeaders() });
    if (!resp.ok) { el.style.display = "none"; return; }
    const data = await resp.json();
    const epics = data.epics || [];
    if (!epics.length) { el.style.display = "none"; return; }  // no epics: hide
    const rows = epics.map((e) => {
      const r = e.run;
      const ro = e.rollup || {};
      const c = ro.counts || {};
      const kids = (e.children || []).map((ch) => `
        <span class="epic-child" data-id="${esc(ch.id)}" title="open timeline">
          ${esc(ch.linear_issue_key)}${badge(ch.status)}</span>`).join("");
      return `<div class="epic">
        <div class="epic-head">
          <span class="key" data-id="${esc(r.id)}" title="open timeline">${esc(r.linear_issue_key)}</span>
          <span class="badge ${EPIC_BADGE[ro.status] || "b-muted"}">${esc(ro.status)}</span>
          <span class="kv">${ro.total || 0} child runs &middot;
            ${c.complete || 0} merged &middot; ${c.active || 0} in flight &middot;
            ${c.unsuccessful || 0} unsuccessful</span>
        </div>
        <div class="epic-kids">${kids || '<span class="kv">no child runs yet</span>'}</div>
      </div>`;
    }).join("");
    el.innerHTML = `<details open><summary>epics &mdash; multi-repo runs rolled up (${epics.length})</summary>${rows}</details>`;
    el.style.display = "block";
    el.querySelectorAll("[data-id]").forEach((node) => {
      node.addEventListener("click", () => loadTimeline(node.dataset.id));
    });
  } catch (err) {
    el.style.display = "none";
  }
}

// The effective policy gate this deployment resolves to (issue #31) - the
// in-app twin of `foundry-policy explain`. Read-only; surfaces what the gate
// enforces (threshold, protected paths, required-approval roles/counts, caps)
// so an auditor can see the gate without CLI access.
function policyList(obj, render) {
  const keys = Object.keys(obj || {});
  if (!keys.length) return '<span class="kv">none</span>';
  return keys.map((k) => `<div class="kv">${esc(k)}: ${render(obj[k])}</div>`).join("");
}

async function loadPolicy() {
  const el = $("#policy");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/policy", { headers: authHeaders() });
    if (!resp.ok) { el.style.display = "none"; return; }
    const m = await resp.json();
    const p = m.policy;
    if (!m.configured || !p) { el.style.display = "none"; return; }  // not built from a config
    const globs = (p.forbidden_globs || []);
    const cap = p.max_cost_per_run == null ? "no cap" : "$" + p.max_cost_per_run;
    const freezes = (p.change_freeze_windows || []);
    const rows = [
      ["policy backend", esc(m.provider || "local")],
      ["repo confidence threshold", esc(p.repo_confidence_threshold)],
      ["max files changed", p.max_files_changed == null ? "no cap" : esc(p.max_files_changed)],
      ["min approvals (two-person rule)", esc(p.min_approvals)],
      ["per-repo min approvals", policyList(p.repo_min_approvals, (v) => esc(v))],
      ["forbidden paths", `${globs.length} &mdash; ` + (globs.map(esc).join(", ") || "none")],
      ["per-repo forbidden paths", policyList(p.repo_forbidden_globs, (v) => esc((v || []).join(", ")))],
      ["per-repo required roles", policyList(p.repo_required_roles, (v) => esc((v || []).join(", ")))],
      ["per-path required roles", policyList(p.path_required_roles, (v) => esc((v || []).join(", ")))],
      ["change-freeze windows", freezes.length ? freezes.map((w) => `<div class="kv">${esc(w)}</div>`).join("") : '<span class="kv">none</span>'],
      ["max agent retries", esc(p.max_agent_retries)],
      ["max cost per run", esc(cap)],
      ["configured approvers", esc(p.approver_count)],
    ];
    const body = rows.map(
      ([k, v]) => `<tr><td>${esc(k)}</td><td>${v}</td></tr>`).join("");
    // The one live, time-dependent signal: is a change freeze in effect right
    // now? Evaluated server-side at request time; surfaced both in the summary
    // (visible without expanding) and as a banner inside the panel.
    const fz = m.active_freeze;
    const summaryBadge = fz ? ' <span class="badge b-red">CHANGE FREEZE ACTIVE</span>' : '';
    const freezeBanner = fz
      ? `<div class="kv"><span class="badge b-red">CHANGE FREEZE ACTIVE</span> ${esc(fz.description)}${fz.reason ? ' &mdash; ' + esc(fz.reason) : ''} &mdash; autonomous re-dispatch is held for a human while active</div>`
      : '';
    el.innerHTML = `<details><summary>policy gate &mdash; what this deployment enforces (the in-app twin of <code>foundry-policy explain</code>)${summaryBadge}</summary>
      ${freezeBanner}
      <table>${body}</table>
      <div class="kv">read-only view of the effective gate; built-ins are a non-overridable floor &mdash; config can only make it stricter</div>
    </details>`;
    el.style.display = "block";
  } catch (err) {
    el.style.display = "none";
  }
}

// The compliance verdict of the live gate against the configured baseline
// (issue #31) - the always-on, in-app twin of `foundry-policy check --against`.
// Read-only; shows whether the gate is at least as strict as the committed
// compliance baseline (and where it has drifted below it), so an auditor sees
// drift on the dashboard without running the CLI in CI.
async function loadPolicyCheck() {
  const el = $("#policy-check");
  if (!hasAuth()) {
    el.style.display = "none";  // unauthenticated: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/policy/check", { headers: authHeaders() });
    if (!resp.ok) { el.style.display = "none"; return; }
    const m = await resp.json();
    if (!m.configured) { el.style.display = "none"; return; }  // no baseline configured
    const findings = m.findings || [];
    const verdict = m.ok
      ? '<span class="badge b-green">PASS</span>'
      : `<span class="badge b-red">FAIL</span> <span class="kv">weaker on ${(m.weaknesses || []).length} control(s)</span>`;
    const body = findings.map((f) => {
      const mark = f.ok
        ? '<span class="badge b-green">PASS</span>'
        : '<span class="badge b-red">FAIL</span>';
      return `<tr><td>${mark}</td><td>${esc(f.knob)}</td><td class="kv">${esc(f.detail)}</td></tr>`;
    }).join("");
    el.innerHTML = `<details><summary>compliance check &mdash; is the gate at least as strict as baseline <code>${esc(m.baseline)}</code>? (the in-app twin of <code>foundry-policy check</code>)</summary>
      <div style="padding:6px 0">result: ${verdict}</div>
      <table>${body}</table>
      <div class="kv">read-only continuous compliance verdict; higher/lower/superset is "stricter" per the gate's direction &mdash; the check labels drift, it blocks no run</div>
    </details>`;
    el.style.display = "block";
  } catch (err) {
    el.style.display = "none";
  }
}

function refresh() {
  loadRuns();
  loadFleet();
  loadApprovals();
  loadExecutions();
  loadReviews();
  loadFailures();
  loadMetrics();
  loadTrends();
  loadAgents();
  loadAgentTrends();
  loadRepoDelivery();
  loadRepoTrends();
  loadEpics();
  loadPolicy();
  loadPolicyCheck();
}

$("#save").addEventListener("click", () => {
  localStorage.setItem("foundry_token", tokenInput.value.trim());
  refresh();
  $("#detail").innerHTML = '<div class="empty">Select a run to see its full decision timeline.</div>';
});

initSso();
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


def render_dashboard(*, oidc_login: bool, session: bool) -> str:
    """The dashboard page with the SSO flags injected (issue #34).

    ``oidc_login`` toggles the "Sign in with SSO" button; ``session`` tells the
    page it is already authenticated by a session cookie (so it skips the pasted
    token UX and renders read panels). Both are emitted as JS booleans into a
    placeholder; nothing user-controlled is interpolated, so there is no
    injection surface.
    """
    return DASHBOARD_HTML.replace(
        "%%OIDC_LOGIN%%", "true" if oidc_login else "false"
    ).replace("%%SESSION%%", "true" if session else "false")
