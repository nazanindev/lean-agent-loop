"""
AI Flow REPL — persistent interactive session.
Manages run lifecycle, phase switching, and Claude Code headless launch (`claude -p`).
"""
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from flow.config import constraints, get_project_id, get_branch, get_plan, get_plan_window_caps
from flow.router import MODEL_ALIASES, model_for
from flow.tracker import (
    Phase, RunStatus, init_db, load_active_run, save_run,
    get_api_spend_today, get_window_usage,
)


def _parse_claude_json_stdout(raw_out: str) -> Optional[Dict[str, Any]]:
    """Parse final JSON object from `claude -p --output-format json` stdout."""
    raw_out = (raw_out or "").strip()
    if not raw_out:
        return None
    try:
        return json.loads(raw_out)
    except json.JSONDecodeError:
        pass
    for line in reversed(raw_out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None
from flow.run_manager import (
    create_run, advance_phase, refresh_context_summary,
    add_artifact, add_decision, complete_plan_step, complete_run, get_session_briefing,
    set_plan_steps,
)
from flow.context import phase_directive

console = Console()
HISTORY_PATH = Path.home() / ".autopilot" / "repl_history"
HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)


class AutopilotREPL:
    def __init__(self):
        self.project = get_project_id()
        self.branch = get_branch()
        self.run = None
        self.model_override = None  # type: Optional[str]
        self.no_agents = False
        c = constraints()
        self.plan_gate_enabled = bool(c.get("plan_approval_gate", True))
        self.pr_gate_enabled = bool(c.get("pr_approval_gate", True))
        self.auto_ship_enabled = bool(c.get("auto_ship_on_verify_pass", False))
        self.ship_branch_name = ""
        self.ship_pr_title = ""
        self.session = PromptSession(
            history=FileHistory(str(HISTORY_PATH)),
            style=Style.from_dict({"prompt": "bold cyan"}),
        )

    # ── Prompt ────────────────────────────────────────────────────────────────

    def _active_feature_token(self) -> str:
        """Prompt/status token for active feature state."""
        try:
            from flow.features import get_active_feature

            feat = get_active_feature()
            if not feat:
                return ""
            return f"feat:{feat.id}"
        except Exception:
            return ""

    def _prompt_str(self) -> str:
        parts = []
        if self.run:
            model = self.model_override or model_for(self.run.phase, self.run.goal)
            model_short = model.split("-")[1] if "-" in model else model
            phase = self.run.phase.value
            step = f"{self.run.current_step}/{self.run.max_steps}"
            budget = f"{self.run.step_budget_used:.1f}"
            api_spend = f"api:${self.run.cost_usd:.2f}"
            # Quota % for the current 5-hour window
            plan = get_plan()
            window = get_window_usage(plan)
            cap = get_plan_window_caps().get(plan, {}).get("msgs", 0)
            quota_str = f"quota:{window['msgs_used']}/{cap}" if cap else ""
            inner_parts = [f"{phase}:{model_short}", f"step:{step}", f"wt:{budget}", api_spend]
            feature_token = self._active_feature_token()
            if feature_token:
                inner_parts.append(feature_token)
            if quota_str:
                inner_parts.append(quota_str)
            parts.append("|".join(inner_parts))
        else:
            parts.append(f"project:{self.project}")
        flags = []
        if self.no_agents:
            flags.append("no-agents")
        if flags:
            parts.append(",".join(flags))
        inner = " | ".join(parts)
        return f"flow [{inner}] > "

    def _parse_numbered_plan_steps(self, text: str) -> list[dict]:
        """Parse strictly structured numbered plan steps from assistant output."""
        steps = []
        raw = text or ""
        for line in raw.splitlines():
            m = re.match(
                r"^\s*(?:\*\*)?\s*(?:step\s*)?(\d+)(?:\s*\*\*)?\s*(?:[.)]|:|—|-)\s+(.+)$",
                line,
                flags=re.IGNORECASE,
            )
            if m:
                steps.append({"id": m.group(1), "description": m.group(2).strip(), "status": "pending"})
        return steps

    def _extract_step_done_ids(self, text: str) -> list[str]:
        """Extract explicit step completion markers from model output."""
        ids: list[str] = []
        for line in (text or "").splitlines():
            m = re.match(r"^\s*STEP_DONE\s*:\s*(\d+)\s*$", line.strip(), flags=re.IGNORECASE)
            if m:
                ids.append(m.group(1))
        return ids

    # ── Slash commands ────────────────────────────────────────────────────────

    def handle_slash(self, cmd: str) -> bool:
        """Returns True if handled."""
        parts = cmd.strip().split(None, 1)
        verb = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if verb == "/plan":
            self._set_phase(Phase.plan)
        elif verb in ("/exec", "/execute"):
            self._set_phase(Phase.execute)
        elif verb == "/fast":
            self._set_phase(Phase.execute)
            self.model_override = MODEL_ALIASES["haiku"]
            console.print("[dim]→ Fast mode: haiku[/dim]")
        elif verb == "/model":
            m = MODEL_ALIASES.get(arg.lower(), arg)
            self.model_override = m
            console.print(f"[dim]→ Model override: {m}[/dim]")
        elif verb == "/no-agents":
            self.no_agents = not self.no_agents
            state = "ON" if self.no_agents else "OFF"
            console.print(f"[dim]→ No-agents mode: {state}[/dim]")
            os.environ["AP_NO_SPAWN"] = "1" if self.no_agents else "0"
        elif verb == "/budget":
            os.environ["AP_BUDGET_USD"] = arg or "2.00"
            console.print(f"[dim]→ Budget set to ${os.environ['AP_BUDGET_USD']}[/dim]")
        elif verb == "/new":
            self._new_session()
        elif verb == "/compact":
            self._compact()
        elif verb == "/resume":
            self._resume(arg)
        elif verb == "/skip-plan":
            if self.run:
                advance_phase(self.run, Phase.execute)
                self.run.phase = Phase.execute
                console.print("[dim]→ Skipped planning phase → execute[/dim]")
        elif verb == "/approve":
            self._approve_plan()
        elif verb == "/reject":
            self._reject_plan()
        elif verb == "/gate":
            self._set_gate(arg)
        elif verb == "/ship-branch":
            self._set_ship_branch(arg)
        elif verb == "/ship-title":
            self._set_ship_title(arg)
        elif verb in ("/step-done", "/next"):
            self._step_done(arg)
        elif verb == "/status":
            self._show_status()
        elif verb == "/verify":
            self._run_verify()
        elif verb == "/ship":
            self._ship_with_gate()
        elif verb == "/done":
            self._finish_run()
        elif verb in ("/quit", "/exit", "/q"):
            console.print("[dim]Goodbye.[/dim]")
            sys.exit(0)
        elif verb == "/help":
            self._show_help()
        else:
            console.print(f"[red]Unknown command: {verb}[/red]")
        return True

    def _run_nested_flow_cli(self, argv: list[str]) -> None:
        """Run a `flow` subcommand in-process (same as the shell CLI)."""
        from flow.cli import app

        try:
            app(argv, standalone_mode=True)
        except SystemExit as e:
            code = e.code
            if code not in (0, None):
                console.print(f"[red]Command exited with code {code}[/red]")

    def _try_dispatch_shell_style_flow(self, user_input: str) -> bool:
        """If input looks like `flow <subcommand>`, run CLI instead of sending to Claude."""
        stripped = user_input.strip()
        if stripped == "flow":
            console.print(
                "[yellow]Bare `flow` starts another REPL — blocked here to avoid nesting.[/yellow]\n"
                "[dim]Use `flow status`, `flow features list`, etc., or slash commands like /status.[/dim]"
            )
            return True
        if not stripped.startswith("flow "):
            return False
        rest = stripped[5:].strip()
        if not rest:
            console.print(
                "[yellow]Missing subcommand after `flow`.[/yellow]\n"
                "[dim]Examples: `flow status`, `flow verify`, `flow features list`[/dim]"
            )
            return True
        try:
            argv = shlex.split(rest)
        except ValueError as e:
            console.print(f"[red]Could not parse command: {e}[/red]")
            return True
        self._run_nested_flow_cli(argv)
        return True

    def _set_phase(self, phase: Phase) -> None:
        if not self.run:
            console.print("[yellow]No active run. Start a task first.[/yellow]")
            return
        self.model_override = None
        advance_phase(self.run, phase)
        self.run.phase = phase
        model = model_for(phase, self.run.goal)
        console.print(f"[dim]→ Phase: {phase.value} | Model: {model}[/dim]")

    def _new_session(self) -> None:
        if not self.run:
            console.print("[yellow]No active run.[/yellow]")
            return
        console.print("[dim]Compressing context...[/dim]")
        refresh_context_summary(self.run)
        console.print("[green]✓ Context compressed. Next session will use RunState briefing.[/green]")

    def _compact(self) -> None:
        self._new_session()

    def _step_done(self, step_ref: str) -> None:
        """Mark a plan step done and auto-advance to verify if complete."""
        if not self.run:
            console.print("[yellow]No active run.[/yellow]")
            return
        if not self.run.plan_steps:
            console.print("[yellow]No plan steps recorded on this run.[/yellow]")
            return

        target_id = step_ref.strip()
        if not target_id:
            pending = [s for s in self.run.plan_steps if s.get("status") != "done"]
            if not pending:
                console.print("[green]All plan steps are already done.[/green]")
                return
            target_id = str(pending[0].get("id"))

        ids = {str(s.get("id")) for s in self.run.plan_steps}
        if target_id not in ids:
            console.print(f"[red]Unknown step id: {target_id}[/red]")
            return

        complete_plan_step(self.run, target_id)
        for step in self.run.plan_steps:
            if str(step.get("id")) == target_id:
                step["status"] = "done"
                break
        console.print(f"[green]✓ Step {target_id} marked done[/green]")

        if all(s.get("status") == "done" for s in self.run.plan_steps):
            advance_phase(self.run, Phase.verify)
            self.run.phase = Phase.verify
            console.print("[green]✓ All plan steps complete — phase auto-advanced to verify[/green]")

    def _approve_plan(self) -> None:
        """Approve captured plan, move to execute, and immediately start execution."""
        if not self.run:
            console.print("[yellow]No active run.[/yellow]")
            return
        if not self.run.plan_steps:
            console.print(
                "[yellow]No plan steps available to approve yet. "
                "Ask for a numbered plan first.[/yellow]"
            )
            return
        if self.run.phase == Phase.execute:
            console.print(
                "[yellow]Already in execute. Use `/next` when a step is done "
                "or send a follow-up task to continue.[/yellow]"
            )
            return
        if self.run.phase != Phase.plan:
            console.print(f"[yellow]Run is in {self.run.phase.value}, not plan.[/yellow]")
            return
        advance_phase(self.run, Phase.execute)
        self.run.phase = Phase.execute
        console.print("[green]✓ Plan approved — starting execution now[/green]")
        self._run_turn("Plan approved. Execute the first pending plan step now.")

    def _reject_plan(self) -> None:
        """Clear current plan and keep the run in plan phase for replanning."""
        if not self.run:
            console.print("[yellow]No active run.[/yellow]")
            return
        if not self.run.plan_steps:
            console.print("[yellow]No captured plan to reject.[/yellow]")
            return
        set_plan_steps(self.run, [])
        self.run.plan_steps = []
        if self.run.phase != Phase.plan:
            advance_phase(self.run, Phase.plan)
            self.run.phase = Phase.plan
        console.print("[green]✓ Plan rejected. Ask for a revised plan, then /approve.[/green]")

    def _set_gate(self, arg: str) -> None:
        """Toggle plan/pr approval gates for this REPL session."""
        tokens = arg.split()
        if len(tokens) != 2 or tokens[0] not in {"plan", "pr", "autoship"} or tokens[1] not in {"on", "off"}:
            console.print("[yellow]Usage: /gate plan|pr|autoship on|off[/yellow]")
            return
        gate, state_token = tokens
        enabled = state_token == "on"
        if gate == "plan":
            self.plan_gate_enabled = enabled
        elif gate == "pr":
            self.pr_gate_enabled = enabled
        else:
            self.auto_ship_enabled = enabled
        state = "ON" if enabled else "OFF"
        console.print(f"[dim]→ {gate.upper()} approval gate: {state}[/dim]")

    def _set_ship_branch(self, arg: str) -> None:
        """Set/clear branch name override used by /ship."""
        value = arg.strip()
        if not value or value.lower() in {"off", "clear", "reset"}:
            self.ship_branch_name = ""
            console.print("[dim]→ Ship branch override cleared[/dim]")
            return
        self.ship_branch_name = value
        console.print(f"[dim]→ Ship branch override: {value}[/dim]")

    def _set_ship_title(self, arg: str) -> None:
        """Set/clear PR title override used by /ship."""
        value = arg.strip()
        if not value or value.lower() in {"off", "clear", "reset"}:
            self.ship_pr_title = ""
            console.print("[dim]→ Ship PR title override cleared[/dim]")
            return
        self.ship_pr_title = value
        console.print(f"[dim]→ Ship PR title override set[/dim]")

    def _maybe_prompt_plan_approval(self) -> None:
        """Show explicit approval prompt when plan gate is enabled."""
        if not self.run or self.run.phase != Phase.plan or not self.run.plan_steps:
            return
        if self.plan_gate_enabled:
            console.print("[yellow]Plan captured. Run /approve to start execution, or /reject to re-plan.[/yellow]")

    def _ship_with_gate(self) -> None:
        """Gate ship command behind explicit PR approval when enabled."""
        if self.pr_gate_enabled:
            console.print(
                "[yellow]PR approval gate is ON. "
                "Run `/gate pr off` to bypass, then `/ship` again.[/yellow]"
            )
            return
        from flow.commands.ship import cmd_ship
        cmd_ship(branch_name=self.ship_branch_name, pr_title_override=self.ship_pr_title)

    def _resume(self, run_id: str) -> None:
        from flow.tracker import load_run, get_recent_runs, RunStatus
        if run_id:
            r = load_run(run_id)
            if not r:
                console.print(f"[red]Run {run_id} not found.[/red]")
                return
            self.run = r
            console.print(f"[green]✓ Resumed run {run_id}: {r.goal}[/green]")
            return

        # No ID given — show picker of recent incomplete runs
        runs = [r for r in get_recent_runs(limit=10) if r["status"] != RunStatus.complete.value]
        if not runs:
            console.print("[yellow]No incomplete runs found.[/yellow]")
            return

        console.print("\n[bold]Recent incomplete runs:[/bold]")
        for i, r in enumerate(runs, 1):
            console.print(
                f"  [cyan]{i}.[/cyan] [{r['run_id']}] {r['goal'][:60]}  "
                f"[dim]{r['phase']} · ${r['cost_usd']:.4f}[/dim]"
            )

        try:
            choice = self.session.prompt("Pick a run (number or ID): ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if choice.isdigit() and 1 <= int(choice) <= len(runs):
            run_id = runs[int(choice) - 1]["run_id"]
        else:
            run_id = choice

        r = load_run(run_id)
        if not r:
            console.print(f"[red]Run {run_id} not found.[/red]")
            return
        self.run = r
        console.print(f"[green]✓ Resumed run {run_id}: {r.goal}[/green]")

    def _run_verify(self) -> None:
        from flow.commands.verify import run_checks
        passed, output = run_checks()
        if passed:
            console.print("[green]✓ Verification passed[/green]")
            if self.run and self.run.phase == Phase.verify and self.auto_ship_enabled and not self.pr_gate_enabled:
                console.print("[dim]→ Auto-ship enabled; shipping after verify pass...[/dim]")
                self._ship_with_gate()
        else:
            console.print("[red]✗ Verification failed[/red]")
            console.print(f"[dim]{output[-1500:]}[/dim]")

    def _finish_run(self) -> None:
        if not self.run:
            return
        complete_run(self.run)
        console.print(f"[green]✓ Run {self.run.run_id} marked complete. Cost: ${self.run.cost_usd:.4f}[/green]")
        self.run = None

    def _show_status(self) -> None:
        api_today = get_api_spend_today(self.project)
        plan = get_plan()
        window = get_window_usage(plan)
        cap = get_plan_window_caps().get(plan, {}).get("msgs", 0)
        quota_str = f"{window['msgs_used']}/{cap} msgs" if cap else f"{window['msgs_used']} msgs"
        feature_token = self._active_feature_token()
        console.print(
            f"[bold]Project:[/bold] {self.project} | "
            f"[bold]API spend today:[/bold] ${api_today:.4f} | "
            f"[bold]Quota (5h window):[/bold] {quota_str}"
            + (f" | [bold]Active feature:[/bold] {feature_token.split(':', 1)[1]}" if feature_token else "")
        )
        if self.run:
            console.print(
                f"[bold]Run:[/bold] {self.run.run_id} | "
                f"[bold]Goal:[/bold] {self.run.goal[:60]} | "
                f"[bold]Phase:[/bold] {self.run.phase.value} | "
                f"[bold]Cost:[/bold] ${self.run.cost_usd:.4f}"
            )

    def _show_help(self) -> None:
        console.print(Panel(
            "[bold]Shell-style CLI (inside this REPL):[/bold]\n"
            "  `flow status` · `flow verify` · `flow features list` — same as in a terminal.\n"
            "  [dim]Bare `flow` is blocked here (would nest another REPL).[/dim]\n\n"
            "[bold]Slash shortcuts:[/bold]\n"
            "  /status · /verify · /ship — same as `flow status`, etc.\n\n"
            "[bold]Phase toggles:[/bold]\n"
            "  /plan          → Opus (planning)\n"
            "  /exec          → Sonnet (execution)\n"
            "  /fast          → Haiku (quick tasks)\n"
            "  /model <name>  → force model (opus/sonnet/haiku or full name)\n\n"
            "[bold]Context:[/bold]\n"
            "  /new           → compress context, start fresh session with RunState\n"
            "  /compact       → same as /new\n\n"
            "[bold]Control:[/bold]\n"
            "  /no-agents     → toggle subagent spawn blocking\n"
            "  /budget $X     → set session budget gate\n"
            "  /skip-plan     → skip planning, go straight to execute\n"
            "  /gate plan on|off → toggle plan approval gate for this session\n"
            "  /gate pr on|off   → toggle PR approval gate for this session\n\n"
            "  /gate autoship on|off → auto-run /ship after successful verify\n"
            "  /ship-branch <name>   → override branch name for /ship (clear/reset/off)\n"
            "  /ship-title <title>   → override PR title for /ship (clear/reset/off)\n\n"
            "[bold]Run lifecycle:[/bold]\n"
            "  /resume [id]   → resume an interrupted run (picker if no ID)\n"
            "  /approve       → approve captured plan and auto-start execution\n"
            "  /reject        → reject captured plan and stay in plan phase\n"
            "  /step-done [id]→ mark a plan step done (default: next pending)\n"
            "  /next          → alias for /step-done\n"
            "  /verify        → run tests/lint for current project\n"
            "  /ship          → verify → commit → create PR\n"
            "  /done          → mark current run complete\n"
            "  /status        → show current run + cost\n"
            "  /quit          → exit AI Flow",
            title="AI Flow commands",
            border_style="dim",
        ))

    def _run_turn(self, launch_task: str) -> None:
        """Run one model turn and apply post-turn phase/plan handling."""
        response_text = self._launch_claude(launch_task)

        # Reload run after session ends (hooks may have updated it)
        from flow.tracker import load_run
        prev_phase = self.run.phase
        updated = load_run(self.run.run_id)
        if updated:
            self.run = updated

        # Fallback: if model produced a numbered plan but didn't call ExitPlanMode,
        # capture steps. Auto-advance only when plan gate is disabled.
        if (
            self.run.phase == Phase.plan
            and not self.run.plan_steps
            and response_text
        ):
            parsed_steps = self._parse_numbered_plan_steps(response_text)
            if parsed_steps:
                set_plan_steps(self.run, parsed_steps)
                if self.plan_gate_enabled:
                    console.print("[green]✓ Parsed plan from response[/green]")
                else:
                    advance_phase(self.run, Phase.execute)
                    self.run.phase = Phase.execute
                    console.print(
                        "[green]✓ Parsed plan from response — auto-advanced to execute[/green]"
                    )

        # Execute-phase completion signal from the model.
        if self.run.phase == Phase.execute and self.run.plan_steps and response_text:
            marked = 0
            known_ids = {str(s.get("id")) for s in self.run.plan_steps}
            for step_id in self._extract_step_done_ids(response_text):
                if step_id in known_ids:
                    complete_plan_step(self.run, step_id)
                    for step in self.run.plan_steps:
                        if str(step.get("id")) == step_id:
                            step["status"] = "done"
                            break
                    marked += 1
            if marked:
                console.print(f"[green]✓ Auto-marked {marked} step(s) done from STEP_DONE markers[/green]")
                if all(s.get("status") == "done" for s in self.run.plan_steps):
                    advance_phase(self.run, Phase.verify)
                    self.run.phase = Phase.verify
                    console.print("[green]✓ All plan steps complete — phase auto-advanced to verify[/green]")

        api_today = get_api_spend_today(self.project)
        console.print(
            f"\n[dim]Session ended. "
            f"API spend: ${self.run.cost_usd:.4f} run / ${api_today:.4f} today | "
            f"Subscription: {self.run.subscription_msgs} msgs this run[/dim]"
        )

        # If a plan was just produced, surface it
        if self.run.plan_steps and (
            prev_phase == Phase.plan or self.run.phase == Phase.execute
        ):
            self._print_plan()
            self._maybe_prompt_plan_approval()

        # If we just transitioned plan -> execute without a gate, immediately run
        # the first execution turn so the user doesn't need an extra prompt.
        if (
            prev_phase == Phase.plan
            and self.run.phase == Phase.execute
            and self.run.plan_steps
            and not self.plan_gate_enabled
        ):
            console.print("[dim]→ Plan accepted; starting execution turn automatically...[/dim]")
            self._run_turn("Begin executing the first pending plan step now.")

    # ── Task launch ───────────────────────────────────────────────────────────

    def _structured_intake(self, goal: str) -> tuple[str, str]:
        """Collect structured context via inline REPL prompts. Returns (enriched_goal, context_summary).

        All fields after the goal are optional — pressing Enter skips them.
        No API call; zero quota consumed.
        """
        FIELDS = [
            ("acceptance", "Acceptance criteria (what does done look like?)"),
            ("out_of_scope", "Out of scope"),
            ("constraints", "Known constraints (tech, time, etc.)"),
            ("approach", "Preferred approach"),
        ]

        console.print("\n[bold cyan]Quick intake — press Enter to skip any field.[/bold cyan]\n")
        answers: dict[str, str] = {}
        for key, label in FIELDS:
            try:
                val = self.session.prompt(f"  {label}: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if val:
                answers[key] = val

        if not answers:
            return goal, ""

        lines = [f"**Goal:** {goal}"]
        label_map = {
            "acceptance": "Acceptance criteria",
            "out_of_scope": "Out of scope",
            "constraints": "Known constraints",
            "approach": "Preferred approach",
        }
        for key, val in answers.items():
            lines.append(f"**{label_map[key]}:** {val}")

        summary = "\n".join(lines)
        return goal, summary

    def _print_plan(self) -> None:
        if not self.run or not self.run.plan_steps:
            return
        done = sum(1 for s in self.run.plan_steps if s.get("status") == "done")
        total = len(self.run.plan_steps)
        lines = [f"[bold]Plan ({done}/{total} done):[/bold]"]
        for s in self.run.plan_steps:
            marker = "[green]✓[/green]" if s.get("status") == "done" else "[dim]○[/dim]"
            lines.append(f"  {marker} {s['description']}")
        console.print("\n" + "\n".join(lines))

    def _launch_claude(self, task: str) -> str:
        """Run Claude Code headlessly (`claude -p`) with briefing + directive; resume prior session when set."""
        model = self.model_override or model_for(self.run.phase, self.run.goal)
        briefing = get_session_briefing(self.run)
        directive = phase_directive(self.run)

        initial_message = (
            f"{briefing}\n"
            f"**Instructions for this session:**\n{directive}\n\n"
            f"---\n\n"
            f"{task}"
        )

        env = os.environ.copy()
        env["AP_ACTIVE"] = "1"
        env["AP_PLAN_GATE"] = "1" if self.plan_gate_enabled else "0"
        if self.no_agents:
            env["AP_NO_SPAWN"] = "1"

        # Avoid the auth conflict between a logged-in claude.ai session and
        # ANTHROPIC_API_KEY. AI Flow's own SDK calls still use the key from
        # the parent process; we only strip it from the subprocess.
        # Set AP_FORCE_API_KEY=1 if you actually want the CLI to bill via API.
        if os.getenv("AP_FORCE_API_KEY") != "1":
            env.pop("ANTHROPIC_API_KEY", None)

        c = constraints()
        max_turns = int(c.get("max_steps_per_run", 30))
        perm = os.getenv("AP_CLAUDE_PERMISSION_MODE", "bypassPermissions")
        timeout_s = int(os.getenv("AP_CLAUDE_TIMEOUT_S", "180"))

        stream_enabled = os.getenv("AP_CLAUDE_STREAM", "1") != "0"
        output_format = "stream-json" if stream_enabled else "json"
        cmd = [
            "claude",
            "-p",
            initial_message,
            "--output-format",
            output_format,
            "--model",
            model,
            "--permission-mode",
            perm,
            "--max-turns",
            str(max_turns),
        ]
        if stream_enabled:
            cmd.extend(["--verbose", "--include-partial-messages"])
        sid = (self.run.claude_session_id or "").strip()
        if sid:
            cmd.extend(["--resume", sid])

        console.print(
            f"\n[dim]→ Claude headless ({model}) | "
            f"phase: {self.run.phase.value} | "
            f"run: {self.run.run_id}"
            + (" | resume" if sid else "")
            + "[/dim]\n"
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        streamed_parts: list[str] = []
        final_data: Optional[Dict[str, Any]] = None
        printed_live = False

        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            console.print("[red]Error: 'claude' CLI not found. Install Claude Code first.[/red]")
            return ""

        q: "queue.Queue[tuple[str, Optional[str]]]" = queue.Queue()

        def _pump(stream_name: str, pipe) -> None:
            try:
                for line in iter(pipe.readline, ""):
                    q.put((stream_name, line))
            finally:
                q.put((stream_name, None))

        assert proc.stdout is not None and proc.stderr is not None
        t_out = threading.Thread(target=_pump, args=("stdout", proc.stdout), daemon=True)
        t_err = threading.Thread(target=_pump, args=("stderr", proc.stderr), daemon=True)
        t_out.start()
        t_err.start()

        done_streams = set()
        start_ts = time.monotonic()
        while True:
            if len(done_streams) == 2 and proc.poll() is not None and q.empty():
                break

            if (time.monotonic() - start_ts) > timeout_s:
                proc.kill()
                console.print(
                    f"[red]claude timed out after {timeout_s}s[/red] "
                    "[dim](set AP_CLAUDE_TIMEOUT_S to adjust)[/dim]"
                )
                break

            try:
                stream_name, line = q.get(timeout=0.2)
            except queue.Empty:
                continue

            if line is None:
                done_streams.add(stream_name)
                continue

            if stream_name == "stderr":
                stderr_lines.append(line)
                msg = line.strip()
                if msg:
                    console.print(f"[dim]{msg}[/dim]")
                continue

            stdout_lines.append(line)
            stripped = line.strip()
            if not stripped:
                continue
            if not stream_enabled:
                continue
            try:
                evt = json.loads(stripped)
            except json.JSONDecodeError:
                continue

            if evt.get("type") == "result":
                final_data = evt
                continue

            if evt.get("type") != "stream_event":
                continue
            event = evt.get("event", {})
            if event.get("type") != "content_block_delta":
                continue
            delta = event.get("delta", {})
            if delta.get("type") != "text_delta":
                continue
            text = str(delta.get("text", ""))
            if not text:
                continue
            if not printed_live:
                console.print("[bold green]Claude[/bold green]", end=": ")
                printed_live = True
            console.print(text, end="")
            streamed_parts.append(text)

        try:
            return_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return_code = proc.wait(timeout=5)
        if printed_live:
            console.print("")

        stdout_raw = "".join(stdout_lines)
        stderr_raw = "".join(stderr_lines)

        data = (
            final_data
            or _parse_claude_json_stdout(stdout_raw)
            or _parse_claude_json_stdout(stderr_raw)
        )
        if return_code != 0 and not data:
            console.print(f"[red]claude exited {return_code}[/red]")
            if stderr_raw.strip():
                console.print(f"[dim]{stderr_raw.strip()[-2000:]}[/dim]")
            elif stdout_raw.strip():
                console.print(f"[dim]{stdout_raw.strip()[-2000:]}[/dim]")
            return ""

        if not data:
            console.print("[red]No JSON result from claude.[/red]")
            if stderr_raw.strip():
                console.print(f"[dim]{stderr_raw.strip()[-2000:]}[/dim]")
            return ""

        if data.get("is_error") or data.get("subtype") == "error":
            err = data.get("result") or data.get("error") or str(data)
            if str(data.get("api_error_status")) == "429" or "limit" in str(err).lower():
                console.print(f"[yellow]Claude quota reached:[/yellow] {err}")
            else:
                console.print(f"[red]Claude error:[/red] {err}")
            return ""

        new_sid = str(data.get("session_id") or "").strip()
        if new_sid:
            self.run.claude_session_id = new_sid
            save_run(self.run)

        result_text = (data.get("result") or "").strip()
        streamed_text = "".join(streamed_parts).strip()
        if result_text:
            if not streamed_text:
                console.print(Panel(Markdown(result_text), title="Claude", border_style="green"))
        else:
            console.print("[dim](empty result)[/dim]")
        return result_text or streamed_text

    # ── Main loop ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        import warnings

        warnings.filterwarnings(
            "ignore",
            message=".*urllib3 v2 only supports OpenSSL.*",
            category=UserWarning,
            module="urllib3",
        )

        init_db()

        # Restore active run for this project
        self.run = load_active_run(self.project)

        console.print(Panel(
            f"[bold cyan]AI Flow[/bold cyan] — {self.project} ({self.branch})\n"
            f"[dim]Type a task to start, /help for commands, /quit to exit.[/dim]\n"
            f"[dim]Tip: `flow status` / `flow verify` work here; or use /status, /verify.[/dim]"
            + (f"\n\n[yellow]Active run: {self.run.run_id} — {self.run.goal[:60]}[/yellow]" if self.run else ""),
            border_style="cyan",
        ))

        while True:
            try:
                user_input = self.session.prompt(self._prompt_str()).strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Use /quit to exit.[/dim]")
                continue

            if not user_input:
                continue

            if user_input.startswith("/"):
                self.handle_slash(user_input)
                continue

            if self._try_dispatch_shell_style_flow(user_input):
                continue

            # New task or continuation
            if not self.run or self.run.status != RunStatus.active:
                goal, intake_summary = self._structured_intake(user_input)
                feature_id = ""
                try:
                    from flow.features import get_active_feature

                    active_feature = get_active_feature()
                    if active_feature:
                        feature_id = active_feature.id
                except Exception:
                    feature_id = ""
                self.run = create_run(goal, feature_id=feature_id)
                if intake_summary:
                    self.run.context_summary = intake_summary
                    from flow.tracker import save_run as _save_run
                    _save_run(self.run)
                model = model_for(self.run.phase, self.run.goal)
                console.print(
                    f"\n[bold]New run {self.run.run_id}[/bold] | "
                    f"phase: {self.run.phase.value} | model: {model}"
                )
                launch_task = goal
            else:
                launch_task = user_input

            self._run_turn(launch_task)


def start_repl() -> None:
    AutopilotREPL().start()
