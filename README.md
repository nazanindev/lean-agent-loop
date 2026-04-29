# Autopilot (`ap`)

Personal AI dev harness built around Claude Code. Tracks cost passively, controls subagent spawning, manages run state explicitly, and automates the PR + review workflow.

## The problem

- No visibility into AI spend without manually checking the UI
- Claude spawns subagents freely — each starts cold and multiplies cost
- Switching between Opus (planning) and Sonnet (execution) is manual cognitive overhead
- Creating PRs and triggering code reviews is friction after every task

## How it works

```
ap REPL → clarify → Claude Code session (hooks track cost + gate subagents) → ap ship → GH Actions review
```

State lives in an explicit **RunState machine** (DuckDB), not Claude's chat history. Every session gets a structured briefing injected — not a transcript. This makes context cheap, runs resumable, and cost attributable.

## Install

```sh
pip install -e .
echo 'export PATH="$HOME/Library/Python/3.9/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

Copy and fill in env vars:
```sh
cp .env.example .env
```

Required:
- `ANTHROPIC_API_KEY` — your Anthropic key
- `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` — free at [cloud.langfuse.com](https://cloud.langfuse.com) (optional but recommended for observability)

## Usage

### Interactive REPL

```sh
ap
```

Type a task in natural language. Autopilot asks clarifying questions, picks the right model, launches Claude Code, and tracks cost.

```
ap [project:lean-agent-loop] > add JWT authentication to the API

Before starting, a few questions:
1. JWT or session-based?
2. Social login needed?

Your answers: JWT only, no social

→ Phase: plan | Model: claude-opus-4-5 | run: a3f2b1c4
```

### Slash commands

| Command | Effect |
|---|---|
| `/plan` | Switch to Opus (planning phase) |
| `/exec` | Switch to Sonnet (execution phase) |
| `/fast` | Switch to Haiku (quick tasks) |
| `/model opus\|sonnet\|haiku` | Force a specific model |
| `/no-agents` | Toggle subagent spawn blocking |
| `/budget $X` | Set session budget gate |
| `/new` | Compress context, start fresh session with RunState injected |
| `/compact` | Same as `/new` |
| `/resume <run_id>` | Resume an interrupted run |
| `/skip-plan` | Skip planning, go straight to execute |
| `/done` | Mark current run complete |
| `/status` | Show run state + cost |
| `/quit` | Exit |

### One-off commands

```sh
ap status              # today's cost + active run
ap stats               # cost breakdown by project
ap stats --project foo # filter by project
ap route "review PR"   # recommend model tier for a task
ap ship                # commit + create PR (Day 2)
ap serve               # start local API on :7331 (Day 2)
```

## Phase routing

| Phase | Model | When |
|---|---|---|
| Plan | `claude-opus-4-5` | Architecture, design, first session on a task |
| Execute | `claude-sonnet-4-6` | Implementation when plan exists |
| Fast | `claude-haiku-4-5` | Quick questions, CI tasks |

Override any time with `/model` or edit `routing.yaml`.

## Hard constraints

Configured in `constraints.yaml` and enforced via Claude Code's `PreToolUse` hook:

```yaml
max_steps_per_run: 20        # blocks further tool calls if exceeded
budget_gate_usd: 2.00        # blocks Agent spawns above this
allowed_bash_commands: [...]  # whitelist — unlisted commands are blocked
agent_spawns_allowed_in: [plan]  # subagents only during planning phase
```

The model is treated as an untrusted subprocess. Constraints are enforced, not hoped for.

## Observability

Every Claude Code session is traced to [Langfuse](https://cloud.langfuse.com) with:
- Project (from `git remote get-url origin`)
- Phase, run ID, step
- Token usage + cost
- Subagent spawn events (allowed or blocked)

Cost is also stored locally in `~/.autopilot/costs.duckdb` and queryable via `ap stats`.

## Hooks

Hooks are wired globally in `~/.claude/settings.json` so they run in every Claude Code session across all your projects.

| Hook | File | Purpose |
|---|---|---|
| `Stop` | `hooks/stop.py` | Captures tokens/cost → DuckDB + Langfuse |
| `PreToolUse` | `hooks/pretool.py` | Step counter, bash whitelist, Agent gate, budget gate |
| `PreCompact` | `hooks/precompact.py` | Custom compaction prompt — preserves RunState artifacts |

## Cross-repo use

`ap` installs globally. The cost DB at `~/.autopilot/costs.duckdb` and hooks in `~/.claude/settings.json` work across all your projects automatically. Project is identified by git remote URL so costs are attributed correctly per repo.

## Day 2 (coming)

- `ap ship` — AI commit message + `gh pr create` with AI PR description
- GitHub Actions AI code reviewer — two-pass Haiku→Sonnet, posts structured PR comment
- `ap serve` — FastAPI on `:7331` for cost dashboard frontend
- Verification layer — auto-run tests + lint after execute phase
- `ap resume` — pick up interrupted runs from last successful step
