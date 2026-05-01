"""Phase → model selection with keyword overrides."""
from flow.config import routing
from flow.tracker import Phase


def model_for(phase: Phase, goal: str = "") -> str:
    r = routing()
    phases = r.get("phases", {})
    overrides = r.get("task_overrides", {})

    goal_lower = goal.lower()
    for keyword, model in overrides.items():
        if keyword in goal_lower:
            return model

    return phases.get(phase.value, "claude-sonnet-4-6")


MODEL_ALIASES = {
    "opus":   "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5-20251001",
}
