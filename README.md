# `flow`

Personal AI dev harness built on Claude Code. Parallel agents, terminal control room, automatic pipeline from task to PR.

![flow control room](docs/screenshot.png)

Three sessions running simultaneously: a planner working through an architecture question, an executor that just shipped rate limiting, and a reviewer that auto-spawned after the ship.

---

## Install

```sh
pip install -e .
flow init
```

`flow init` writes hooks into `~/.claude/settings.json` and creates `~/.autopilot/.env`:

```sh
ANTHROPIC_API_KEY=sk-ant-...   # for ship, check, ci-review
AP_PLAN=pro                    # pro | max5 | max20 | api_only
```

---

## Usage

```sh
flow
```

Type a task, press Enter. Prefix to change behavior:

| Prefix | Model | Behavior |
|---|---|---|
| _(none)_ | sonnet | Full pipeline: plan → execute → verify → ship, reviewer auto-spawned |
| `plan: <question>` | opus | Interactive planner — stays alive, responds to your follow-ups |
| `review: <branch>` | haiku | One-shot diff review |

### Commands

| | |
|---|---|
| `/view N` | Drill into session N — full output + live input |
| `/stop [N]` | Stop session N or all running |
| `/prompt N <msg>` | Inject a message into session N |
| `/model opus\|sonnet\|haiku` | Override model for new sessions |
| `/resume [run_id]` | Reattach to an interrupted run |
| `/quit` | Exit, clean up completed worktrees |

Planners show `?` in the pane title when waiting for input — `/view N` to reply.

---

## CI / scripting

```sh
flow doctor [--fix]              # check hook health
flow stats                       # cost by project
flow ship                        # verify → commit → PR
flow check                       # AI review of local diff
flow ci-review --pr 42           # for GitHub Actions
```

---

## Prerequisites

- [Claude Code](https://claude.ai/code) installed and authenticated
- Python 3.9+
- [`gh`](https://cli.github.com) (for `flow ship` and CI review)
- A GitHub repo with `origin` set
- Anthropic API key

<!-- dummy: 2026-05-14 -->
