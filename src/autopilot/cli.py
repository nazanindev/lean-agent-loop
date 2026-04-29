"""CLI entry point — `ap` command."""
import typer
from typing import Optional

app = typer.Typer(
    name="ap",
    help="Autopilot — personal AI dev harness",
    no_args_is_help=False,
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Start the Autopilot REPL. Run with no arguments to enter interactive mode."""
    if ctx.invoked_subcommand is None:
        from autopilot.repl import start_repl
        start_repl()


@app.command()
def init(force: bool = typer.Option(False, "--force", help="Overwrite existing hooks")) -> None:
    """Wire Autopilot hooks into ~/.claude/settings.json and create ~/.autopilot/.env."""
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
    """Commit changes, create PR with AI-generated description."""
    from autopilot.commands.ship import cmd_ship
    cmd_ship()


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
