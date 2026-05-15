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
    "opus":        "claude-opus-4-7",
    "sonnet":      "claude-sonnet-4-6",
    "haiku":       "claude-haiku-4-5-20251001",
    "flash":       "gemini/gemini-2.5-flash",
    "flash-lite":  "gemini/gemini-2.5-flash-lite",
    "gemini-pro":  "gemini/gemini-2.5-pro",
}

_UTILITY_DEFAULTS = {
    "fast":  "claude-haiku-4-5-20251001",
    "smart": "claude-sonnet-4-6",
}


def utility_model(tier: str = "fast") -> str:
    """Return the utility model for flow-internal calls (commit msgs, review, etc.)."""
    r = routing()
    return r.get("utility", {}).get(tier, _UTILITY_DEFAULTS[tier])
