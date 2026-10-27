---
name: status
description: Report on an agent-loop run (the background sprint or ticket loop) - which tickets are ready to ship, which need the user, which were deferred, and what is still running.
argument-hint: "[run id]"
---

# Where is the run

The CLI is `scripts/agentloop.py` in this plugin, two directories above this file:
`AL="python3 <plugin root>/scripts/agentloop.py"`.

```bash
$AL status [run id]          # the latest run if you give no id; --json for all the data
```

**While the run continues:** tell the user the stage of each ticket and how long it has run. The
last lines of the log show this.

**When `finished` has a value,** report:

- **Ready to ship:** ref, name, commit message, number of fix rounds. Show high-risk tickets first.
- **Needs you:** ref, and the exact question or blocker. The handoff note
  (`handoffs/<id>.md` in the state folder) and the stage logs in the run folder tell what
  happened.
- **Deferred:** ref and the reason.

Refer the user to the ship skill for the ready tickets.

Then run `$AL findings`. If a finding occurs on more than one ticket, tell the user what it is and
where to fix it at the source: a `REVIEW.md` line, an `AGENTS.md` rule, a lint rule or the ticket
template. A fix at the source saves a review round on each future ticket.

**If a run stopped** before it was complete (no `finished` value, and its `pid` does not run),
continue it with `$AL run --resume <run id> --detach`. The run uses the agent results that it has
already.
