---
name: sprint
description: Plan, implement, review and fix every ready ticket on the board in parallel, one agent chain per ticket in its own worktree, ending with staged changes and a handoff note per ticket. Runs in the background; check it with the agent-loop status skill.
argument-hint: "[ticket ids...] [--plan]"
disable-model-invocation: true
---

# Run the sprint loop

When the user asks for this skill, start the loop. The loop stops at staged, reviewed changes. It
does not commit, push or open a PR. The ship skill does that, after the user approves.

The CLI is `scripts/agentloop.py` in this plugin, two directories above this file:
`AL="python3 <plugin root>/scripts/agentloop.py"`.

## 1. Select the tickets

```bash
$AL tickets --all          # each ticket in the Ready column, with its status and the reason
```

Use the ids that the user gave (`812`, `#812` or `ABC-12`). Without ids, the run takes each
ticket with the status `ready`. In one line, tell the user which tickets will run, which will
not, and why (blocked, already in progress). Then continue. Do not wait. If no ticket is ready,
say so and stop.

## 2. Start the run in the background

```bash
$AL run <ids...> --detach            # add --plan if the user asked for --plan
```

The command prepares a worktree for each ticket, runs the setup commands and refreshes the brief.
Then it starts the loop in the background and prints `{run, pid, log, tickets}`. It writes
skipped tickets to stderr. Report them.

A run can take from a few minutes to one hour. Do not wait for it in this turn. Tell the user:

- the run id,
- the tool for each stage (`agents` in `$AL show-config`),
- that the status skill or `$AL status` shows the progress.

## 3. When the run is complete

Report from `$AL status <run>` (or `--json`):

- **Ready to ship:** ref, name, commit message, number of fix rounds.
- **Needs you:** ref, and the exact question or blocker.
- **Deferred:** ref and the reason (overlap with the files of a different ticket, or over
  `maxParallel`). These tickets run in a later sprint.

Then refer the user to the ship skill for the ready tickets.

After a `--plan` run, show the summary and the files of each plan (`summary.plans` in
`$AL status <run> --json`). Ask if the user wants to continue. If yes, run
`$AL run --resume <run> --build --detach`. It uses the same plans and starts the implementation.
