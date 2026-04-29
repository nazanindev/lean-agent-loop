import os
from pathlib import Path
from dotenv import load_dotenv
import yaml

# Load from ~/.autopilot/.env (portable) then local .env (dev override)
load_dotenv(Path.home() / ".autopilot" / ".env")
load_dotenv(override=False)

DB_PATH = Path(os.getenv("AP_DB_PATH", "~/.autopilot/costs.duckdb")).expanduser()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_ROOT = Path(__file__).parent.parent.parent


def _load_yaml(name: str) -> dict:
    p = _ROOT / name
    if p.exists():
        with open(p) as f:
            return yaml.safe_load(f) or {}
    return {}


def routing() -> dict:
    return _load_yaml("routing.yaml")


def constraints() -> dict:
    return _load_yaml("constraints.yaml")


def model_for_phase(phase: str) -> str:
    r = routing()
    return r.get("phases", {}).get(phase, "claude-sonnet-4-6")


def get_project_id() -> str:
    """Normalized project ID from git remote, falls back to directory name."""
    import subprocess
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        # github.com/user/repo-name → repo-name
        return url.rstrip("/").rstrip(".git").split("/")[-1]
    except Exception:
        return Path.cwd().name


def get_branch() -> str:
    import subprocess
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"
