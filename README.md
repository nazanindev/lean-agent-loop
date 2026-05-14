# `flow`

**Prompt-to-PR autopilot for Claude Code.**

Type a task. Get a PR. Run multiple tasks in parallel. Everything in between is automatic.

```
flow [$0.00] > add JWT authentication to the API

→ Session 1 started on branch flow-add-jwt-authentication-a3f2

flow [$0.03 | 1 running] > fix the race condition in the queue processor

→ Session 2 started on branch flow-fix-the-race-condition-in-b1e9

  flow  |  $0.03 today  |  2 running

  #   Task                             Phase    Steps   Cost    Last output
  1   add JWT authentication to the…  execute  4/8     $0.02   → writing auth.py
  2   fix race condition in queue p…   plan     —       $0.01   Claude: Here's my plan...

flow [$0.11 | 2 running] > /view 1

─── Session 1: add JWT authentication to the API ───
→ claude-opus-4-7 | plan
Claude: Here's the plan:
1. Add PyJWT dependency
2. Implement auth middleware
3. Protect routes
4. Add tests

✓ Plan captured — executing
→ claude-sonnet-4-6 | execute
Claude: ...
✓ Steps done (4/8)

[1:add JWT authentica] (read-only) > /back

← Back to orchestrator

flow [$0.24 | 1 running] > _
```

When a session finishes:

```
✓ Verification passed
✓ Code review passed
→ Shipping...
✓ PR created: https://github.com/you/repo/pull/42

API: $0.12 this run / $0.24 today
```

---

## Why

Plain `claude` sessions have no cost visibility, no bounds on agent spawning, and no automated path to a PR. `flow` adds all of that without adding friction:

- **Orchestrator view** — live table of all running sessions with phase, steps, cost, and last output
- **Parallel sessions** — each task runs in its own git worktree + branch; no file conflicts
- **Drill-down** — `/view N` shows a session's full output history + live tail
- **Cost visible at all times** — API spend in the prompt, per-run, per-project
- **Hard limits enforced by hooks** — step budgets, bash allowlist, agent spawn gates; the model can't bypass them
- **Automatic pipeline** — verify → fix loop → code review → ship; no manual phase management
- **Smart agent gating** — read-only subagents always allowed; write-capable ones gated by spend tier

The PR is the review gate. `flow` doesn't add another one.

---

## How it works

```
flow REPL → sessions (each: claude -p in worktree + hooks) → auto verify / check / ship → PR
```

| Property | Mechanism |
|---|---|
| **Parallel** | Each task gets a git worktree + branch. Sessions run as background threads with live status in the table. |
| **Cost-aware** | Two billing surfaces tracked separately: subscription quota (msgs/window) + API USD (utility calls). Spend gate blocks writes over budget. |
| **Bounded** | Weighted step budgets per phase, bash allowlist, subagent spawn policy enforced via `PreToolUse` hook — not prompts. |
| **Automatic** | All steps done → verify → auto-remediate if failing (capped) → code review → ship. No manual gates. |

State lives in DuckDB (`~/.autopilot/costs.duckdb`), not chat history. Each session gets a structured briefing injected so runs are resumable and cost is attributable.

---

## Prerequisites

