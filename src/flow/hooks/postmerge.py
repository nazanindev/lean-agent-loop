"""
Git post-merge hook for AI Flow.

When a local merge/pull happens, check whether the active run's PR is merged on
GitHub and auto-complete the run if so.
"""
from __future__ import annotations

import re
import subprocess

from flow.config import get_project_id
from flow.run_manager import complete_run
from flow.tracker import init_db, load_active_run


def _pr_number_from_url(pr_url: str) -> str:
    m = re.search(r"/pull/(\d+)", pr_url or "")
    return m.group(1) if m else ""


def main() -> int:
    init_db()
    project = get_project_id()
    run = load_active_run(project)
    if not run or not run.pr_url:
        return 0

    pr_number = _pr_number_from_url(run.pr_url)
    if not pr_number:
        return 0

    # Requires `gh` auth + network; fail-open if unavailable.
    result = subprocess.run(
        ["gh", "pr", "view", pr_number, "--json", "state,merged"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return 0

    output = (result.stdout or "").lower()
    is_merged = '"merged":true' in output or '"state":"merged"' in output
    if not is_merged:
        return 0

    complete_run(run)
    print(f"[flow] Auto-closed run {run.run_id}: PR merged ({run.pr_url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
