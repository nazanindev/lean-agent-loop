"""flow init — wire AI Flow hooks into ~/.claude/settings.json."""
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
# AI Flow style — controls AI voice across all outputs.
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
        {"hooks": [{"type": "command", "command": "python3 -m flow.hooks.stop"}]}
    ],
    "PreToolUse": [
        {"matcher": "", "hooks": [{"type": "command", "command": "python3 -m flow.hooks.pretool"}]}
    ],
    "PreCompact": [
        {"hooks": [{"type": "command", "command": "python3 -m flow.hooks.precompact"}]}
    ],
}

REPO_FEATURES_TEMPLATE = """\
features:
  - id: F01
    behavior: "Describe a concrete behavior"
    verification: "pytest tests/path_or_command -x"
    state: "not_started"
    evidence: ""
    blocked_reason: ""
"""

REPO_PROGRESS_TEMPLATE = """\
# Progress

## Completed
- [ ] Nothing yet

## In Progress
- [ ] Select active feature with `flow features pick`

## Blocked
- (none)

## Next
1. Keep WIP=1
2. Verify before marking passing
"""


def cmd_init(force: bool = False, repo: bool = False) -> None:
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
            "Run [bold]flow init --force[/bold] to overwrite."
        )
        _show_status(settings)
        if repo:
            _scaffold_repo_artifacts()
        return

    settings["hooks"] = HOOKS
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)

    console.print("[green]✓ Hooks wired into ~/.claude/settings.json[/green]")
    console.print("[dim]  Stop       → python3 -m flow.hooks.stop[/dim]")
    console.print("[dim]  PreToolUse → python3 -m flow.hooks.pretool[/dim]")
    console.print("[dim]  PreCompact → python3 -m flow.hooks.precompact[/dim]")
    console.print(f"\n[dim]Next: add your API keys to {AP_ENV_PATH}[/dim]")

    if repo:
        _scaffold_repo_artifacts()


def _show_status(settings: dict) -> None:
    hooks = settings.get("hooks", {})
    for hook_type, configs in hooks.items():
        for cfg in configs:
            for h in cfg.get("hooks", []):
                console.print(f"  [dim]{hook_type}:[/dim] {h.get('command', '')}")


def _scaffold_repo_artifacts() -> None:
    """Create repo-local harness artifacts when missing."""
    cwd = Path.cwd()
    features_path = cwd / "features.yaml"
    progress_dir = cwd / "docs"
    progress_path = progress_dir / "PROGRESS.md"
    agents_path = cwd / "AGENTS.md"

    if not features_path.exists():
        features_path.write_text(REPO_FEATURES_TEMPLATE)
        console.print(f"[green]✓ Created {features_path}[/green]")
    else:
        console.print(f"[dim]Repo artifact exists: {features_path}[/dim]")

    progress_dir.mkdir(parents=True, exist_ok=True)
    if not progress_path.exists():
        progress_path.write_text(REPO_PROGRESS_TEMPLATE)
        console.print(f"[green]✓ Created {progress_path}[/green]")
    else:
        console.print(f"[dim]Repo artifact exists: {progress_path}[/dim]")

    if not agents_path.exists():
        agents_path.write_text(
            "# AGENTS.md\n\n"
            "Project-level agent instructions.\n\n"
            "- Use `flow features pick` before implementation.\n"
            "- Keep one active feature at a time.\n"
            "- Verify before marking features passing.\n"
        )
        console.print(f"[green]✓ Created {agents_path}[/green]")
    else:
        console.print(f"[dim]Repo artifact exists: {agents_path}[/dim]")
