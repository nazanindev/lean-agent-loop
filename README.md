# Autopilot (`ap`)

A cost-aware CLI workflow for AI-assisted development: prompt → patch → PR → review → merge. Optimized for minimal context, controlled token usage, and human-in-the-loop iteration.

---

## The problem

AI coding tools are powerful but expensive and hard to control:

- No visibility into spend without manually checking the UI
- Claude spawns subagents freely — each starts cold and multiplies cost
- Switching between Opus (planning) and Sonnet (execution) is manual cognitive overhead
- Creating PRs and triggering code reviews is friction after every task
- Long conversations bloat context and inflate cost with no structured way to compress

Autopilot treats the model as an **untrusted subprocess**: every constraint is enforced by hooks, not hoped for.

---

## How it works

```
ap REPL → clarify → Claude Code session (hooks track cost + gate subagents) → ap ship → GH Actions review
```

State lives in an explicit **RunState machine** backed by DuckDB, not Claude's chat history. Every session gets a structured briefing injected — not a transcript. This keeps context cheap, runs resumable, and cost attributable.

### RunState lifecycle

```
clarify → plan → execute → verify → ship
```

Each phase selects a different model tier and enforces different constraints. Phase transitions are explicit — either from a slash command or from autopilot routing based on task keywords.

### Hooks

Three hooks run globally across **every Claude Code session** via `~/.claude/settings.json`:

| Hook | File | Purpose |
|---|---|---|
| `Stop` | `hooks/stop.py` | Captures token usage + cost → DuckDB + Langfuse on session end |
| `PreToolUse` | `hooks/pretool.py` | Step counter, bash allowlist, Agent spawn gate, budget gate |
| `PreCompact` | `hooks/precompact.py` | Injects custom compaction prompt that preserves RunState artifacts |

Because hooks are wired globally, Autopilot's cost tracking and constraints apply to Claude Code sessions in **any repo** on your machine — not just this one.

---

## Prerequisites

- [Claude Code](https://claude.ai/code) CLI installed and authenticated
- Python 3.9+
- [`gh`](https://cli.github.com) CLI (for `ap ship` and the GH Actions reviewer)
- A GitHub repo with a remote set as `origin`

---

## Install

```sh
pip install -e .
ap init
```

`ap init` writes the hooks into `~/.claude/settings.json` and creates `~/.autopilot/.env` with a template. Fill in your keys:

```sh
# ~/.autopilot/.env
ANTHROPIC_API_KEY=sk-ant-...
LANGFUSE_PUBLIC_KEY=pk-lf-...   # optional — free at cloud.langfuse.com
LANGFUSE_SECRET_KEY=sk-lf-...   # optional
```

If `ap` isn't found after install, add Python's user bin to your PATH:

```sh
echo 'export PATH="$HOME/Library/Python/3.9/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

---

## Usage

### Interactive REPL

```sh
ap
```

Type a task in natural language. Autopilot asks clarifying questions, routes to the right model, launches Claude Code, and tracks cost.

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

### CLI commands

```sh
ap                     # launch interactive REPL
ap status              # today's cost + active run
ap stats               # cost breakdown by project
ap stats --project foo # filter by project
ap route "review PR"   # recommend model tier for a task description
ap ship                # commit + create PR (Day 2)
ap serve               # start local API on :7331 (Day 2)
```

---

## Phase routing

Autopilot automatically selects a model based on the current phase. You can override at any time with `/model` or by editing `routing.yaml`.

| Phase | Model | When |
|---|---|---|
| Plan | `claude-opus-4-5` | Architecture, design, first session on a task |
| Execute | `claude-sonnet-4-6` | Implementation once a plan exists |
| Fast / CI | `claude-haiku-4-5` | Quick questions, lightweight tasks |

### Keyword overrides

Task descriptions are scanned for keywords before phase routing kicks in:

| Keyword | Model |
|---|---|
| `architecture`, `design` | Opus |
| `refactor`, `review`, `test`, `fix` | Sonnet |
| `quick`, `explain` | Haiku |

---

## Hard constraints

Configured in `constraints.yaml` and enforced via the `PreToolUse` hook — not prompted, not hoped for:

```yaml
max_steps_per_run: 20        # blocks further tool calls once exceeded
max_tokens_per_step: 8000    # per-step token ceiling
budget_gate_usd: 2.00        # blocks Agent spawns when cumulative cost exceeds this
context_warn_pct: 0.80       # warns when context window is 80% full

allowed_bash_commands:       # allowlist — unlisted commands are blocked
  - git, pytest, python, uv, pip, npm, npx, gh, cat, ls, find, grep ...

agent_spawns_allowed_in:     # subagents only during planning phase
  - plan

allowed_write_paths:         # write operations restricted to project root
  - "./"
```

---

## Observability

Every Claude Code session is traced to [Langfuse](https://cloud.langfuse.com) (free tier available) with:

- Project (derived from `git remote get-url origin`)
- Phase, run ID, step count
- Token usage + cost per session
- Subagent spawn events (allowed or blocked)

Cost is also stored locally in `~/.autopilot/costs.duckdb` and queryable at any time via `ap stats` — no external dependency required.

---

## Cross-repo use

`ap` installs globally. The cost DB at `~/.autopilot/costs.duckdb` and hooks in `~/.claude/settings.json` work across all your projects automatically. Project is identified by git remote URL so costs are attributed correctly per repo.

---

## Day 2 (coming)

- `ap ship` — AI-generated commit message + `gh pr create` with AI-written PR description
- GitHub Actions AI code reviewer — two-pass Haiku → Sonnet, posts structured PR comment
- `ap serve` — FastAPI on `:7331` for a local cost dashboard
- Verification layer — auto-run tests + lint after execute phase, blocks ship if failing
- `ap resume` — pick up interrupted runs from the last successful step
