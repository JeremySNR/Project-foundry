"""The Foundry dashboard: one static page, zero build step, zero new deps.

Read-only visibility over the audit data that already exists: the run list and,
per run, the full decision timeline (artifacts, audit events, policy decisions,
agent jobs). All data comes from ``GET /runs`` and ``GET /runs/{id}/timeline``;
the timeline call carries the bearer token the user pastes once (kept in
localStorage, never sent anywhere but this API).

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
</style>
</head>
<body>
<header>
  <h1><span>Foundry</span> run dashboard</h1>
  <div class="spacer"></div>
  <input id="token" type="password" placeholder="API token (stored locally)">
  <button id="save">Connect</button>
</header>
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
  analysing: "b-muted", blocked: "b-red", rejected: "b-red", failed: "b-red",
  stopped: "b-muted",
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

async function loadRuns() {
  const el = $("#runs");
  try {
    const resp = await fetch("runs", { headers: authHeaders() });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    if (!data.runs.length) {
      el.innerHTML = '<div class="run"><span class="meta">No runs yet.</span></div>';
      return;
    }
    el.innerHTML = data.runs
      .slice()
      .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))
      .map((r) => `
        <div class="run" data-id="${esc(r.id)}">
          <span class="key">${esc(r.linear_issue_key)}</span>${badge(r.status)}
          <div class="meta">${esc(r.id)} &middot; ${esc(r.risk_level || "unclassified")} risk
            &middot; ${when(r.created_at)}</div>
        </div>`)
      .join("");
    el.querySelectorAll(".run[data-id]").forEach((node) => {
      node.addEventListener("click", () => {
        el.querySelectorAll(".run").forEach((n) => n.classList.remove("active"));
        node.classList.add("active");
        loadTimeline(node.dataset.id);
      });
    });
  } catch (err) {
    el.innerHTML = `<div class="error">Could not load runs: ${esc(err.message)}</div>`;
  }
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

$("#save").addEventListener("click", () => {
  localStorage.setItem("foundry_token", tokenInput.value.trim());
  loadRuns();
  $("#detail").innerHTML = '<div class="empty">Select a run to see its full decision timeline.</div>';
});

loadRuns();
setInterval(loadRuns, 15000);
</script>
</body>
</html>
"""
