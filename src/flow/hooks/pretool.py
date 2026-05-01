"""
Claude Code PreToolUse hook — invoked as: python3 -m flow.hooks.pretool
Enforces hard constraints: step limit, bash whitelist, Agent spawn gate, budget gate.
Exit 0 = allow. Exit 2 = block (Claude sees the reason).

Billing surfaces:
  subscription — Claude Code runs against claude.ai Pro/Max login.
    Agent spawn gated on: AP_NO_SPAWN flag + api_spend_gate_usd (utility $).
    Quota warnings emitted via stderr when nearing 5-hour window limit.
  api (AP_FORCE_API_KEY=1) — Claude Code bills via ANTHROPIC_API_KEY.
    Agent spawn gated on: AP_NO_SPAWN flag + api_spend_gate_usd (real $).
"""
import json
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path.home() / ".autopilot" / ".env")

from flow.config import get_project_id, constraints, get_plan, get_plan_window_caps
from flow.tracker import (
    Phase, init_db, load_active_run, save_run, save_subagent_event,
    get_api_spend_today, get_window_usage,
)
from flow.observe import trace_subagent


def _parse_plan_steps(plan_text: str) -> list:
    """Parse numbered list items from Claude's ExitPlanMode plan_text."""
    import re
    steps = []
    for line in plan_text.splitlines():
        m = re.match(r"^\s*(\d+)[.)]\s+(.+)", line)
        if m:
            steps.append({"id": m.group(1), "description": m.group(2).strip(), "status": "pending"})
    return steps


def block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(2)


def allow() -> None:
    sys.exit(0)


def main() -> None:
    if os.getenv("AP_ACTIVE") != "1":
        sys.exit(0)

    init_db()

    payload = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except Exception:
        pass

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    project = get_project_id()
    c = constraints()

    run = load_active_run(project)
    run_id = run.run_id if run else "none"
    phase = run.phase.value if run else "unknown"
    session_id = payload.get("session_id", str(uuid.uuid4())[:8])

    # ── ExitPlanMode — persist plan steps and advance to execute ─────────────
    if tool_name == "ExitPlanMode":
        plan_text = tool_input.get("plan_text", "")
        if run and plan_text:
            steps = _parse_plan_steps(plan_text)
            if steps:
                from flow.run_manager import set_plan_steps, advance_phase
                set_plan_steps(run, steps)
                advance_phase(run, Phase.execute)
        allow()

    # ── Agent spawn gate ─────────────────────────────────────────────────────
    if tool_name == "Agent":
        allowed_phases = c.get("agent_spawns_allowed_in", ["plan"])
        api_spend_gate = float(os.getenv("AP_BUDGET_USD") or c.get("api_spend_gate_usd", 1.0))
        no_spawn = os.getenv("AP_NO_SPAWN", "0") == "1"
        api_spend_today = get_api_spend_today(project)

        if no_spawn:
            reason = "AP_NO_SPAWN=1: subagent spawning disabled for this session"
            save_subagent_event(session_id, run_id, project, phase, "", False, reason)
            trace_subagent(session_id, run_id, project, phase, False, reason)
            block(reason)

        if phase not in allowed_phases:
            reason = (
                f"Subagent spawn blocked: phase '{phase}' not in allowed phases {allowed_phases}. "
                "Iterate in the main loop instead."
            )
            save_subagent_event(session_id, run_id, project, phase, "", False, reason)
            trace_subagent(session_id, run_id, project, phase, False, reason)
            block(reason)

        if api_spend_today >= api_spend_gate:
            reason = (
                f"API spend gate: today's flow utility spend ${api_spend_today:.2f} >= "
                f"${api_spend_gate:.2f} limit. Subagent spawn blocked."
            )
            save_subagent_event(session_id, run_id, project, phase, "", False, reason)
            trace_subagent(session_id, run_id, project, phase, False, reason)
            block(reason)

        # Allowed — log it and emit a quota warning if nearing 5-hour window limit
        save_subagent_event(session_id, run_id, project, phase, str(tool_input), True)
        trace_subagent(session_id, run_id, project, phase, True)
        _maybe_warn_quota(c)

    # ── Bash command whitelist ────────────────────────────────────────────────
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", "")).strip()
        allowed_cmds = c.get("allowed_bash_commands", [])
        base_cmd = cmd.split()[0] if cmd else ""
        base_cmd = base_cmd.split("/")[-1]
        if allowed_cmds and base_cmd and base_cmd not in allowed_cmds:
            block(f"Bash command '{base_cmd}' not in allowed_bash_commands whitelist.")

    # ── Step counter (weighted) ───────────────────────────────────────────────
    if run and tool_name not in ("", "Agent"):
        tool_weights = c.get("tool_weights", {})
        weight = float(tool_weights.get(tool_name, tool_weights.get("default", 1.0)))

        phase_budgets = c.get("phase_step_budgets", {})
        phase_budget = phase_budgets.get(run.phase.value)
        effective_max = float(
            phase_budget if phase_budget is not None
            else (run.max_steps or c.get("max_steps_per_run", 20))
        )

        if run.step_budget_used >= effective_max:
            block(
                f"Step budget exhausted ({run.step_budget_used:.1f}/{effective_max:.0f} weighted steps). "
                "Stop and summarize progress."
            )

        run.current_step += 1
        run.step_budget_used += weight
        save_run(run)

    allow()


def _maybe_warn_quota(c: dict) -> None:
    """Print a stderr warning if the subscription quota window is nearly full."""
    warn_pct = float(c.get("subscription_quota_warn_pct", 0.80))
    plan = get_plan()
    caps = get_plan_window_caps()
    msg_cap = caps.get(plan, {}).get("msgs", 0)
    if not msg_cap:
        return
    window = get_window_usage(plan)
    used_pct = window["msgs_used"] / msg_cap
    if used_pct >= warn_pct:
        print(
            f"[flow warn] Subscription quota: {window['msgs_used']}/{msg_cap} msgs used "
            f"({used_pct*100:.0f}%) in current 5-hour window.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
