"""RunState lifecycle: create, update, phase transitions."""
import anthropic
import os

from flow.tracker import RunState, Phase, RunStatus, init_db, save_run, load_run, load_active_run
from flow.context import build_briefing, summarize_for_new_session
from flow.observe import trace_run_event, trace_run_started
from flow.config import get_project_id, get_branch, constraints


def _anthropic():
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))


def create_run(goal: str, feature_id: str = "") -> RunState:
    init_db()
    run = RunState(
        goal=goal,
        project=get_project_id(),
        branch=get_branch(),
        feature_id=feature_id,
    )
    save_run(run)
    trace_run_started(run.run_id, run.project, run.branch, goal)
    return run


def advance_phase(run: RunState, new_phase: Phase) -> RunState:
    run.phase = new_phase
    run.current_step = 0
    save_run(run)
    trace_run_event(run.run_id, run.project, f"phase:{new_phase.value}")
    return run


def add_artifact(run: RunState, artifact: str) -> RunState:
    if artifact not in run.artifacts:
        run.artifacts.append(artifact)
    save_run(run)
    return run


def add_decision(run: RunState, decision: str) -> RunState:
    run.decisions.append(decision)
    save_run(run)
    return run


def set_plan_steps(run: RunState, steps: list) -> RunState:
    """Replace plan steps and derive a weighted step budget from plan length."""
    run.plan_steps = steps
    if steps:
        c = constraints()
        multiplier = float(c.get("plan_steps_multiplier", 3.0))
        fallback = int(c.get("max_steps_per_run", 20))
        run.max_steps = max(fallback, round(len(steps) * multiplier))
    save_run(run)
    trace_run_event(run.run_id, run.project, "plan_set", {"step_count": len(steps), "max_steps": run.max_steps})
    return run


def set_check_acked(run: RunState, acked: bool) -> RunState:
    """Persist blocker-ack state so it survives REPL restarts."""
    run.check_blockers_acked = acked
    save_run(run)
    return run


def store_check_result(run: RunState, result_json: str) -> RunState:
    """Persist the latest flow check JSON and record its grade in decisions."""
    import json as _json
    run.last_check_result = result_json
    try:
        data = _json.loads(result_json)
        overall = data.get("overall", "?")
        blockers = data.get("blocker_count", 0)
        add_decision(run, f"flow check: overall={overall}, blockers={blockers}")
    except Exception:
        save_run(run)
    return run


def complete_plan_step(run: RunState, step_id: str) -> RunState:
    """Mark a single plan step as done."""
    for step in run.plan_steps:
        if step.get("id") == step_id:
            step["status"] = "done"
            break
    save_run(run)
    return run


def complete_run(run: RunState) -> RunState:
    run.status = RunStatus.complete
    run.claude_session_id = ""
    save_run(run)
    trace_run_event(run.run_id, run.project, "run_complete", {
        "api_spend_usd": run.cost_usd,
        "subscription_msgs": run.subscription_msgs,
        "subscription_tokens_in": run.subscription_tokens_in,
        "subscription_tokens_out": run.subscription_tokens_out,
    })
    return run


def refresh_context_summary(run: RunState) -> RunState:
    """Compress run state into a tight context summary using Haiku."""
    run.context_summary = summarize_for_new_session(run, _anthropic())
    run.claude_session_id = ""
    save_run(run)
    return run


def get_or_create_run(project: str, goal: str = "") -> RunState:
    run = load_active_run(project)
    if run:
        return run
    if not goal:
        raise ValueError("No active run and no goal provided")
    return create_run(goal)


def save_pr_url(run: RunState, pr_url: str) -> RunState:
    """Record the PR URL on the run after shipping."""
    run.pr_url = pr_url
    save_run(run)
    trace_run_event(run.run_id, run.project, "pr_created", {
        "pr_url": pr_url,
        "api_spend_usd": run.cost_usd,
        "subscription_msgs": run.subscription_msgs,
    })
    return run


def get_session_briefing(run: RunState) -> str:
    return build_briefing(run)
