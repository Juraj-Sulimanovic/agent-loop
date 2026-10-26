---
name: sprint
description: Plan, implement, review and fix every ready ticket on the board in parallel, one agent chain per ticket in its own worktree, ending with staged changes and a handoff note per ticket. Runs in the background; check it with the agent-loop status skill.
argument-hint: "[ticket ids...] [--plan]"
disable-model-invocation: true
---

# Run the sprint loop

Asking for this skill is the user's go-ahead to start the loop. It does everything up to staged,
reviewed changes; it never commits, pushes or opens a PR. That is the ship skill, after the user
confirms.

The CLI is `scripts/agentloop.py` in this plugin, two directories above this SKILL.md:
`AL="python3 <plugin root>/scripts/agentloop.py"`.

## 1. Pick the tickets

```bash
$AL tickets --all          # every ticket in the Ready column, with its status and reason
```

The ids are the ones the user gave (`812`, `#812` or `ABC-12`); without ids the run takes every
ticket whose status is `ready`. Say in one line which tickets will run and which are skipped
and why (blocked, already in flight), then go on without waiting. No ready tickets: say so and stop.

## 2. Start the run in the background

```bash
$AL run <ids...> --detach            # add --plan when the user asked for --plan
```

It creates or reuses each ticket's worktree, runs the config's setup commands, refreshes the
brief, then starts the loop as a background process and prints `{run, pid, log, tickets}`. Any
ticket it had to skip is printed on stderr; report those. A run takes from minutes to an hour,
so never wait on it in this turn.

Tell the user the run id, which tool runs each stage (`$AL show-config`, `agents`), and that the
status skill or `$AL status` reports progress.

## 3. When it finishes

Report from `$AL status <run>` (or `--json`):
- **Ready to ship:** ref, name, commit message, fix rounds used.
- **Needs you:** ref and the exact question or blocker.
- **Deferred:** ref and the reason (overlaps another ticket's files, over `maxParallel`); they run
  in a later sprint once the overlapping ticket merges.

Then point at the ship skill for the ready ones. With `--plan`, show each planned ticket's
summary and files (`$AL status <run> --json`, `summary.plans`) and offer to build them: once the
user agrees, `$AL run --resume <run> --build --detach` reuses those plans and goes on to
implementation.
