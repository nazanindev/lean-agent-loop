"""CLI entry point — `flow` command."""
import typer
from typing import Optional

app = typer.Typer(
    name="flow",
    help="AI Flow — personal AI dev harness",
    no_args_is_help=False,
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Start the AI Flow REPL. Run with no arguments to enter interactive mode."""
    if ctx.invoked_subcommand is None:
        from autopilot.repl import start_repl
        start_repl()


@app.command()
def init(force: bool = typer.Option(False, "--force", help="Overwrite existing hooks")) -> None:
    """Wire AI Flow hooks into ~/.claude/settings.json and create ~/.autopilot/.env."""
    from autopilot.commands.init import cmd_init
    cmd_init(force=force)


@app.command()
def status() -> None:
    """Show current run state and today's cost."""
    from autopilot.commands.stats import cmd_status
    cmd_status()


@app.command()
def stats(project: Optional[str] = typer.Option(None, "--project", "-p", help="Filter by project name")) -> None:
    """Show cost breakdown by project and recent runs."""
    from autopilot.commands.stats import cmd_stats
    cmd_stats(project)


@app.command()
def ship() -> None:
    """Verify tests, commit with AI message, create PR with AI description."""
    from autopilot.commands.ship import cmd_ship
    cmd_ship()


@app.command()
def verify() -> None:
    """Run tests/lint for the current project."""
    from autopilot.commands.verify import cmd_verify
    cmd_verify()


@app.command()
def resume(run_id: Optional[str] = typer.Argument(None, help="Run ID to resume (shows picker if omitted)")) -> None:
    """Resume an interrupted run. Shows a picker if no run ID is given."""
    from autopilot.tracker import init_db, load_run, get_recent_runs, RunStatus
    from rich.console import Console
    c = Console()
    init_db()

    if not run_id:
        runs = [r for r in get_recent_runs(limit=10) if r["status"] != RunStatus.complete.value]
        if not runs:
            c.print("[yellow]No incomplete runs found.[/yellow]")
            raise typer.Exit()
        c.print("\n[bold]Recent incomplete runs:[/bold]")
        for i, r in enumerate(runs, 1):
            c.print(
                f"  [cyan]{i}.[/cyan] [{r['run_id']}] {r['goal'][:60]}  "
                f"[dim]{r['phase']} · ${r['cost_usd']:.4f}[/dim]"
            )
        run_id = typer.prompt("Run ID to resume")

    r = load_run(run_id)
    if not r:
        c.print(f"[red]Run {run_id} not found.[/red]")
        raise typer.Exit(1)

    c.print(f"[green]Resuming run {r.run_id}:[/green] {r.goal}")
    c.print(f"[dim]Phase: {r.phase.value} | Steps: {r.current_step}/{r.max_steps} | Cost: ${r.cost_usd:.4f}[/dim]")
    if r.plan_steps:
        c.print("\n[bold]Plan steps:[/bold]")
        for s in r.plan_steps:
            marker = "[green]✓[/green]" if s.get("status") == "done" else "○"
            c.print(f"  {marker} {s['description']}")
    c.print("\n[dim]Start the REPL with `flow` and use /resume to continue in an interactive session.[/dim]")


@app.command(name="ci-review")
def ci_review(
    diff: Optional[str] = typer.Option(None, "--diff", help="Path to diff file"),
    pr: Optional[int] = typer.Option(None, "--pr", help="PR number to review"),
) -> None:
    """AI code review for CI (two-pass Haiku→Sonnet). Designed for GitHub Actions."""
    from autopilot.commands.ci_review import cmd_ci_review
    cmd_ci_review(diff_path=diff, pr_number=pr)


@app.command()
def serve(port: int = typer.Option(7331, "--port", "-p")) -> None:
    """Start local API server for dashboard frontend."""
    from autopilot.commands.serve import cmd_serve
    cmd_serve(port)


@app.command()
def route(task: str = typer.Argument(..., help="Describe the task")) -> None:
    """Recommend which model tier to use for a task."""
    from autopilot.router import model_for
    from autopilot.tracker import Phase
    from rich.console import Console
    c = Console()
    model = model_for(Phase.execute, task)
    c.print(f"[bold]Recommended model:[/bold] {model}")
    c.print(f"[dim]For task:[/dim] {task}")
