"""
AI Flow Orchestrator — multi-agent TUI with drill-down.

Type a task → runs in a background thread with its own git worktree.
Multiple tasks run simultaneously. /view N to drill into any session.
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
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*", category=UserWarning)

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from flow.config import DB_PATH, constraints, get_project_id, get_branch, get_plan, get_plan_window_caps
from flow.router import MODEL_ALIASES, model_for
from flow.tracker import (
    Phase, RunState, RunStatus, init_db, load_active_run, load_run, save_run,
    get_api_spend_today, get_window_usage,
)
from flow.run_manager import (
    advance_phase, complete_plan_step, get_session_briefing,
    set_plan_steps, store_check_result, save_pr_url,
)
from flow.session_accounting import account_claude_code_session_end, usage_from_claude_result
from flow.context import phase_directive
from flow.observe import trace_run_started


console = Console()
HISTORY_PATH = Path.home() / ".autopilot" / "repl_history"
HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)


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


@dataclass
class AgentSession:
    idx: int
    goal: str
    run: Any                          # RunState — owned by session worker thread
    project: str
    branch: str
    cwd: Path
    thread: Optional[threading.Thread] = None
    output_queue: queue.Queue = field(default_factory=queue.Queue)
    output_history: List[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    status: str = "running"           # "running" | "done" | "failed"
    last_line: str = ""
    pr_url: str = ""


class FlowOrchestrator:
    def __init__(self):
        self.project = get_project_id()
        self.branch = get_branch()
        self.sessions: List[AgentSession] = []
        self.model_override: Optional[str] = None
        self.no_agents = False
        c = constraints()
        self.auto_remediate = bool(c.get("auto_remediate", True))
        self.auto_remediate_max_tries = int(c.get("auto_remediate_max_tries", 2))
        self.auto_verify = bool(c.get("auto_verify_on_steps_complete", True))
        self.auto_check = bool(c.get("auto_check_before_ship", True))
        self.prompt_session = PromptSession(
            history=FileHistory(str(HISTORY_PATH)),
            style=Style.from_dict({"prompt": "bold cyan"}),
        )

    # ── Git worktree ──────────────────────────────────────────────────────────

    def _git_root(self) -> Path:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True,
            )
            return Path(r.stdout.strip())
        except subprocess.CalledProcessError:
            return Path.cwd()

    def _create_worktree(self, goal: str) -> tuple:
        """Create a git worktree for a new session. Returns (path, branch_name)."""
        slug = re.sub(r"[^a-z0-9]+", "-", goal.lower())[:25].strip("-")
        name = f"flow-{slug}-{uuid.uuid4().hex[:4]}"
        git_root = self._git_root()
        worktree_dir = git_root / ".claude" / "worktrees"
        worktree_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_dir / name), "-b", name],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            console.print(f"[yellow]Worktree creation failed — using main directory.[/yellow]")
            return git_root, self.branch
        return worktree_dir / name, name

    def _remove_worktree(self, session: AgentSession) -> None:
        if session.cwd == self._git_root():
            return
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(session.cwd)],
            capture_output=True, text=True,
        )

    # ── Session output routing ────────────────────────────────────────────────

    def _session_push(self, session: AgentSession, text: str) -> None:
        """Thread-safe: route output chunk to session queue and update last_line."""
        if not text:
            return
        session.output_queue.put(text)
        with session.lock:
            stripped = text.strip()
            if stripped:
                session.last_line = stripped[-100:]

    def _drain_queues(self) -> None:
        """Main-thread: drain all session queues into output_history."""
        for session in self.sessions:
            while True:
                try:
                    chunk = session.output_queue.get_nowait()
                    session.output_history.append(chunk)
                except queue.Empty:
                    break

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def _start_session(self, goal: str) -> AgentSession:
        cwd, branch = self._create_worktree(goal)

        init_db()
        run = RunState(goal=goal, project=self.project, branch=branch)
        save_run(run)
        trace_run_started(run.run_id, run.project, run.branch, goal)

        idx = len(self.sessions) + 1
        session = AgentSession(idx=idx, goal=goal, run=run,
                               project=self.project, branch=branch, cwd=cwd)
        session.thread = threading.Thread(
            target=self._session_worker, args=(session,), daemon=True,
        )
        self.sessions.append(session)
        session.thread.start()
        return session

    def _session_worker(self, session: AgentSession) -> None:
        try:
            self._run_turn(session.goal, session)
            with session.lock:
                if session.status == "running":
                    session.status = "done"
        except SystemExit:
            with session.lock:
                if session.status == "running":
                    session.status = "done"
        except Exception as e:
            with session.lock:
                session.status = "failed"
                session.last_line = str(e)[:100]

    # ── Plan helpers ──────────────────────────────────────────────────────────

    def _parse_numbered_plan_steps(self, text: str) -> list:
        steps = []
        for line in (text or "").splitlines():
            m = re.match(
                r"^\s*(?:\*\*)?\s*(?:step\s*)?(\d+)(?:\s*\*\*)?\s*(?:[.)]|:|—|-)\s+(.+)$",
                line, flags=re.IGNORECASE,
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

    # ── Turn execution ────────────────────────────────────────────────────────

    def _run_turn(self, task: str, session: AgentSession) -> str:
        response_text = self._launch_claude(task, session)

        prev_phase = session.run.phase
        updated = load_run(session.run.run_id)
        if updated:
            session.run = updated

        # Fallback plan step parsing if ExitPlanMode wasn't called
        if session.run.phase == Phase.plan and not session.run.plan_steps and response_text:
            parsed = self._parse_numbered_plan_steps(response_text)
            if parsed:
                set_plan_steps(session.run, parsed)
                updated = load_run(session.run.run_id)
                if updated:
                    session.run = updated
                advance_phase(session.run, Phase.execute)
                session.run.phase = Phase.execute
                self._session_push(session, "✓ Plan captured — executing\n")

        # Detect STEP_DONE markers in execute phase
        if session.run.phase == Phase.execute and session.run.plan_steps and response_text:
            marked = 0
            known_ids = {str(s.get("id")) for s in session.run.plan_steps}
            for step_id, _ in self._extract_step_done_ids(response_text):
                if step_id in known_ids:
                    complete_plan_step(session.run, step_id)
                    for step in session.run.plan_steps:
                        if str(step.get("id")) == step_id:
                            step["status"] = "done"
                            break
                    marked += 1
            if marked:
                done = sum(1 for s in session.run.plan_steps if s.get("status") == "done")
                total = len(session.run.plan_steps)
                self._session_push(session, f"✓ Steps done ({done}/{total})\n")

        api_today = get_api_spend_today(session.project)
        self._session_push(
            session,
            f"API: ${session.run.cost_usd:.4f} this run / ${api_today:.4f} today\n",
        )

        # Auto-advance plan → execute
        if prev_phase == Phase.plan and session.run.phase == Phase.execute and session.run.plan_steps:
            self._session_push(session, "→ Starting execution...\n")
            self._run_turn("Begin executing the first pending plan step now.", session)
            return response_text

        # Auto-pipeline when all steps complete
        if (
            session.run.phase == Phase.execute
            and session.run.plan_steps
            and all(s.get("status") == "done" for s in session.run.plan_steps)
            and self.auto_verify
        ):
            self._run_pipeline(session)

        return response_text

    def _run_pipeline(self, session: AgentSession) -> None:
        from flow.commands.verify import run_checks

        self._session_push(session, "\n→ Running verification...\n")
        passed, output = run_checks(cwd=session.cwd)

        if not passed:
            if self.auto_remediate:
                ok = self._auto_remediate_verify(output, self.auto_remediate_max_tries, session)
                if not ok:
                    self._session_push(session, "✗ Verification still failing — fix manually.\n")
                    with session.lock:
                        session.status = "failed"
                    return
            else:
                self._session_push(session, f"✗ Verification failed\n{output[-1500:]}\n")
                with session.lock:
                    session.status = "failed"
                return
        self._session_push(session, "✓ Verification passed\n")

        if self.auto_check and os.getenv("ANTHROPIC_API_KEY"):
            self._session_push(session, "→ Running code review...\n")
            try:
                diff_result = subprocess.run(
                    ["git", "diff", "HEAD"],
                    capture_output=True, text=True, cwd=str(session.cwd),
                )
                from flow.commands.check import run_check
                report = run_check(diff_text=diff_result.stdout or None)
                blockers = int(report.get("blocker_count", 0))
                overall = report.get("overall", "?")
                self._session_push(
                    session,
                    f"Review: {overall} ({blockers} blocker{'s' if blockers != 1 else ''})\n",
                )
                store_check_result(session.run, json.dumps(report))
                if blockers > 0:
                    if self.auto_remediate:
                        ok = self._auto_remediate_check(report, self.auto_remediate_max_tries, session)
                        if not ok:
                            self._session_push(session, "✗ Code review blockers remain — fix manually.\n")
                            with session.lock:
                                session.status = "failed"
                            return
                    else:
                        self._session_push(session, "✗ Code review found blockers\n")
                        with session.lock:
                            session.status = "failed"
                        return
                self._session_push(session, "✓ Code review passed\n")
            except Exception as e:
                self._session_push(session, f"Code review skipped: {e}\n")

        self._session_push(session, "→ Shipping...\n")
        ship_env = {**os.environ, "AP_ACTIVE": "0"}
        ship_result = subprocess.run(
            ["flow", "ship"],
            cwd=str(session.cwd),
            capture_output=True, text=True,
            env=ship_env,
        )
        ship_output = (ship_result.stdout + ship_result.stderr).strip()
        self._session_push(session, ship_output + "\n")

        pr_match = re.search(r"https?://github\.com/\S+/pull/\d+", ship_output)
        if pr_match:
            with session.lock:
                session.pr_url = pr_match.group(0)
                session.last_line = f"PR: {session.pr_url}"

    def _auto_remediate_verify(self, output: str, tries_left: int, session: AgentSession) -> bool:
        if tries_left <= 0:
            return False
        self._session_push(
            session,
            f"→ Auto-fix: verify failed ({tries_left} attempt{'s' if tries_left != 1 else ''} left)\n",
        )
        fix_task = (
            "Verification failed. Fix the root cause — do not add new features:\n\n"
            f"{output[-2000:]}"
        )
        self._run_turn(fix_task, session)
        from flow.commands.verify import run_checks
        passed, new_output = run_checks(cwd=session.cwd)
        if passed:
            return True
        return self._auto_remediate_verify(new_output, tries_left - 1, session)

    def _auto_remediate_check(self, report: dict, tries_left: int, session: AgentSession) -> bool:
        if tries_left <= 0:
            return False
        self._session_push(
            session,
            f"→ Auto-fix: code review blockers ({tries_left} attempt{'s' if tries_left != 1 else ''} left)\n",
        )
        blockers = [f for f in report.get("findings", []) if f.get("severity") == "blocker"]
        items = "\n".join(
            f"- {f['title']} ({f.get('file', 'unknown')}:{f.get('line', 0)}): "
            f"{f.get('detail', '')} → {f.get('action', '')}"
            for f in blockers
        )
        self._run_turn(f"Code review found blockers. Fix all — do not add features:\n\n{items}", session)
        try:
            diff_result = subprocess.run(
                ["git", "diff", "HEAD"], capture_output=True, text=True, cwd=str(session.cwd),
            )
            from flow.commands.check import run_check
            new_report = run_check(diff_text=diff_result.stdout or None)
        except Exception:
            return False
        if new_report.get("blocker_count", 0) == 0:
            return True
        return self._auto_remediate_check(new_report, tries_left - 1, session)

    # ── Claude subprocess ─────────────────────────────────────────────────────

    def _launch_claude(self, task: str, session: AgentSession) -> str:
        model = self.model_override or model_for(session.run.phase, session.run.goal)
        briefing = get_session_briefing(session.run)
        directive = phase_directive(session.run)

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
        sid = (session.run.claude_session_id or "").strip()
        if sid:
            cmd.extend(["--resume", sid])

        self._session_push(
            session,
            f"\n→ {model} | {session.run.phase.value}"
            + (f" | resume {sid[:8]}" if sid else "")
            + "\n",
        )

        stdout_lines: list = []
        stderr_lines: list = []
        streamed_parts: list = []
        final_data: Optional[Dict[str, Any]] = None
        printed_header = False

        try:
            proc = subprocess.Popen(
                cmd, env=env,
                cwd=str(session.cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            self._session_push(session, "Error: 'claude' CLI not found.\n")
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
                self._session_push(session, f"Timed out after {timeout_s}s\n")
                break
            try:
                stream_name, line = q.get(timeout=0.2)
            except queue.Empty:
                continue

            sentinel = DB_PATH.parent / f"stop_{session.run.run_id}"
            if sentinel.exists():
                sentinel.unlink(missing_ok=True)
                user_stopped = True
                proc.kill()
                self._session_push(session, "Stopped via /stop\n")
                break

            if line is None:
                done_streams.add(stream_name)
                continue

            if stream_name == "stderr":
                stderr_lines.append(line)
                msg = line.strip()
                if msg:
                    self._session_push(session, msg + "\n")
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
            if not printed_header:
                self._session_push(session, "Claude: ")
                printed_header = True
            self._session_push(session, text)
            streamed_parts.append(text)

        if user_stopped:
            session.run.status = RunStatus.blocked
            session.run.claude_session_id = ""
            save_run(session.run)
            return ""

        try:
            return_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return_code = proc.wait(timeout=5)
        if printed_header:
            self._session_push(session, "\n")

        stdout_raw = "".join(stdout_lines)
        stderr_raw = "".join(stderr_lines)
        data = (
            final_data
            or _parse_claude_json_stdout(stdout_raw)
            or _parse_claude_json_stdout(stderr_raw)
        )

        if return_code != 0 and not data:
            self._session_push(session, f"claude exited {return_code}\n")
            tail = (stderr_raw or stdout_raw).strip()
            if tail:
                self._session_push(session, tail[-2000:] + "\n")
            return ""

        if not data:
            self._session_push(session, "No result from claude.\n")
            return ""

        if isinstance(data, dict):
            tin, tout, model_used, cr = usage_from_claude_result(data)
            sid = str(data.get("session_id") or "").strip() or str(uuid.uuid4())[:8]
            try:
                account_claude_code_session_end(
                    project=session.project, branch=session.branch, session_id=sid,
                    model=model_used, tokens_in=tin, tokens_out=tout,
                    cache_read_input_tokens=cr, run=session.run,
                )
            except Exception as e:
                self._session_push(session, f"Could not record usage: {e}\n")

            new_sid = str(data.get("session_id") or "").strip()
            if new_sid:
                session.run.claude_session_id = new_sid
                save_run(session.run)

        if data.get("is_error") or data.get("subtype") == "error":
            err = data.get("result") or data.get("error") or str(data)
            if str(data.get("api_error_status")) == "429" or "limit" in str(err).lower():
                self._session_push(session, f"Quota reached: {err}\n")
            else:
                self._session_push(session, f"Claude error: {err}\n")
            return ""

        result_text = (data.get("result") or "").strip()
        streamed_text = "".join(streamed_parts).strip()
        if result_text and not streamed_text:
            self._session_push(session, result_text + "\n")
        return result_text or streamed_text

    # ── Live table display ────────────────────────────────────────────────────

    def _render_table(self) -> Table:
        api_today = get_api_spend_today(self.project)
        running = sum(1 for s in self.sessions if s.status == "running")

        table = Table(
            title=f"flow  |  ${api_today:.2f} today  |  {running} running",
            show_header=True, header_style="bold",
            border_style="dim", expand=True,
        )
        table.add_column("#", width=3, justify="right")
        table.add_column("Task", ratio=3)
        table.add_column("Phase", width=8)
        table.add_column("Steps", width=6)
        table.add_column("Cost", width=8)
        table.add_column("Last output", ratio=4)

        for session in self.sessions:
            fresh = load_run(session.run.run_id) or session.run
            phase = fresh.phase.value if fresh else "?"

            steps_str = ""
            if fresh and fresh.plan_steps:
                done = sum(1 for s in fresh.plan_steps if s.get("status") == "done")
                total = len(fresh.plan_steps)
                steps_str = f"{done}/{total}" if session.status == "running" else "✓"

            cost_str = f"${fresh.cost_usd:.2f}" if fresh else "$0.00"

            with session.lock:
                status = session.status
                last = session.last_line
                pr_url = session.pr_url

            if status == "done":
                status_str = "[green]done[/green]"
                display_last = pr_url or last
            elif status == "failed":
                status_str = "[red]failed[/red]"
                display_last = last
            else:
                status_str = phase
                display_last = last

            table.add_row(
                str(session.idx),
                session.goal[:55],
                status_str,
                steps_str,
                cost_str,
                display_last[:80],
            )

        if not self.sessions:
            table.add_row("", "[dim]No sessions yet — type a task to start[/dim]", "", "", "", "")

        return table

    # ── Drill-down ────────────────────────────────────────────────────────────

    def _drill_down(self, idx: int, live: Live) -> None:
        if idx < 1 or idx > len(self.sessions):
            console.print(f"[red]No session {idx}[/red]")
            return

        session = self.sessions[idx - 1]
        live.stop()

        console.print(f"\n[bold]─── Session {idx}: {session.goal} ───[/bold]")
        for chunk in session.output_history:
            console.print(chunk, end="", markup=False, highlight=False)

        stop_event = threading.Event()

        def _drain_and_print():
            while not stop_event.is_set():
                try:
                    while True:
                        chunk = session.output_queue.get_nowait()
                        session.output_history.append(chunk)
                        with session.lock:
                            stripped = chunk.strip()
                            if stripped:
                                session.last_line = stripped[-100:]
                        console.print(chunk, end="", markup=False, highlight=False)
                except queue.Empty:
                    time.sleep(0.1)

        drain_thread = threading.Thread(target=_drain_and_print, daemon=True)
        drain_thread.start()

        goal_short = session.goal[:20]
        while True:
            try:
                cmd = self.prompt_session.prompt(
                    f"[{idx}:{goal_short}] (read-only) > "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if cmd in ("/back", "b", ""):
                break
            if cmd.startswith("/"):
                console.print("[dim]In drill-down: only /back is accepted. Use /back to return.[/dim]")

        stop_event.set()
        drain_thread.join(timeout=1)
        console.print(f"\n[dim]← Back to orchestrator[/dim]")
        live.start()

    # ── Prompt ────────────────────────────────────────────────────────────────

    def _prompt_str(self) -> str:
        api_today = get_api_spend_today(self.project)
        running = sum(1 for s in self.sessions if s.status == "running")
        flags = " | no-agents" if self.no_agents else ""
        if running:
            return f"flow [${api_today:.2f} | {running} running{flags}] > "
        return f"flow [${api_today:.2f}{flags}] > "

    # ── Slash commands ────────────────────────────────────────────────────────

    def handle_slash(self, cmd: str, live: Optional[Live] = None) -> None:
        parts = cmd.strip().split(None, 1)
        verb = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if verb == "/status":
            self._show_status()
        elif verb == "/sessions":
            self._show_sessions()
        elif verb == "/view":
            if not arg.isdigit():
                console.print("[red]Usage: /view N[/red]")
            elif live:
                self._drill_down(int(arg), live)
            else:
                console.print("[yellow]/view requires live mode[/yellow]")
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
            self._stop_session(int(arg) if arg.isdigit() else None)
        elif verb == "/resume":
            self._resume(arg)
        elif verb in ("/quit", "/exit", "/q"):
            self._on_quit()
        elif verb == "/help":
            self._show_help()
        else:
            console.print(f"[red]Unknown command: {verb}[/red]")

    def _stop_session(self, idx: Optional[int]) -> None:
        targets = (
            [self.sessions[idx - 1]] if idx and 1 <= idx <= len(self.sessions)
            else [s for s in self.sessions if s.status == "running"]
        )
        if not targets:
            console.print("[dim]No running sessions.[/dim]")
            return
        for s in targets:
            sentinel = DB_PATH.parent / f"stop_{s.run.run_id}"
            sentinel.touch()
            console.print(f"[yellow]→ Stop signal sent to session {s.idx}[/yellow]")

    def _resume(self, run_id: str) -> None:
        from flow.tracker import get_recent_runs
        if run_id:
            r = load_run(run_id)
            if not r:
                console.print(f"[red]Run {run_id} not found.[/red]")
                return
            self._attach_existing_run(r)
            return

        runs = [r for r in get_recent_runs(limit=10) if r["status"] != RunStatus.complete.value]
        if not runs:
            console.print("[yellow]No incomplete runs found.[/yellow]")
            return

        console.print("\n[bold]Recent incomplete runs:[/bold]")
        for i, r in enumerate(runs, 1):
            console.print(
                f"  [cyan]{i}.[/cyan] [{r['run_id']}] {r['goal'][:50]}  "
                f"[dim]{r['phase']} · ${r['cost_usd']:.4f}[/dim]"
            )
        try:
            choice = self.prompt_session.prompt("Pick (number or ID): ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        run_id = runs[int(choice) - 1]["run_id"] if choice.isdigit() and 1 <= int(choice) <= len(runs) else choice
        r = load_run(run_id)
        if not r:
            console.print(f"[red]Run {run_id} not found.[/red]")
            return
        self._attach_existing_run(r)

    def _attach_existing_run(self, run: RunState) -> None:
        idx = len(self.sessions) + 1
        session = AgentSession(
            idx=idx, goal=run.goal, run=run,
            project=run.project, branch=run.branch,
            cwd=self._git_root(),
        )
        session.thread = threading.Thread(
            target=self._session_worker, args=(session,), daemon=True,
        )
        self.sessions.append(session)
        session.thread.start()
        console.print(f"[green]✓ Resumed: {run.goal[:55]}[/green]")

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
        self._show_sessions()

    def _show_sessions(self) -> None:
        if not self.sessions:
            console.print("[dim]No sessions.[/dim]")
            return
        for s in self.sessions:
            with s.lock:
                status = s.status
                last = s.last_line
            console.print(
                f"  [cyan]{s.idx}.[/cyan] [{status}] {s.goal[:50]}  "
                f"[dim]{s.branch}[/dim]  {last[:60]}"
            )

    def _show_help(self) -> None:
        console.print(Panel(
            "[bold]Commands:[/bold]\n"
            "  /view N           drill into session N (read-only output)\n"
            "  /sessions         list all sessions\n"
            "  /status           cost, quota, all sessions\n"
            "  /model <name>     force model (opus / sonnet / haiku)\n"
            "  /no-agents        toggle subagent spawning\n"
            "  /budget $X        set API spend cap\n"
            "  /stop [N]         stop session N (or all running)\n"
            "  /resume [id]      attach to an interrupted run\n"
            "  /quit             exit\n\n"
            "[bold]Pipeline:[/bold]\n"
            "  prompt → plan → execute → verify → fix loop → PR\n"
            "  Each task runs in its own git worktree + branch.",
            title="AI Flow", border_style="dim",
        ))

    def _on_quit(self) -> None:
        done = [s for s in self.sessions if s.status in ("done", "failed")]
        running = [s for s in self.sessions if s.status == "running"]
        for s in done:
            self._remove_worktree(s)
        if running:
            console.print(
                f"[yellow]{len(running)} session(s) still running — worktrees kept.[/yellow]"
            )
        console.print("[dim]Goodbye.[/dim]")
        sys.exit(0)

    # ── Main loop ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        init_db()

        from flow.commands.doctor import hook_health_ok, hook_health_one_liner
        if not hook_health_ok():
            console.print(Panel(
                hook_health_one_liner() + "\n\n"
                "Hooks are not firing — enforcement and cost tracking are disabled.\n"
                "Run [bold]flow doctor --fix[/bold] or [bold]flow init --force[/bold], then restart.",
                title="[bold red]Hook configuration issue[/bold red]",
                border_style="red",
            ))

        console.print(Panel(
            f"[bold cyan]AI Flow[/bold cyan] — {self.project} ({self.branch})\n"
            f"[dim]Type a task to start. Multiple tasks run in parallel. /help for commands.[/dim]",
            border_style="cyan",
        ))

        live = Live(refresh_per_second=4, screen=False, console=console)
        live.start()

        with patch_stdout(raw=True):
            while True:
                self._drain_queues()
                live.update(self._render_table())
                live.stop()

                try:
                    user_input = self.prompt_session.prompt(self._prompt_str()).strip()
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[dim]Use /quit to exit.[/dim]")
                    live.start()
                    continue

                live.start()

                if not user_input:
                    continue

                if user_input.startswith("/"):
                    self.handle_slash(user_input, live=live)
                    continue

                if self._try_dispatch_flow_cmd(user_input):
                    continue

                # New task
                session = self._start_session(user_input)
                console.print(
                    f"[dim]→ Session {session.idx} started on branch {session.branch}[/dim]"
                )

    def _try_dispatch_flow_cmd(self, user_input: str) -> bool:
        stripped = user_input.strip()
        if stripped == "flow":
            console.print("[yellow]Bare `flow` blocked — you're already in the REPL.[/yellow]")
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


def start_repl() -> None:
    FlowOrchestrator().start()
