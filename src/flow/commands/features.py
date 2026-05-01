"""`flow features` — manage repo-local feature state machine."""
from __future__ import annotations

import subprocess
from typing import Optional

from rich.console import Console
from rich.table import Table

from flow.features import Feature, get_active_feature, load_features, save_features

console = Console()


def _load_or_empty() -> list[Feature]:
    return load_features()


def cmd_features_list() -> None:
    feats = _load_or_empty()
    if not feats:
        console.print("[yellow]No features.yaml entries found.[/yellow]")
        return
    t = Table(title="Features")
    t.add_column("ID")
    t.add_column("State")
    t.add_column("Behavior")
    t.add_column("Verification")
    for f in feats:
        t.add_row(f.id, f.state, f.behavior, f.verification)
    console.print(t)


def cmd_features_add(
    feature_id: str,
    behavior: str,
    verification: str,
    state: str = "not_started",
) -> None:
    feats = _load_or_empty()
    if any(f.id == feature_id for f in feats):
        raise SystemExit(f"feature {feature_id} already exists")
    feat = Feature(id=feature_id, behavior=behavior, verification=verification, state=state)
    feats.append(feat)
    save_features(feats)
    console.print(f"[green]✓ Added feature {feature_id}[/green]")


def cmd_features_active() -> None:
    feat = get_active_feature()
    if not feat:
        console.print("[yellow]No active feature.[/yellow]")
        return
    console.print(f"[bold]{feat.id}[/bold] {feat.behavior}")
    console.print(f"[dim]verify: {feat.verification}[/dim]")


def cmd_features_pick(feature_id: Optional[str] = None) -> None:
    feats = _load_or_empty()
    if not feats:
        raise SystemExit("no features defined; add one first")

    active = [f for f in feats if f.state == "active"]
    if active and (feature_id is None or active[0].id != feature_id):
        raise SystemExit(f"feature {active[0].id} is already active; verify or unblock it first")

    target: Optional[Feature] = None
    if feature_id:
        for f in feats:
            if f.id == feature_id:
                target = f
                break
        if not target:
            raise SystemExit(f"feature {feature_id} not found")
    else:
        for f in feats:
            if f.state == "not_started":
                target = f
                break
        if not target:
            raise SystemExit("no not_started feature to pick")

    for f in feats:
        if f.id == target.id:
            f.state = "active"
            f.blocked_reason = ""
        elif f.state == "active":
            f.state = "not_started"
    save_features(feats)
    console.print(f"[green]✓ Active feature: {target.id}[/green] {target.behavior}")


def cmd_features_verify(feature_id: Optional[str] = None) -> None:
    feats = _load_or_empty()
    if not feats:
        raise SystemExit("no features defined")

    target: Optional[Feature] = None
    if feature_id:
        for f in feats:
            if f.id == feature_id:
                target = f
                break
    else:
        target = get_active_feature()

    if not target:
        raise SystemExit("no target feature found (pass --id or set active feature)")

    if target.state != "active":
        raise SystemExit(f"feature {target.id} is {target.state}; only active can transition to passing")

    console.print(f"[dim]→ Running: {target.verification}[/dim]")
    result = subprocess.run(target.verification, shell=True, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        for f in feats:
            if f.id == target.id:
                f.state = "blocked"
                f.blocked_reason = "verification command failed"
        save_features(feats)
        console.print(f"[red]✗ Feature {target.id} blocked[/red]")
        if output:
            console.print(f"[dim]{output[-2000:]}[/dim]")
        raise SystemExit(1)

    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    for f in feats:
        if f.id == target.id:
            f.state = "passing"
            f.evidence = f"verified via `{target.verification}` @ {sha}"
            f.blocked_reason = ""
    save_features(feats)
    console.print(f"[green]✓ Feature {target.id} marked passing[/green]")
