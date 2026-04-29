"""ap init — wire Autopilot hooks into ~/.claude/settings.json."""
import json
import shutil
from pathlib import Path

from rich.console import Console

console = Console()

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
AP_ENV_PATH = Path.home() / ".autopilot" / ".env"
AP_STYLE_PATH = Path.home() / ".autopilot" / "style.yaml"
AP_ENV_EXAMPLE = Path(__file__).parent.parent.parent.parent / ".env.example"

DEFAULT_STYLE = """\
# Autopilot style — controls AI voice across all outputs.
# Set any section to null (or delete it) to skip that style injection entirely.

agent:
  verbosity: concise          # concise | verbose
  emoji: false
  confirm_before_destructive: true

commit_message:
  format: "short, imperative, no label prefix"
  # e.g. "add JWT middleware" not "feat: add JWT middleware"
  max_length: 72

pr_title:
  format: "plain description, sentence case, no prefix brackets"
  # e.g. "Add rate limiting to the API" not "[Feature] add rate limiting"

pr_body: |
  ## What
  {what}

  ## Why
  {why}

  ## Checklist
  - [ ] tests pass
  - [ ] no new dependencies without justification

ci_review:
  tone: "direct, no filler, flag real issues only"
  severity_labels: [blocker, suggestion, nit]
  skip_nitpicks: false
"""

HOOKS = {
    "Stop": [
        {"hooks": [{"type": "command", "command": "python3 -m autopilot.hooks.stop"}]}
    ],
    "PreToolUse": [
        {"matcher": "", "hooks": [{"type": "command", "command": "python3 -m autopilot.hooks.pretool"}]}
    ],
    "PreCompact": [
        {"hooks": [{"type": "command", "command": "python3 -m autopilot.hooks.precompact"}]}
    ],
}


def cmd_init(force: bool = False) -> None:
    # ── Ensure ~/.autopilot exists ────────────────────────────────────────────
    AP_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not AP_ENV_PATH.exists():
        shutil.copy(AP_ENV_EXAMPLE, AP_ENV_PATH)
        console.print(
            f"[yellow]Created {AP_ENV_PATH} — fill in your API key and set AP_PLAN "
            f"(pro|max5|max20|api_only).[/yellow]"
        )
    else:
        console.print(f"[dim]Env file already exists: {AP_ENV_PATH}[/dim]")

    if not AP_STYLE_PATH.exists():
        AP_STYLE_PATH.write_text(DEFAULT_STYLE)
        console.print(f"[green]✓ Created {AP_STYLE_PATH}[/green]")
    else:
        console.print(f"[dim]Style file already exists: {AP_STYLE_PATH}[/dim]")

    # ── Read existing Claude Code settings ───────────────────────────────────
    settings = {}
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH) as f:
            try:
                settings = json.load(f)
            except json.JSONDecodeError:
                settings = {}

    existing_hooks = settings.get("hooks", {})

    if existing_hooks and not force:
        console.print(
            "[yellow]Hooks already configured in ~/.claude/settings.json.[/yellow]\n"
            "Run [bold]ap init --force[/bold] to overwrite."
        )
        _show_status(settings)
        return

    settings["hooks"] = HOOKS
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)

    console.print("[green]✓ Hooks wired into ~/.claude/settings.json[/green]")
    console.print("[dim]  Stop       → python3 -m autopilot.hooks.stop[/dim]")
    console.print("[dim]  PreToolUse → python3 -m autopilot.hooks.pretool[/dim]")
    console.print("[dim]  PreCompact → python3 -m autopilot.hooks.precompact[/dim]")
    console.print(f"\n[dim]Next: add your API keys to {AP_ENV_PATH}[/dim]")


def _show_status(settings: dict) -> None:
    hooks = settings.get("hooks", {})
    for hook_type, configs in hooks.items():
        for cfg in configs:
            for h in cfg.get("hooks", []):
                console.print(f"  [dim]{hook_type}:[/dim] {h.get('command', '')}")
