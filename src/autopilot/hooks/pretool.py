"""
Claude Code PreToolUse hook — invoked as: python3 -m autopilot.hooks.pretool
Enforces hard constraints: step limit, bash whitelist, Agent spawn gate, budget gate.
Exit 0 = allow. Exit 2 = block (Claude sees the reason).
"""
import json
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path.home() / ".autopilot" / ".env")

from autopilot.config import get_project_id, constraints
from autopilot.tracker import init_db, load_active_run, save_run, save_subagent_event, get_cost_today
from autopilot.observe import trace_subagent


def block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(2)


def allow() -> None:
    sys.exit(0)


def main() -> None:
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

    # ── Agent spawn gate ─────────────────────────────────────────────────────
    if tool_name == "Agent":
        allowed_phases = c.get("agent_spawns_allowed_in", ["plan"])
        budget_gate = float(c.get("budget_gate_usd", 2.0))
        no_spawn = os.getenv("AP_NO_SPAWN", "0") == "1"
        today_cost = get_cost_today(project)

        if no_spawn:
            reason = "AP_NO_SPAWN=1: subagent spawning disabled for this session"
            save_subagent_event(session_id, run_id, project, phase, "", False, reason)
            trace_subagent(session_id, run_id, project, phase, False, reason)
            block(reason)

        if phase not in allowed_phases:
            reason = f"Subagent spawn blocked: phase '{phase}' not in allowed phases {allowed_phases}. Iterate in the main loop instead."
            save_subagent_event(session_id, run_id, project, phase, "", False, reason)
            trace_subagent(session_id, run_id, project, phase, False, reason)
            block(reason)

        if today_cost >= budget_gate:
            reason = f"Budget gate: today's cost ${today_cost:.2f} >= ${budget_gate:.2f} limit. Subagent spawn blocked."
            save_subagent_event(session_id, run_id, project, phase, "", False, reason)
            trace_subagent(session_id, run_id, project, phase, False, reason)
            block(reason)

        # Allowed — log it
        save_subagent_event(session_id, run_id, project, phase, str(tool_input), True)
        trace_subagent(session_id, run_id, project, phase, True)

    # ── Bash command whitelist ────────────────────────────────────────────────
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", "")).strip()
        allowed_cmds = c.get("allowed_bash_commands", [])
        base_cmd = cmd.split()[0] if cmd else ""
        # strip path prefix: /usr/bin/git → git
        base_cmd = base_cmd.split("/")[-1]
        if allowed_cmds and base_cmd and base_cmd not in allowed_cmds:
            block(f"Bash command '{base_cmd}' not in allowed_bash_commands whitelist.")

    # ── Step counter ──────────────────────────────────────────────────────────
    if run and tool_name not in ("", "Agent"):
        max_steps = run.max_steps or c.get("max_steps_per_run", 20)
        if run.current_step >= max_steps:
            block(f"Step limit reached ({run.current_step}/{max_steps}). Stop and summarize progress.")
        run.current_step += 1
        save_run(run)

    allow()


if __name__ == "__main__":
    main()
