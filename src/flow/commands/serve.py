"""flow serve — local FastAPI dashboard on :7331."""
import os

from rich.console import Console

console = Console()

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Flow</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace; background: #0d1117; color: #e6edf3; padding: 2rem; }
  h1 { font-size: 1.25rem; color: #58a6ff; margin-bottom: 1.5rem; letter-spacing: 0.05em; }
  h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.1em; color: #8b949e; margin: 1.5rem 0 0.75rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1.25rem; }
  .card .label { font-size: 0.7rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.4rem; }
  .card .value { font-size: 1.5rem; font-weight: 600; color: #e6edf3; }
  .card .sub { font-size: 0.75rem; color: #8b949e; margin-top: 0.25rem; }
  .bar-wrap { background: #21262d; border-radius: 4px; height: 6px; margin-top: 0.5rem; overflow: hidden; }
  .bar { height: 100%; border-radius: 4px; background: #238636; transition: width 0.3s; }
  .bar.warn { background: #d29922; }
  .bar.danger { background: #da3633; }
  table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
  th { text-align: left; color: #8b949e; font-weight: 500; padding: 0.5rem 0.75rem; border-bottom: 1px solid #21262d; }
  td { padding: 0.5rem 0.75rem; border-bottom: 1px solid #161b22; color: #c9d1d9; }
  tr:hover td { background: #161b22; }
  .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 12px; font-size: 0.7rem; font-weight: 500; }
  .badge-active { background: #0d4429; color: #3fb950; }
  .badge-complete { background: #0c2d6b; color: #58a6ff; }
  .badge-failed { background: #4b0000; color: #f85149; }
  .badge-blocked { background: #3d2b00; color: #d29922; }
  .phase { font-size: 0.7rem; color: #58a6ff; text-transform: uppercase; }
  #refresh { font-size: 0.7rem; color: #8b949e; float: right; cursor: pointer; background: none; border: none; color: #8b949e; }
  #refresh:hover { color: #58a6ff; }
  .section-label { font-size: 0.65rem; color: #58a6ff; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 0.3rem; }
</style>
</head>
<body>
<h1>⚡ AI Flow <button id="refresh" onclick="load()">↻ refresh</button></h1>
<div id="app"><p style="color:#8b949e">Loading...</p></div>
<script>
async function load() {
  const [status, stats, runs] = await Promise.all([
    fetch('/status').then(r=>r.json()),
    fetch('/stats').then(r=>r.json()),
    fetch('/runs').then(r=>r.json()),
  ]);

  const apiGate = status.api_spend_gate_usd || 1.0;
  const apiPct = Math.min((status.api_spend_today / apiGate) * 100, 100);
  const apiBarClass = apiPct > 80 ? 'danger' : apiPct > 50 ? 'warn' : '';

  const quota = status.quota || {};
  const msgCap = quota.msg_cap || 0;
  const msgsUsed = quota.msgs_used || 0;
  const quotaPct = msgCap > 0 ? Math.min((msgsUsed / msgCap) * 100, 100) : 0;
  const quotaBarClass = quotaPct > 80 ? 'danger' : quotaPct > 50 ? 'warn' : '';

  let activeHtml = '';
  if (status.active_run) {
    const r = status.active_run;
    const runPct = r.max_steps > 0 ? Math.min((r.current_step / r.max_steps) * 100, 100) : 0;
    const projected = r.current_step > 0 ? (r.cost_usd / r.current_step * r.max_steps).toFixed(4) : '—';
    activeHtml = `
      <h2>Active run</h2>
      <div class="card" style="grid-column:1/-1">
        <div class="label">${r.run_id} &middot; <span class="phase">${r.phase}</span></div>
        <div class="value" style="font-size:1rem;margin-top:0.25rem">${r.goal.substring(0,100)}</div>
        <div class="sub">Step ${r.current_step}/${r.max_steps} &middot; API: $${r.cost_usd.toFixed(4)} &middot; ~$${projected} projected</div>
        <div class="sub">Subscription: ${r.subscription_msgs} msgs &middot; ${((r.subscription_tokens_in||0)+(r.subscription_tokens_out||0)).toLocaleString()} tokens</div>
        <div class="bar-wrap"><div class="bar ${runPct>80?'warn':''}" style="width:${runPct}%"></div></div>
      </div>`;
  }

  const projectRows = (stats.projects||[]).map(p => `
    <tr>
      <td>${p.project}</td>
      <td>${p.sessions}</td>
      <td>$${(p.api_spend||0).toFixed(4)}</td>
      <td>${((p.sub_tokens||0)).toLocaleString()}</td>
      <td>${(p.last_active||'').substring(0,10)}</td>
    </tr>`).join('');

  const runRows = (runs||[]).map(r => {
    const badge = `badge-${r.status}`;
    return `<tr>
      <td style="font-family:monospace;font-size:0.75rem">${r.run_id}</td>
      <td>${r.goal.substring(0,50)}</td>
      <td><span class="phase">${r.phase}</span></td>
      <td><span class="badge ${badge}">${r.status}</span></td>
      <td>$${r.cost_usd.toFixed(4)}</td>
      <td>${r.subscription_msgs||0}</td>
      <td>${(r.updated_at||'').substring(0,10)}</td>
    </tr>`;}).join('');

  const quotaCard = msgCap > 0 ? `
    <div class="card">
      <div class="section-label">Subscription</div>
      <div class="label">5h window quota (${quota.plan||'pro'})</div>
      <div class="value">${msgsUsed}<span style="font-size:1rem;color:#8b949e">/${msgCap}</span></div>
      <div class="sub">msgs used &middot; ${quotaPct.toFixed(0)}% of window</div>
      <div class="bar-wrap"><div class="bar ${quotaBarClass}" style="width:${quotaPct}%"></div></div>
    </div>` : `
    <div class="card">
      <div class="section-label">Subscription</div>
      <div class="label">Quota</div>
      <div class="value" style="font-size:1rem;margin-top:0.3rem">${quota.plan||'api_only'}</div>
      <div class="sub">No window cap to track</div>
    </div>`;

  document.getElementById('app').innerHTML = `
    <div class="grid">
      ${quotaCard}
      <div class="card">
        <div class="section-label">API utility calls</div>
        <div class="label">Spend today (this project)</div>
        <div class="value">$${status.api_spend_today.toFixed(4)}</div>
        <div class="sub">${apiPct.toFixed(0)}% of $${apiGate} gate</div>
        <div class="bar-wrap"><div class="bar ${apiBarClass}" style="width:${apiPct}%"></div></div>
      </div>
      <div class="card">
        <div class="section-label">API utility calls</div>
        <div class="label">Spend today (all projects)</div>
        <div class="value">$${status.api_spend_all.toFixed(4)}</div>
        <div class="sub">clarify + ship + ci-review</div>
      </div>
      <div class="card">
        <div class="label">Active project</div>
        <div class="value" style="font-size:1rem;margin-top:0.3rem">${status.project}</div>
      </div>
    </div>
    ${activeHtml}
    <h2>By project</h2>
    <table>
      <thead><tr><th>Project</th><th>Sessions</th><th>API spend</th><th>Sub tokens</th><th>Last active</th></tr></thead>
      <tbody>${projectRows || '<tr><td colspan="5" style="color:#8b949e">No data yet</td></tr>'}</tbody>
    </table>
    <h2>Recent runs</h2>
    <table>
      <thead><tr><th>ID</th><th>Goal</th><th>Phase</th><th>Status</th><th>API spend</th><th>Sub msgs</th><th>Updated</th></tr></thead>
      <tbody>${runRows || '<tr><td colspan="7" style="color:#8b949e">No runs yet</td></tr>'}</tbody>
    </table>`;
}
load();
</script>
</body>
</html>"""


def cmd_serve(port: int = 7331) -> None:
    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, JSONResponse
        import uvicorn
    except ImportError:
        console.print("[red]fastapi and uvicorn are required: pip install fastapi uvicorn[/red]")
        raise SystemExit(1)

    from autopilot.tracker import (
        init_db, get_api_spend_today, get_project_stats, get_recent_runs,
        load_active_run, get_window_usage,
    )
    from autopilot.config import get_project_id, constraints, get_plan, get_plan_window_caps

    init_db()
    app = FastAPI(title="AI Flow", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        return _HTML

    @app.get("/status")
    async def status():
        project = get_project_id()
        run = load_active_run(project)
        c = constraints()
        plan = get_plan()
        caps = get_plan_window_caps()
        api_gate = float(os.getenv("AP_BUDGET_USD") or c.get("api_spend_gate_usd", 1.0))
        window = get_window_usage(plan)
        msg_cap = caps.get(plan, {}).get("msgs", 0)
        api_today = get_api_spend_today(project)
        api_today_all = get_api_spend_today()

        active = None
        if run:
            projected = None
            if run.current_step > 0:
                projected = round(run.cost_usd / run.current_step * run.max_steps, 6)
            active = {
                "run_id": run.run_id,
                "goal": run.goal,
                "phase": run.phase.value,
                "current_step": run.current_step,
                "max_steps": run.max_steps,
                "cost_usd": run.cost_usd,
                "projected_usd": projected,
                "status": run.status.value,
                "plan_steps": run.plan_steps,
                "subscription_msgs": run.subscription_msgs,
                "subscription_tokens_in": run.subscription_tokens_in,
                "subscription_tokens_out": run.subscription_tokens_out,
            }

        return JSONResponse({
            "project": project,
            "api_spend_today": api_today,
            "api_spend_all": api_today_all,
            "api_spend_gate_usd": api_gate,
            "quota": {
                "plan": plan,
                "msgs_used": window["msgs_used"],
                "msg_cap": msg_cap,
                "tokens_in": window["tokens_in"],
                "tokens_out": window["tokens_out"],
                "window_start": window["window_start"],
            },
            "active_run": active,
        })

    @app.get("/stats")
    async def stats():
        return JSONResponse({"projects": get_project_stats()})

    @app.get("/runs")
    async def runs(limit: int = 20, project: str = None):
        return JSONResponse(get_recent_runs(project, limit=limit))

    console.print(f"[bold cyan]AI Flow dashboard[/bold cyan] → http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
