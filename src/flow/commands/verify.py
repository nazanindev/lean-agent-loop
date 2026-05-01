"""Verification gate — auto-detect and run tests/lint before shipping."""
import json
import subprocess
from pathlib import Path
from typing import Tuple, Optional

from rich.console import Console

console = Console()


def detect_runner(cwd: Path = None) -> Optional[str]:
    """Return the test command for the project, or None if none detected."""
    cwd = cwd or Path.cwd()

    # Python: pytest
    for marker in ("pytest.ini", "setup.cfg", "tox.ini"):
        if (cwd / marker).exists():
            return "pytest"
    pyproject = cwd / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text()
        if "[tool.pytest" in content:
            return "pytest"

    # Node: npm test (only if test script is defined)
    pkg = cwd / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            if data.get("scripts", {}).get("test"):
                return "npm test"
        except Exception:
            pass

    # Makefile with a test target
    makefile = cwd / "Makefile"
    if makefile.exists():
        content = makefile.read_text()
        if "test:" in content or "test :" in content:
            return "make test"

    return None


def run_checks(cwd: Path = None) -> Tuple[bool, str]:
    """Run the detected test suite. Returns (passed, output)."""
    cwd = cwd or Path.cwd()
    runner = detect_runner(cwd)

    if runner is None:
        msg = "No test runner detected — skipping verification."
        console.print(f"[yellow]{msg}[/yellow]")
        return True, msg

    console.print(f"[dim]→ Running: {runner}[/dim]")
    try:
        result = subprocess.run(
            runner,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = (result.stdout + result.stderr).strip()
        passed = result.returncode == 0
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "Verification timed out after 300s."
    except Exception as e:
        return False, f"Verification error: {e}"


def cmd_verify() -> None:
    """Run verification checks and print the result."""
    from autopilot.tracker import init_db, load_active_run
    from autopilot.config import get_project_id
    from autopilot.run_manager import advance_phase
    from autopilot.tracker import Phase

    init_db()
    project = get_project_id()
    run = load_active_run(project)

    if run:
        advance_phase(run, Phase.verify)

    passed, output = run_checks()

    if passed:
        console.print("[green]✓ Verification passed[/green]")
        if run:
            advance_phase(run, Phase.ship)
    else:
        console.print("[red]✗ Verification failed[/red]")
        console.print(f"[dim]{output[-2000:]}[/dim]")

    if not passed:
        raise SystemExit(1)
