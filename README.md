# AI Flow (`flow`)

**🚧 Status: Prototype** — workflow is implemented; iterating on end-to-end stability and integration.

A CLI harness for cost-aware AI-assisted development: prompt → patch → PR → review → merge. Optimized for minimal context, controlled token usage, and human-in-the-loop iteration.

---

## The problem

- Limited visibility into real-time token usage and API spend
- Uncontrolled subagent spawning increases cost and resets context
- Manual switching between models adds cognitive overhead
- Creating PRs and triggering code reviews is friction after every task
- Long-running conversations bloat context with no structured compression

AI Flow treats the model as an **untrusted subprocess**: every constraint is enforced by hooks, not hoped for.

---

## How it works

```
flow REPL → Claude Code session (hooks track quota + gate subagents) → flow ship → GH Actions review
```

State lives in an explicit **RunState machine** backed by DuckDB, not Claude's chat history. Every session gets a structured briefing injected — not a transcript. This keeps context cheap, runs resumable, and cost attributable.

### RunState lifecycle

```
plan → execute → verify → ship
```

Each phase selects a different model tier and enforces different constraints. Phase transitions are explicit — either from a slash command or from AI Flow routing based on task keywords.

### Hooks

Three hooks run for `flow` sessions via `~/.claude/settings.json`:

| Hook | File | Purpose |
|---|---|---|
| `Stop` | `hooks/stop.py` | Captures token usage → subscription quota window (DuckDB + Langfuse) on session end |
| `PreToolUse` | `hooks/pretool.py` | Step counter, bash allowlist, Agent spawn gate, API spend gate, quota warnings |
| `PreCompact` | `hooks/precompact.py` | Injects custom compaction prompt that preserves RunState artifacts |

Hooks only fire when you launch Claude Code through `flow` — regular `claude` sessions are unaffected.

---

## Two billing surfaces

Autopilot tracks two distinct cost surfaces separately — mixing them up produces meaningless numbers:

| Surface | Auth | Billing | What ap tracks |
|---|---|---|---|
| **Claude Code sessions** | `claude login` (claude.ai Pro/Max) | Flat subscription — $0 per session | 5-hour quota window msgs + tokens |
| **ap utility calls** | `ANTHROPIC_API_KEY` | Per-token API billing | Real USD per call (ship, ci-review) |

**Why the split matters:** Claude Code interactive sessions (the big coding loop) run against your Pro/Max subscription. They cost you $0 marginal, but they burn through your 5-hour message window. The `ap` CLI itself makes a handful of direct SDK calls per PR — those are metered and cost real money (typically cents, Haiku-heavy).

