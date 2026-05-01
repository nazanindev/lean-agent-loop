# AGENTS.md

This repository builds `flow`: a CLI harness for long-running AI coding sessions.
The goal is reliable session execution and clean handoffs, not maximum code output.

## Quick Start

- Install: `pip install -e .`
- Initialize hooks: `flow init`
- Start REPL: `flow`

## Verification

- Primary check: `flow verify`
- Python sanity: `python3 -m compileall src/flow`

Never treat code as complete until verification passes.

## Runtime Model

- State machine: `plan -> execute -> verify -> ship`
- Session state is persisted in DuckDB (`~/.autopilot/costs.duckdb`)
- Context is injected from structured run artifacts, not raw chat transcripts

## Hard Constraints

- Enforce constraints via hooks, not prompt-only instructions
- Respect `constraints.yaml` for step budgets, spawn gates, and spend gates
- Avoid destructive git operations unless explicitly requested
- Keep work scoped to the current run phase

## Key Files

- `README.md` - product overview and usage
- `constraints.yaml` - hard runtime limits and gating rules
- `routing.yaml` - phase and keyword model routing
- `src/flow/repl.py` - interactive runtime loop
- `src/flow/hooks/pretool.py` - pre-tool enforcement gate
- `src/flow/hooks/stop.py` - stop hook usage tracking and clean-state checks
- `src/flow/tracker.py` - persistent state store

## Session Exit Expectations

Before ending implementation work:

1. Run verification (`flow verify`).
2. Ensure no stale debug artifacts remain in the working tree.
3. Leave the repo in a restartable state for the next session.
