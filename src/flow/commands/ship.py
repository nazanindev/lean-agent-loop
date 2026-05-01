"""flow ship — verify → AI commit message → git commit → AI PR body → gh pr create."""
import os
import subprocess
from pathlib import Path

import anthropic
from rich.console import Console
from rich.panel import Panel

from flow.billing import metered_call
from flow.config import get_project_id, load_style, style_prompt
from flow.tracker import init_db, load_active_run, Phase, RunStatus
from flow.run_manager import advance_phase, complete_run, save_pr_url
from flow.commands.verify import run_checks

console = Console()

HAIKU = "claude-haiku-4-5-20251001"


def _git(args: list, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, capture_output=True, text=True, check=check)


def _gh(args: list, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["gh"] + args, capture_output=True, text=True, check=check)


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))


def _generate_commit_message(diff: str, style: dict, run_id: str) -> str:
    system = style_prompt(style, ["commit_message"]) or "Write a short, imperative commit message."
    system += "\nOutput ONLY the commit message — no explanation, no code fences."

    resp = metered_call(
        _client(), HAIKU,
        run_id=run_id, purpose="commit_msg",
        max_tokens=120,
        system=system,
        messages=[{"role": "user", "content": f"Git diff:\n\n{diff[:8000]}"}],
    )
    return resp.content[0].text.strip().strip('"').strip("'")


def _generate_pr_body(run, diff: str, style: dict) -> tuple[str, str]:
    """Returns (title, body)."""
    system_title = style_prompt(style, ["pr_title"]) or "Write a plain PR title, sentence case."
    system_title += "\nOutput ONLY the title — one line, no explanation."

    system_body = style_prompt(style, ["pr_body"]) or "Write a clear PR description."
    system_body += "\nOutput ONLY the PR body in markdown."

    plan_summary = ""
    if run and run.plan_steps:
        done = [s["description"] for s in run.plan_steps if s.get("status") == "done"]
        pending = [s["description"] for s in run.plan_steps if s.get("status") != "done"]
        if done:
            plan_summary += "\nCompleted steps:\n" + "\n".join(f"- {d}" for d in done)
        if pending:
            plan_summary += "\nRemaining:\n" + "\n".join(f"- {p}" for p in pending)

    context = f"""Goal: {run.goal if run else 'unknown'}
Decisions: {run.decisions if run else []}
Artifacts: {run.artifacts if run else []}
{plan_summary}

Diff (first 6000 chars):
{diff[:6000]}"""

    client = _client()
    run_id = run.run_id if run else "none"

    title_resp = metered_call(
        client, HAIKU,
        run_id=run_id, purpose="pr_title",
        max_tokens=80, system=system_title,
        messages=[{"role": "user", "content": context}],
    )
    body_resp = metered_call(
        client, HAIKU,
        run_id=run_id, purpose="pr_body",
        max_tokens=600, system=system_body,
        messages=[{"role": "user", "content": context}],
    )

    return (
        title_resp.content[0].text.strip().strip('"'),
        body_resp.content[0].text.strip(),
    )


def cmd_ship() -> None:
    init_db()
    project = get_project_id()
    run = load_active_run(project)
    style = load_style()
    run_id = run.run_id if run else "none"

    # ── 1. Verify gate ────────────────────────────────────────────────────────
    console.print("[bold]Running verification...[/bold]")
    passed, output = run_checks()
    if not passed:
        console.print("[red]✗ Verification failed — fix tests before shipping.[/red]")
        console.print(f"[dim]{output[-2000:]}[/dim]")
        raise SystemExit(1)
    console.print("[green]✓ Verification passed[/green]")

    # ── 2. Get diff ───────────────────────────────────────────────────────────
    diff_result = _git(["diff", "HEAD"], check=False)
    diff = diff_result.stdout.strip()
    if not diff:
        diff_result = _git(["diff", "--cached"], check=False)
        diff = diff_result.stdout.strip()
    if not diff:
        console.print("[yellow]Nothing to commit — working tree is clean.[/yellow]")
        raise SystemExit(0)

    # ── 3. Generate commit message ────────────────────────────────────────────
    console.print("[dim]Generating commit message...[/dim]")
    commit_msg = _generate_commit_message(diff, style, run_id)
    console.print(f"[bold]Commit:[/bold] {commit_msg}")

    # ── 4. Stage and commit ───────────────────────────────────────────────────
    _git(["add", "-A"])
    commit_result = _git(["commit", "-m", commit_msg], check=False)
    if commit_result.returncode != 0:
        console.print(f"[red]git commit failed:[/red] {commit_result.stderr}")
        raise SystemExit(1)
    console.print("[green]✓ Committed[/green]")

    # ── 5. Generate PR title + body ───────────────────────────────────────────
    console.print("[dim]Generating PR description...[/dim]")
    pr_title, pr_body = _generate_pr_body(run, diff, style)
    console.print(f"[bold]PR title:[/bold] {pr_title}")

    # ── 6. Push and create PR ─────────────────────────────────────────────────
    branch_result = _git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    branch = branch_result.stdout.strip() or "HEAD"

    push_result = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        capture_output=True, text=True,
    )
    if push_result.returncode != 0:
        console.print(f"[red]git push failed:[/red] {push_result.stderr}")
        raise SystemExit(1)

    pr_result = _gh(["pr", "create", "--title", pr_title, "--body", pr_body], check=False)
    if pr_result.returncode != 0:
        console.print(f"[red]gh pr create failed:[/red] {pr_result.stderr}")
        raise SystemExit(1)

    pr_url = pr_result.stdout.strip()
    console.print(f"[green]✓ PR created:[/green] {pr_url}")

    # ── 7. Advance run to ship + complete ─────────────────────────────────────
    if run:
        save_pr_url(run, pr_url)
        advance_phase(run, Phase.ship)
        complete_run(run)
        console.print(Panel(
            f"[bold green]Run complete[/bold green]\n"
            f"Goal: {run.goal[:80]}\n"
            f"API spend: ${run.cost_usd:.4f} | Budget used: {run.step_budget_used:.1f} steps\n"
            f"Subscription: {run.subscription_msgs} msgs | "
            f"{(run.subscription_tokens_in + run.subscription_tokens_out):,} tokens\n"
            f"PR: {pr_url}",
            border_style="green",
        ))