**Trust boundary:** If you flip to API mode (`AP_FORCE_API_KEY=1`), set a workspace spend cap in the [Anthropic console](https://console.anthropic.com) — autopilot's gates don't protect against runaway in-session spend. The guards here only gate the ap utility calls.

---


## Prerequisites

- [Claude Code](https://claude.ai/code) CLI installed and authenticated (`claude login`)
- Python 3.9+
- [`gh`](https://cli.github.com) CLI (for `flow ship` and the GH Actions reviewer)
- A GitHub repo with a remote set as `origin`
- An Anthropic API key (for flow utility calls only — `flow ship`, `flow ci-review`)

---

## Install

```sh
pip install -e .
flow init
```

`flow init` writes the hooks into `~/.claude/settings.json` and creates `~/.autopilot/.env` with a template. Fill in your keys:

```sh
# ~/.autopilot/.env
ANTHROPIC_API_KEY=sk-ant-...         # for flow utility calls (ship, ci-review, clarify)
AP_PLAN=pro                          # your claude.ai plan: pro | max5 | max20 | api_only

LANGFUSE_PUBLIC_KEY=pk-lf-...        # optional — free at cloud.langfuse.com
LANGFUSE_SECRET_KEY=sk-lf-...        # optional
```

If `flow` isn't found after install, add Python's user bin to your PATH:

```sh
echo 'export PATH="$HOME/Library/Python/3.9/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

---

## Usage

### Interactive REPL

```sh
flow
```

Type a task in natural language. AI Flow runs a short **structured intake** in the REPL (optional fields), then runs **Claude Code headlessly** (`claude -p` with JSON output): each turn is one bounded agentic pass, the reply is printed in the terminal, and the same Claude session is **resumed** on the next message until you `/done`. Hooks (`PreToolUse`, `Stop`, …) only run when `flow` sets `AP_ACTIVE=1` on that subprocess, so a normal interactive `claude` session elsewhere is unaffected. Subscription quota + API utility spend are still tracked.

```
flow [plan:sonnet|step:0/30|wt:0.0|api:$0.00|quota:3/45] > add JWT authentication to the API

Quick intake — press Enter to skip any field.
  Acceptance criteria: …

→ Claude headless (claude-opus-4-5) | phase: plan | run: a3f2b1c4
```

### Slash commands

| Command | Effect |
|---|---|
| `/plan` | Switch to Opus (planning phase) |
| `/exec` | Switch to Sonnet (execution phase) |
| `/fast` | Switch to Haiku (quick tasks) |
| `/model opus\|sonnet\|haiku` | Force a specific model |
| `/no-agents` | Toggle subagent spawn blocking |
| `/budget $X` | Set API spend gate (applies to utility calls) |
| `/new` | Compress context, start fresh session with RunState injected |
| `/compact` | Same as `/new` |
| `/resume [run_id]` | Resume an interrupted run (picker if no ID given) |
| `/skip-plan` | Skip planning, go straight to execute |
| `/verify` | Run tests/lint for current project |
| `/ship` | Verify → commit → create PR |
| `/done` | Mark current run complete |
| `/status` | Show quota window + API spend + run state |
| `/quit` | Exit |

### CLI commands

```sh
flow                     # launch interactive REPL
flow status              # quota window + API spend today + active run
flow stats               # usage breakdown by project
flow stats --project foo # filter by project
flow route "review PR"   # recommend model tier for a task description
flow verify              # run tests/lint for the current project
flow ship                # verify → AI commit message → git commit → AI PR description → gh pr create
flow resume [run-id]     # resume an interrupted run (shows picker if no ID given)
flow serve               # local dashboard on :7331
flow ci-review --pr 42   # AI code review for a PR (used by GitHub Actions)
```

---

## Phase routing

AI Flow automatically selects a model based on the current phase. You can override at any time with `/model` or by editing `routing.yaml`.

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

# Claude Code subscription quota (warns, doesn't hard-block — Anthropic enforces the real cap)
subscription_quota_warn_pct: 0.80   # warn at 80% of 5-hour window
plan_window_caps:
  pro:   { msgs: 45 }    # ~45 msgs per 5h window on Pro
  max5:  { msgs: 225 }   # Max 5x
  max20: { msgs: 900 }   # Max 20x

# flow utility API spend (hard gate — blocks Agent spawns)
api_spend_gate_usd: 1.00    # blocks Agent spawns if flow utility $ today >= this

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
- Token usage per session (subscription surface: $0, tokens only)
- API spend per utility call (real $)
- Subagent spawn events (allowed or blocked)

Cost is also stored locally in `~/.autopilot/costs.duckdb` and queryable at any time via `flow stats` — no external dependency required.

---

## Cross-repo use

`flow` installs globally. The cost DB at `~/.autopilot/costs.duckdb` and hooks in `~/.claude/settings.json` work across all your projects automatically. Project is identified by git remote URL so quota and spend are attributed correctly per repo.

---

## API mode

If you want Claude Code itself to bill via the API (instead of riding your subscription), set:

```sh
AP_FORCE_API_KEY=1   # in ~/.autopilot/.env or shell
```

The Stop hook will route session tokens through the `api` billing path and compute real USD. **Before doing this, set a workspace spend cap in the [Anthropic console](https://console.anthropic.com) — Claude Code sessions can run 2–10M tokens and flow's step gate doesn't cap in-session spend.**

---

## Style

AI Flow injects your personal style into every AI-generated artifact. `flow init` creates `~/.autopilot/style.yaml` with defaults — edit what you care about, set a section to `null` to skip it entirely:

```yaml
commit_message:
  format: "short, imperative, no label prefix"
  max_length: 72

pr_title:
  format: "plain description, sentence case, no prefix brackets"

pr_body: |
  ## What
  {what}

  ## Why
  {why}

  ## Checklist
  - [ ] tests pass

ci_review:
  tone: "direct, no filler, flag real issues only"
  severity_labels: [blocker, suggestion, nit]

agent:
  verbosity: concise
  emoji: false
  confirm_before_destructive: true
```

Per-repo overrides: create `.ap-style.yaml` in the repo root. It deep-merges on top of the global file.

Each call site only receives the sections it needs — no context bloat.
