"""
Claude Code Stop hook — invoked as: python3 -m autopilot.hooks.stop
Reads session data from hook payload, writes to DuckDB + Langfuse.

Two billing surfaces:
  subscription (default) — Claude Code runs against claude.ai Pro/Max login.
    Records token + message quota into subscription_windows; no real $ computed.
  api (AP_FORCE_API_KEY=1) — Claude Code bills via ANTHROPIC_API_KEY.
    Computes and records real USD cost.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path.home() / ".autopilot" / ".env")

from autopilot.billing import calc_cost
from autopilot.config import get_project_id, get_branch, get_plan
from autopilot.tracker import (
    init_db, save_session, load_active_run, save_run,
    record_subscription_window,
)
from autopilot.observe import trace_session


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

    # Determine which surface this session ran on.
    billing_source = "api" if os.getenv("AP_FORCE_API_KEY") == "1" else "subscription"

    run = load_active_run(project)
    run_id = run.run_id if run else "none"
    phase = run.phase.value if run else "unknown"

    if billing_source == "subscription":
        # $0 marginal cost — track quota consumption only.
        cost = 0.0
        plan = get_plan()
        record_subscription_window(tokens_in, tokens_out, plan=plan)

        if run:
            run.subscription_msgs += 1
            run.subscription_tokens_in += tokens_in
            run.subscription_tokens_out += tokens_out
            save_run(run)

    else:
        # Real API billing — compute $ and accumulate on the run.
        cost = calc_cost(model, tokens_in, tokens_out)
        if run:
            run.cost_usd += cost
            save_run(run)

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
        billing_source=billing_source,
    )

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
        metadata={"billing_source": billing_source},
    )


if __name__ == "__main__":
    main()
