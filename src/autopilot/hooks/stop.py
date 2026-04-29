"""
Claude Code Stop hook — invoked as: python3 -m autopilot.hooks.stop
Reads session data from hook payload, writes to DuckDB + Langfuse.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path.home() / ".autopilot" / ".env")

from autopilot.config import get_project_id, get_branch
from autopilot.tracker import init_db, save_session, load_active_run, save_run
from autopilot.observe import trace_session

# MODEL COST TABLE (per 1M tokens, USD)
COSTS = {
    "claude-opus-4-5":           {"in": 15.0,  "out": 75.0},
    "claude-opus-4-7":           {"in": 15.0,  "out": 75.0},
    "claude-sonnet-4-6":         {"in": 3.0,   "out": 15.0},
    "claude-sonnet-4-5":         {"in": 3.0,   "out": 15.0},
    "claude-haiku-4-5-20251001": {"in": 0.8,   "out": 4.0},
    "claude-haiku-4-5":          {"in": 0.8,   "out": 4.0},
}


def calc_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    rates = COSTS.get(model, {"in": 3.0, "out": 15.0})
    return (tokens_in * rates["in"] + tokens_out * rates["out"]) / 1_000_000


def main() -> None:
    init_db()

    payload = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except Exception:
        pass

    project = get_project_id()
    branch = get_branch()
    session_id = payload.get("session_id") or str(uuid.uuid4())[:8]

    usage = payload.get("usage", {})
    tokens_in = usage.get("input_tokens", 0)
    tokens_out = usage.get("output_tokens", 0)
    model = payload.get("model", "claude-sonnet-4-6")
    context_tokens = usage.get("cache_read_input_tokens", 0) + tokens_in
    cost = calc_cost(model, tokens_in, tokens_out)

    run = load_active_run(project)
    run_id = run.run_id if run else "none"
    phase = run.phase.value if run else "unknown"

    save_session(
        session_id=session_id,
        run_id=run_id,
        project=project,
        branch=branch,
        phase=phase,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        context_tokens=context_tokens,
    )

    if run:
        run.cost_usd += cost
        save_run(run)

    trace_session(
        session_id=session_id,
        run_id=run_id,
        project=project,
        branch=branch,
        phase=phase,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        context_tokens=context_tokens,
    )


if __name__ == "__main__":
    main()
