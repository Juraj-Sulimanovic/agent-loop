---
name: ticket
description: Run the full plan, implement, review and fix loop on one Trello card in its own worktree. Use when the user wants a single ticket picked up end to end.
argument-hint: "<card id> [--plan]"
disable-model-invocation: true
---

# One ticket, end to end

The sprint loop on a single card. Follow `/agent-loop:sprint`'s steps with the one card id
from `$ARGUMENTS`: `tickets <id>`, `prepare <id>` (with `--force` when the card is blocked), then the `agent-loop:sprint` workflow with
that one ticket. The sprint skill is at `../sprint/SKILL.md` from this skill's base directory;
read it for the exact commands and arguments.

Differences from the sprint:
- A ticket the CLI reports as `blocked` still runs when the user named it, after one line saying
  what it is waiting on; pass the prepared object as is.
- Without `--plan`, still show the plan summary in one line once the workflow returns, so the
  user can see the approach the code took.

End by offering `/agent-loop:ship <id>`.
