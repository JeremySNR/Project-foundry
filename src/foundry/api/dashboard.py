"""The Foundry dashboard: one static page, zero build step, zero new deps.

Read-only visibility over the audit data that already exists: a live fleet
strip (runs in flight / approval queue / spend in flight, from the current run
states), the delivery metrics strip, a delivery-trend-over-time table, the
agent scorecards, the run list (with an approval-queue filter) and, per run,
the full decision timeline (artifacts, audit events, policy decisions, agent
jobs). All data comes from ``GET /runs``, ``GET /metrics/fleet``,
``GET /metrics/delivery``, ``GET /metrics/delivery/trends``,
``GET /metrics/agents`` and ``GET /runs/{id}/timeline``; the calls carry the
bearer token the user pastes once (kept in localStorage, never sent anywhere
but this API).

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
  #fleet, #metrics, #agents, #trends {
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
  #agents summary, #trends summary { color: var(--text); cursor: pointer; }
  #trends td.num { text-align: right; font-variant-numeric: tabular-nums; }
  #trends .bar {
    display: inline-block; height: 8px; background: var(--green);
    border-radius: 2px; vertical-align: middle; min-width: 1px;
  }
  #trends .bar.blocked { background: var(--red); }
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
</style>
</head>
<body>
<header>
  <h1><span>Foundry</span> run dashboard</h1>
  <div class="spacer"></div>
  <input id="token" type="password" placeholder="API token (stored locally)">
  <button id="save">Connect</button>
</header>
<div id="fleet"></div>
<div id="metrics"></div>
<div id="trends"></div>
<div id="agents"></div>
<main>
  <div id="runs"></div>
  <div id="detail"><div class="empty">Select a run to see its full decision timeline.</div></div>
</main>
<script>
"use strict";
const $ = (s) => document.querySelector(s);
const tokenInput = $("#token");
tokenInput.value = localStorage.getItem("foundry_token") || "";

const STATUS_BADGE = {
  complete: "b-green", pr_open: "b-blue", agent_running: "b-purple",
  waiting_approval: "b-amber", review_required: "b-amber",
  needs_clarification: "b-amber", approved: "b-blue", plan_ready: "b-blue",
  analysing: "b-muted", blocked: "b-red", rejected: "b-red",
  execution_failed: "b-red",
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
      el.innerHTML = '<div class="error">Unauthorised. Paste the API token above and press Connect.</div>';
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
  if (!localStorage.getItem("foundry_token")) {
    el.style.display = "none";  // no token: skip the call, it can only 401
    return;
  }
  try {
    const resp = await fetch("metrics/fleet", { headers: authHeaders() });
    if (!resp.ok) { el.style.display = "none"; return; }
    const f = await resp.json();
    const spend = f.active_cost_usd == null ? "-" : "$" + f.active_cost_usd;
    el.innerHTML = `
      <span class="label">Fleet now</span>
      <span class="stat"><b>${f.runs_active}</b> in flight</span>
      <span class="stat"><b>${f.agents_running}</b> agents running</span>
      <span class="stat ${f.awaiting_human ? "bad" : ""}"><b>${f.awaiting_human}</b> awaiting a human</span>
      <span class="stat"><b>${f.prs_open}</b> PRs open</span>
      <span class="stat"><b>${spend}</b> spend in flight</span>
      <span class="stat"><b>${f.total_runs}</b> total runs</span>`;
    el.style.display = "block";
  } catch (err) {
    el.style.display = "none";
  }
}

async function loadMetrics() {
  const el = $("#metrics");
  if (!localStorage.getItem("foundry_token")) {
    el.style.display = "none";  // no token: skip the call, it can only 401
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
  if (!localStorage.getItem("foundry_token")) {
    el.style.display = "none";  // no token: skip the call, it can only 401
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
  if (!localStorage.getItem("foundry_token")) {
    el.style.display = "none";  // no token: skip the call, it can only 401
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

function refresh() {
  loadRuns();
  loadFleet();
  loadMetrics();
  loadTrends();
  loadAgents();
}

$("#save").addEventListener("click", () => {
  localStorage.setItem("foundry_token", tokenInput.value.trim());
  refresh();
  $("#detail").innerHTML = '<div class="empty">Select a run to see its full decision timeline.</div>';
});

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""
