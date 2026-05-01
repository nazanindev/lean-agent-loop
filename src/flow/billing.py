"""
Billing utilities shared across flow.

Two surfaces:
  subscription  — Claude Code sessions run against claude.ai Pro/Max login.
                  They cost $0 marginal; we track token + message quota instead.
  api           — flow utility calls (clarify, ship, ci-review) hit the Anthropic
                  API directly with ANTHROPIC_API_KEY. These bill real money
                  (cents per call, Haiku-mostly).

metered_call() wraps every flow-internal SDK call so real API spend is recorded.
"""
import os
import uuid

# Per 1M tokens, USD — API surface only.
COSTS: dict[str, dict[str, float]] = {
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


_MOCK_API: bool = os.getenv("AP_MOCK_API") == "1"


class _MockResponse:
    """Fake SDK response returned when AP_MOCK_API=1."""
    class _Usage:
        input_tokens = 0
        output_tokens = 0
    class _Content:
        text = "[mock] AP_MOCK_API=1 — real API call skipped"
    usage = _Usage()
    content = [_Content()]


def metered_call(client, model: str, *, run_id: str = "none", purpose: str, **kwargs):
    """
    Drop-in wrapper around client.messages.create that records real API spend
    in the sessions table (billing_source='api').

    Set AP_MOCK_API=1 to skip the real API call and return a stub response —
    useful when API credits are unavailable but you want to test the pipeline.

    Usage:
        resp = metered_call(client, HAIKU, run_id=run.run_id,
                            purpose="commit_msg", max_tokens=120,
                            system=system, messages=[...])
    """
    if os.getenv("AP_MOCK_API") == "1":
        return _MockResponse()

    resp = client.messages.create(model=model, **kwargs)

    tokens_in = getattr(resp.usage, "input_tokens", 0)
    tokens_out = getattr(resp.usage, "output_tokens", 0)
    cost = calc_cost(model, tokens_in, tokens_out)

    try:
        from autopilot.tracker import save_session
        save_session(
            session_id=str(uuid.uuid4())[:8],
            run_id=run_id,
            project="ap-internal",
            branch="",
            phase=purpose,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            billing_source="api",
        )
    except Exception:
        pass  # never let tracking kill the actual call

    return resp