- [Claude Code](https://claude.ai/code) CLI installed and authenticated (`claude login`)
- Python 3.9+
- [`gh`](https://cli.github.com) CLI (for `flow ship` and the GH Actions reviewer)
- A GitHub repo with a remote set as `origin`
- An Anthropic API key (for utility calls — `flow ship`, `flow ci-review`, `flow check`)

---

## Install

```sh
pip install -e .
flow init
```

`flow init` writes hooks into `~/.claude/settings.json` and creates `~/.autopilot/.env`:

```sh
ANTHROPIC_API_KEY=sk-ant-...         # for flow utility calls (ship, ci-review, check)
AP_PLAN=pro                          # claude.ai plan: pro | max5 | max20 | api_only

LANGFUSE_PUBLIC_KEY=pk-lf-...        # optional
LANGFUSE_SECRET_KEY=sk-lf-...        # optional

AP_DB_PATH=~/.autopilot/costs.duckdb # optional
AP_BUDGET_USD=1.00                   # optional — override API spend gate
```

If `flow` isn't found after install:

```sh
echo 'export PATH="$HOME/Library/Python/3.9/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

---

## Usage

### REPL — primary interface

```sh
flow
```

Type a task. A new session starts immediately in a background thread with its own git worktree. Type another task while the first is running — they run in parallel, each visible in the live table.

```
flow [$0.14 | exec 4/8] > _
```

The prompt shows API spend today and how many sessions are running.

### Slash commands

Ten commands. Visibility and emergency brakes only — the pipeline handles everything else.

| Command | Effect |
|---|---|
| `/view N` | Drill into session N — full output history + live tail |
| `/back` | Return to orchestrator view (from drill-down) |
| `/sessions` | List all sessions with status |
| `/status` | Cost, quota window, all sessions |
| `/model opus\|sonnet\|haiku` | Force model for new sessions |
| `/no-agents` | Toggle subagent spawning |
| `/budget $X` | Set API spend cap |
| `/stop [N]` | Send stop signal to session N (or all running) |
| `/resume [run_id]` | Attach to an interrupted run |
| `/quit` | Exit (cleans up completed worktrees) |

### CLI commands — scripting and CI only

These are designed for use outside the REPL (scripts, CI pipelines, one-off checks). Use slash commands inside the REPL.

```sh
flow status              # quota window + API spend + active run
flow stats               # usage breakdown by project
flow stats --project foo
flow route "review PR"   # recommend model tier for a task
flow verify              # run tests/lint
flow check               # AI code review on git diff HEAD (--json for structured output)
flow ship                # verify → commit → PR
flow ship --branch-name feat/x --pr-title "My title"
flow resume [run-id]     # resume interrupted run (CLI shorthand)
flow serve               # local dashboard on :7331
flow ci-review --pr 42   # AI review for CI (GitHub Actions)
flow ci-review --diff path/to/file.diff
flow doctor              # check hook health
flow doctor --fix        # rewrite hooks for current interpreter
flow features list
flow features add F01 "POST /x returns 201" --verify "pytest tests/test_x.py -x"
flow features pick       # set active feature (WIP=1)
flow features verify
```

---

## Phase routing

| Phase | Default model | Notes |
|---|---|---|
| Plan | `claude-opus-4-7` | Architecture, design, first pass |
| Execute | `claude-sonnet-4-6` | Implementation |
| Verify / CI | `claude-haiku-4-5-20251001` | Lightweight tasks, code review |

Keyword overrides in `routing.yaml` are scanned before phase routing. Override with `/model` for all new sessions.

---

## Agent spawn policy

`constraints.yaml` sets `agent_spawn_policy: smart` by default:

| Agent type | Condition | Decision |
|---|---|---|
| Read-only tools only | any | Always allowed |
| Write-capable, low spend | `< gate × 0.5` | Allowed any phase |
| Write-capable, medium spend | `≥ gate × 0.5` | Allowed in `agent_spawns_allowed_in` phases |
| Write-capable, high spend | `≥ gate` | Blocked |

Set `agent_spawn_policy: phase_only` to revert to the legacy phase-whitelist behavior.

---

## Auto-pipeline config

All on by default in `constraints.yaml`:

```yaml
auto_verify_on_steps_complete: true   # run verify when all plan steps done
auto_check_before_ship: true          # run code review before ship
auto_remediate: true                  # spawn fix worker on failure
auto_remediate_max_tries: 2           # cap before surfacing to user
```

---

For engineering internals, billing surfaces, observability, and the style system, see [ENGINEERING.md](docs/ENGINEERING.md).
