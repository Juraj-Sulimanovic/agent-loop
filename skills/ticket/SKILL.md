---
name: ticket
description: Run the full plan, implement, review and fix loop on one ticket in its own worktree. Use when the user wants a single ticket picked up end to end.
argument-hint: "<ticket id> [--plan]"
disable-model-invocation: true
---

# One ticket, end to end

The sprint loop on a single ticket: follow the sprint skill (`../sprint/SKILL.md`, next to this
skill's folder) with the one id the user gave.

Differences from the sprint:
- A ticket the CLI reports as `blocked` still runs when the user named it, after one line saying
  what it is waiting on: add `--force` to `$AL run`.
- Without `--plan`, also show the plan's summary in one line when the run finishes, so the user
  sees the approach the code took (the plan is in the agent-loop folder under `plans/<id>.md`).

End by offering the ship skill for that ticket.
