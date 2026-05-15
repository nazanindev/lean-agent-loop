# AI Flow E2E Regression Checklist

Use this checklist after lifecycle or shipping-flow changes.

## 1) Plan -> Execute -> Verify happy path

1. Start REPL with `flow`.
2. Enter a goal that produces a multi-step plan.
3. Run `/approve` and confirm phase becomes `execute`.
4. Complete plan steps using `/next` or `STEP_DONE: <id>` markers.
5. Confirm run auto-advances to `verify` after all steps are done.

## 2) Verify-phase checker prompt and opt-in behavior

1. From execute, finish all plan steps and enter `verify`.
2. Confirm prompt appears: `Run flow check ... before /ship? [y/N]`.
3. Answer `n` and confirm no checker output is printed.
4. Run `/check` manually and confirm a checker summary prints.

## 3) Blocker acknowledgement gate before shipping

1. Trigger checker output with at least one blocker finding.
2. Run `/ship` and confirm shipping is blocked with `/ack-check` instruction.
3. Run `/ack-check`.
4. Run `/ship` again and confirm the checker gate no longer blocks shipping.

## 4) Sprint contract context injection

1. Ensure an active feature exists (`flow features pick`).
2. Start a new run and inspect the initial briefing.
3. Confirm briefing includes:
   - `Sprint contract (this run)`
   - Scope = active feature behavior
   - Verification command = active feature verification command
   - Out-of-scope guardrail line

## 5) CLI checker JSON output

1. Run `flow check --json`.
2. Confirm output is valid JSON with:
   - `overall`
   - `dimensions.correctness|architecture|test_coverage`
   - `findings`
   - `blocker_count|warning_count|note_count`

<!-- dummy change for pipeline smoke test -->
