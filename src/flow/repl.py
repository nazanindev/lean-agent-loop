"""
AI Flow REPL — prompt-to-PR autopilot.

Happy path: type a task → plan → execute → verify → fix loop → ship PR.
No approval gates. No manual phase management. The PR is the review.
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
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from flow.config import DB_PATH, constraints, get_project_id, get_branch, get_plan, get_plan_window_caps
from flow.router import MODEL_ALIASES, model_for
from flow.tracker import (
    Phase, RunStatus, init_db, load_active_run, save_run,
    get_api_spend_today, get_window_usage,
)


def _parse_claude_json_stdout(raw_out: str) -> Optional[Dict[str, Any]]:
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
    set_plan_steps, store_check_result,
)
from flow.session_accounting import account_claude_code_session_end, usage_from_claude_result
from flow.context import phase_directive

console = Console()
HISTORY_PATH = Path.home() / ".autopilot" / "repl_history"
HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)


class AutopilotREPL:
    def __init__(self):
        self.project = get_project_id()
        self.branch = get_branch()
        self.run: Optional[Any] = None
        self.model_override: Optional[str] = None
        self.no_agents = False
        c = constraints()
        self.auto_remediate = bool(c.get("auto_remediate", True))
        self.auto_remediate_max_tries = int(c.get("auto_remediate_max_tries", 2))
        self.auto_verify = bool(c.get("auto_verify_on_steps_complete", True))
        self.auto_check = bool(c.get("auto_check_before_ship", True))
        self.session = PromptSession(
            history=FileHistory(str(HISTORY_PATH)),
            style=Style.from_dict({"prompt": "bold cyan"}),
        )

    # ── Prompt ────────────────────────────────────────────────────────────────

    def _prompt_str(self) -> str:
        api_spend = get_api_spend_today(self.project)
        spend_str = f"${api_spend:.2f}"

        if not self.run:
            flags = " | no-agents" if self.no_agents else ""
            return f"flow [{spend_str}{flags}] > "

        phase = self.run.phase.value
        step_info = ""
        if phase == "execute" and self.run.plan_steps:
            done = sum(1 for s in self.run.plan_steps if s.get("status") == "done")
            total = len(self.run.plan_steps)
            step_info = f" {done}/{total}"

        flags = " | no-agents" if self.no_agents else ""
        return f"flow [{spend_str} | {phase}{step_info}{flags}] > "

    # ── Slash commands ────────────────────────────────────────────────────────

    def handle_slash(self, cmd: str) -> bool:
        parts = cmd.strip().split(None, 1)
        verb = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if verb == "/status":
            self._show_status()
        elif verb == "/model":
            m = MODEL_ALIASES.get(arg.lower(), arg)
            self.model_override = m
            console.print(f"[dim]→ Model: {m}[/dim]")
        elif verb == "/no-agents":
            self.no_agents = not self.no_agents
            os.environ["AP_NO_SPAWN"] = "1" if self.no_agents else "0"
            console.print(f"[dim]→ Agent spawning: {'OFF' if self.no_agents else 'ON'}[/dim]")
        elif verb == "/budget":
            os.environ["AP_BUDGET_USD"] = arg or "2.00"
            console.print(f"[dim]→ Budget cap: ${os.environ['AP_BUDGET_USD']}[/dim]")
        elif verb == "/stop":
            self._stop_current()
        elif verb == "/resume":
            self._resume(arg)
        elif verb in ("/quit", "/exit", "/q"):
            console.print("[dim]Goodbye.[/dim]")
            sys.exit(0)
        elif verb == "/help":
            self._show_help()
        else:
            console.print(f"[red]Unknown command: {verb}[/red]")
        return True

    def _stop_current(self) -> None:
        if not self.run:
            console.print("[dim]No active run.[/dim]")
            return
        sentinel = DB_PATH.parent / f"stop_{self.run.run_id}"
        sentinel.touch()
        console.print(f"[yellow]→ Stop signal sent[/yellow]")

    def _resume(self, run_id: str) -> None:
        from flow.tracker import load_run, get_recent_runs
        if run_id:
            r = load_run(run_id)
            if not r:
                console.print(f"[red]Run {run_id} not found.[/red]")
                return
            self.run = r
            console.print(f"[green]✓ Resumed: {r.goal[:60]}[/green]")
            return

        runs = [r for r in get_recent_runs(limit=10) if r["status"] != RunStatus.complete.value]
        if not runs:
            console.print("[yellow]No incomplete runs found.[/yellow]")
            return

        console.print("\n[bold]Recent incomplete runs:[/bold]")
        for i, r in enumerate(runs, 1):
            console.print(
                f"  [cyan]{i}.[/cyan] [{r['run_id']}] {r['goal'][:55]}  "
                f"[dim]{r['phase']} · ${r['cost_usd']:.4f}[/dim]"
            )
        try:
            choice = self.session.prompt("Pick (number or ID): ").strip()
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
        console.print(f"[green]✓ Resumed: {r.goal[:60]}[/green]")

    def _show_status(self) -> None:
        api_today = get_api_spend_today(self.project)
        plan = get_plan()
        window = get_window_usage(plan)
        cap = get_plan_window_caps().get(plan, {}).get("msgs", 0)
        quota_str = f"{window['msgs_used']}/{cap} msgs" if cap else f"{window['msgs_used']} msgs"
        console.print(
            f"[bold]Project:[/bold] {self.project} | "
            f"[bold]API spend today:[/bold] ${api_today:.4f} | "
            f"[bold]Quota (5h):[/bold] {quota_str}"
        )
        if self.run:
            done = sum(1 for s in self.run.plan_steps if s.get("status") == "done") if self.run.plan_steps else 0
            total = len(self.run.plan_steps)
            console.print(
                f"[bold]Run:[/bold] {self.run.run_id} | "
                f"[bold]Goal:[/bold] {self.run.goal[:55]} | "
                f"[bold]Phase:[/bold] {self.run.phase.value} | "
                f"[bold]Steps:[/bold] {done}/{total} | "
                f"[bold]Cost:[/bold] ${self.run.cost_usd:.4f}"
            )
            if self.run.plan_steps:
                self._print_plan()

    def _show_help(self) -> None:
        console.print(Panel(
            "[bold]Commands:[/bold]\n"
            "  /status           show cost, quota, active run\n"
            "  /model <name>     force model (opus / sonnet / haiku or full ID)\n"
            "  /no-agents        toggle subagent spawning\n"
            "  /budget $X        set API spend cap\n"
            "  /stop             send stop signal to running agent\n"
            "  /resume [id]      resume an interrupted run\n"
            "  /quit             exit\n\n"
            "[bold]The pipeline is automatic:[/bold]\n"
            "  prompt → plan → execute → verify → fix loop → PR\n"
            "  The PR is the review gate. No manual approvals needed.",
            title="AI Flow",
            border_style="dim",
        ))

    def _print_hook_misconfig_banner(self, message: str) -> None:
        console.print(Panel(
            f"{message}\n\n"
            "Hooks are not firing — enforcement and cost tracking are disabled.\n"
            "Run [bold]flow doctor --fix[/bold] or [bold]flow init --force[/bold], then restart.",
            title="[bold red]Hook configuration issue[/bold red]",
            border_style="red",
        ))

    # ── Plan parsing ──────────────────────────────────────────────────────────

    def _parse_numbered_plan_steps(self, text: str) -> list:
        steps = []
        for line in (text or "").splitlines():
            m = re.match(
                r"^\s*(?:\*\*)?\s*(?:step\s*)?(\d+)(?:\s*\*\*)?\s*(?:[.)]|:|—|-)\s+(.+)$",
                line,
                flags=re.IGNORECASE,
            )
            if m:
                steps.append({"id": m.group(1), "description": m.group(2).strip(), "status": "pending"})
        return steps

    def _extract_step_done_ids(self, text: str) -> list:
        results = []
        pattern = re.compile(
            r"^\s*STEP_DONE\s*:\s*(\d+)"
            r"(?:\s+\[evidence:\s*([^\]]+)\])?\s*$",
            flags=re.IGNORECASE,
        )
        for line in (text or "").splitlines():
            m = pattern.match(line.strip())
            if m:
                results.append((m.group(1), (m.group(2) or "").strip()))
        return results

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

    # ── Auto-pipeline ─────────────────────────────────────────────────────────

    def _run_pipeline(self) -> None:
        """Auto-run verify → check → ship after all plan steps complete."""
        from flow.commands.verify import run_checks
        from flow.commands.ship import cmd_ship

        # Step 1: verify
        console.print("\n[dim]→ Running verification...[/dim]")
        passed, output = run_checks()
        if not passed:
            if self.auto_remediate:
                ok = self._auto_remediate_verify(output, self.auto_remediate_max_tries)
                if not ok:
                    console.print(
                        "[red]✗ Verification still failing after remediation. Fix manually.[/red]"
                    )
                    console.print(f"[dim]{output[-1500:]}[/dim]")
                    return
            else:
                console.print("[red]✗ Verification failed[/red]")
                console.print(f"[dim]{output[-1500:]}[/dim]")
                return
        console.print("[green]✓ Verification passed[/green]")

        # Step 2: code review (requires API key)
        if self.auto_check and os.getenv("ANTHROPIC_API_KEY"):
            console.print("[dim]→ Running code review...[/dim]")
            try:
                from flow.commands.check import run_check
                report = run_check()
                blockers = int(report.get("blocker_count", 0))
                overall = report.get("overall", "?")
                console.print(f"[dim]Review: {overall} ({blockers} blocker{'s' if blockers != 1 else ''})[/dim]")
                if self.run:
                    store_check_result(self.run, json.dumps(report))
                if blockers > 0:
                    if self.auto_remediate:
                        ok = self._auto_remediate_check(report, self.auto_remediate_max_tries)
                        if not ok:
                            console.print(
                                "[red]✗ Code review blockers remain after remediation. Fix manually.[/red]"
                            )
                            for f in report.get("findings", []):
                                if f.get("severity") == "blocker":
                                    console.print(f"  [red]•[/red] {f['title']} — {f.get('detail', '')}")
                            return
                    else:
                        console.print("[red]✗ Code review found blockers[/red]")
                        return
                console.print("[green]✓ Code review passed[/green]")
            except Exception as e:
                console.print(f"[yellow]Code review skipped:[/yellow] {e}")

        # Step 3: ship
        console.print("[dim]→ Shipping...[/dim]")
        try:
            cmd_ship()
        except SystemExit as e:
            if e.code != 0:
                console.print("[red]✗ Ship failed[/red]")

    def _auto_remediate_verify(self, output: str, tries_left: int) -> bool:
        if tries_left <= 0:
            return False
        console.print(f"[yellow]→ Auto-fix: verify failed ({tries_left} attempt{'s' if tries_left != 1 else ''} left)[/yellow]")
        fix_task = (
            "Verification failed. Fix the root cause — do not add new features, "
            "only fix what's broken:\n\n"
            f"{output[-2000:]}"
        )
        self._run_turn(fix_task)
        from flow.commands.verify import run_checks
        passed, new_output = run_checks()
        if passed:
            return True
        return self._auto_remediate_verify(new_output, tries_left - 1)

    def _auto_remediate_check(self, report: dict, tries_left: int) -> bool:
        if tries_left <= 0:
            return False
        console.print(f"[yellow]→ Auto-fix: code review blockers ({tries_left} attempt{'s' if tries_left != 1 else ''} left)[/yellow]")
        blockers = [f for f in report.get("findings", []) if f.get("severity") == "blocker"]
        items = "\n".join(
            f"- {f['title']} ({f.get('file', 'unknown')}:{f.get('line', 0)}): "
            f"{f.get('detail', '')} → {f.get('action', '')}"
            for f in blockers
        )
        fix_task = f"Code review found blockers. Fix all of them — do not add features:\n\n{items}"
        self._run_turn(fix_task)
        try:
            from flow.commands.check import run_check
            new_report = run_check()
            new_report["blocker_count"] = sum(
                1 for f in new_report.get("findings", []) if f["severity"] == "blocker"
            )
        except Exception:
            return False
        if new_report.get("blocker_count", 0) == 0:
            return True
        return self._auto_remediate_check(new_report, tries_left - 1)

    # ── Turn execution ────────────────────────────────────────────────────────

    def _run_turn(self, launch_task: str) -> str:
        """Run one model turn, handle phase transitions, return response text."""
        response_text = self._launch_claude(launch_task)

        # Reload run after session ends (hooks may have updated it)
        from flow.tracker import load_run
        prev_phase = self.run.phase if self.run else Phase.plan
        updated = load_run(self.run.run_id) if self.run else None
        if updated:
            self.run = updated

        if not self.run:
            return response_text

        # Fallback: parse plan steps from response if ExitPlanMode wasn't called
        if self.run.phase == Phase.plan and not self.run.plan_steps and response_text:
            parsed_steps = self._parse_numbered_plan_steps(response_text)
            if parsed_steps:
                set_plan_steps(self.run, parsed_steps)
                updated = load_run(self.run.run_id)
                if updated:
                    self.run = updated
                advance_phase(self.run, Phase.execute)
                self.run.phase = Phase.execute
                console.print("[green]✓ Plan captured — executing[/green]")

        # Detect STEP_DONE markers in execute phase
        if self.run.phase == Phase.execute and self.run.plan_steps and response_text:
            marked = 0
            known_ids = {str(s.get("id")) for s in self.run.plan_steps}
            for step_id, _ in self._extract_step_done_ids(response_text):
                if step_id in known_ids:
                    complete_plan_step(self.run, step_id)
                    for step in self.run.plan_steps:
                        if str(step.get("id")) == step_id:
                            step["status"] = "done"
                            break
                    marked += 1
            if marked:
                done = sum(1 for s in self.run.plan_steps if s.get("status") == "done")
                total = len(self.run.plan_steps)
                console.print(f"[green]✓ Step{'s' if marked > 1 else ''} done ({done}/{total})[/green]")

        api_today = get_api_spend_today(self.project)
        if self.run:
            console.print(
                f"\n[dim]API: ${self.run.cost_usd:.4f} this run / ${api_today:.4f} today[/dim]"
            )

        # Show plan after plan phase
        if self.run.plan_steps and prev_phase == Phase.plan:
            self._print_plan()

        # Auto-advance plan → execute and immediately start executing
        if prev_phase == Phase.plan and self.run.phase == Phase.execute and self.run.plan_steps:
            console.print("[dim]→ Starting execution...[/dim]")
            self._run_turn("Begin executing the first pending plan step now.")
            return response_text

        # Auto-pipeline when all steps complete
        if (
            self.run.phase == Phase.execute
            and self.run.plan_steps
            and all(s.get("status") == "done" for s in self.run.plan_steps)
            and self.auto_verify
        ):
            self._run_pipeline()

        return response_text

    # ── Claude subprocess ─────────────────────────────────────────────────────

    def _try_dispatch_shell_style_flow(self, user_input: str) -> bool:
        stripped = user_input.strip()
        if stripped == "flow":
            console.print(
                "[yellow]Bare `flow` starts another REPL — blocked here.[/yellow]\n"
                "[dim]Use slash commands or `flow status`, `flow verify`, etc.[/dim]"
            )
            return True
        if not stripped.startswith("flow "):
            return False
        rest = stripped[5:].strip()
        if not rest:
            return False
        try:
            argv = shlex.split(rest)
        except ValueError as e:
            console.print(f"[red]Could not parse command: {e}[/red]")
            return True
        from flow.cli import app
        try:
            app(argv, standalone_mode=True)
        except SystemExit as e:
            if e.code not in (0, None):
                console.print(f"[red]Command exited with code {e.code}[/red]")
        return True

    def _launch_claude(self, task: str) -> str:
        """Run Claude Code headlessly (`claude -p`) and stream output."""
        if not self.run:
            return ""

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
        env["AP_FLOW_HEADLESS"] = "1"
        env["AP_NO_SPAWN"] = "1" if self.no_agents else env.get("AP_NO_SPAWN", "0")
        if os.getenv("AP_FORCE_API_KEY") != "1":
            env.pop("ANTHROPIC_API_KEY", None)

        c = constraints()
        max_turns = int(c.get("max_steps_per_run", 30))
        perm = os.getenv("AP_CLAUDE_PERMISSION_MODE", "bypassPermissions")
        timeout_s = int(os.getenv("AP_CLAUDE_TIMEOUT_S", "180"))
        stream_enabled = os.getenv("AP_CLAUDE_STREAM", "1") != "0"
        output_format = "stream-json" if stream_enabled else "json"

        cmd = [
            "claude", "-p", initial_message,
            "--output-format", output_format,
            "--model", model,
            "--permission-mode", perm,
            "--max-turns", str(max_turns),
        ]
        if stream_enabled:
            cmd.extend(["--verbose", "--include-partial-messages"])
        sid = (self.run.claude_session_id or "").strip()
        if sid:
            cmd.extend(["--resume", sid])

        console.print(
            f"\n[dim]→ {model} | {self.run.phase.value}"
            + (f" | resume {sid[:8]}" if sid else "")
            + "[/dim]\n"
        )

        stdout_lines: list = []
        stderr_lines: list = []
        streamed_parts: list = []
        final_data: Optional[Dict[str, Any]] = None
        printed_live = False

        try:
            proc = subprocess.Popen(
                cmd, env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            console.print("[red]Error: 'claude' CLI not found. Install Claude Code first.[/red]")
            return ""

        q: "queue.Queue" = queue.Queue()

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

        done_streams: set = set()
        start_ts = time.monotonic()
        user_stopped = False

        while True:
            if len(done_streams) == 2 and proc.poll() is not None and q.empty():
                break
            if (time.monotonic() - start_ts) > timeout_s:
                proc.kill()
                console.print(
                    f"[red]Timed out after {timeout_s}s[/red] "
                    "[dim](set AP_CLAUDE_TIMEOUT_S to adjust)[/dim]"
                )
                break
            try:
                stream_name, line = q.get(timeout=0.2)
            except queue.Empty:
                continue
            except KeyboardInterrupt:
                user_stopped = True
                proc.kill()
                console.print("[yellow]Stopped (Ctrl+C)[/yellow]")
                break

            sentinel = DB_PATH.parent / f"stop_{self.run.run_id}"
            if sentinel.exists():
                sentinel.unlink(missing_ok=True)
                user_stopped = True
                proc.kill()
                console.print("[yellow]Stopped via /stop[/yellow]")
                break

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
            if not stripped or not stream_enabled:
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
            console.print(text, end="", markup=False, highlight=False)
            streamed_parts.append(text)

        if user_stopped:
            self.run.status = RunStatus.blocked
            self.run.claude_session_id = ""
            save_run(self.run)
            return ""

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
            tail = (stderr_raw or stdout_raw).strip()
            if tail:
                console.print(f"[dim]{tail[-2000:]}[/dim]")
            return ""

        if not data:
            console.print("[red]No result from claude.[/red]")
            if stderr_raw.strip():
                console.print(f"[dim]{stderr_raw.strip()[-2000:]}[/dim]")
            return ""

        if isinstance(data, dict) and self.run:
            tin, tout, model_used, cr = usage_from_claude_result(data)
            sid = str(data.get("session_id") or "").strip() or str(uuid.uuid4())[:8]
            try:
                account_claude_code_session_end(
                    project=self.project, branch=self.branch, session_id=sid,
                    model=model_used, tokens_in=tin, tokens_out=tout,
                    cache_read_input_tokens=cr, run=self.run,
                )
            except Exception as e:
                console.print(f"[yellow]Could not record usage:[/yellow] {e}")

            new_sid = str(data.get("session_id") or "").strip()
            if new_sid:
                self.run.claude_session_id = new_sid
                save_run(self.run)

        if data.get("is_error") or data.get("subtype") == "error":
            err = data.get("result") or data.get("error") or str(data)
            if str(data.get("api_error_status")) == "429" or "limit" in str(err).lower():
                console.print(f"[yellow]Quota reached:[/yellow] {err}")
            else:
                console.print(f"[red]Claude error:[/red] {err}")
            return ""

        result_text = (data.get("result") or "").strip()
        streamed_text = "".join(streamed_parts).strip()
        if result_text and not streamed_text:
            console.print(Panel(Markdown(result_text), title="Claude", border_style="green"))
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
        self.run = load_active_run(self.project, self.branch)

        from flow.commands.doctor import hook_health_ok, hook_health_one_liner
        if not hook_health_ok():
            self._print_hook_misconfig_banner(hook_health_one_liner())

        active_note = (
            f"\n\n[yellow]Active: {self.run.run_id} — {self.run.goal[:55]}[/yellow]"
            if self.run else ""
        )
        console.print(Panel(
            f"[bold cyan]AI Flow[/bold cyan] — {self.project} ({self.branch})\n"
            f"[dim]Type a task to start. /help for commands.[/dim]{active_note}",
            border_style="cyan",
        ))

        with patch_stdout():
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
                    self.run = create_run(user_input)
                    model = model_for(self.run.phase, self.run.goal)
                    console.print(
                        f"\n[bold]Run {self.run.run_id}[/bold] | "
                        f"phase: {self.run.phase.value} | model: {model}"
                    )
                    self._run_turn(user_input)
                else:
                    self._run_turn(user_input)


def start_repl() -> None:
    AutopilotREPL().start()
