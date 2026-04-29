"""
Claude Code PreCompact hook — invoked as: python3 -m autopilot.hooks.precompact
Provides a custom compaction prompt that preserves RunState artifacts
and drops conversation noise.
"""
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path.home() / ".autopilot" / ".env")

from autopilot.config import get_project_id
from autopilot.tracker import init_db, load_active_run

COMPACTION_PROMPT = """You are compacting a Claude Code session. Preserve ALL of the following — discard everything else:

1. **Goal**: The original task goal in one sentence.
2. **Phase**: Current phase (plan/execute/verify/ship) and step number.
3. **Plan**: The complete plan if one was produced (every step, numbered).
4. **Decisions**: Key decisions made so far and the reasoning (bullet list).
5. **Artifacts**: Files created/modified, test results, diffs (filenames + status).
6. **Current state**: What was just completed and what comes next.
7. **Constraints**: Any active constraints or overrides (/no-agents, budget limit, etc.).

Do NOT preserve: conversational back-and-forth, repeated tool output, intermediate reasoning that led to a decision already recorded above.

Format the summary as structured markdown under these exact headers."""


def main() -> None:
    init_db()
    project = get_project_id()
    run = load_active_run(project)

    context_addon = ""
    if run:
        context_addon = f"""
## Active Run Context
- Run ID: {run.run_id}
- Goal: {run.goal}
- Phase: {run.phase.value} (step {run.current_step}/{run.max_steps})
- Status: {run.status.value}
- Cost so far: ${run.cost_usd:.4f}
"""

    output = {
        "summary_prompt": COMPACTION_PROMPT + context_addon,
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
