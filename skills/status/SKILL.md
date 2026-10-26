---
name: status
description: Report on an agent-loop run (the background sprint or ticket loop) - which tickets are ready to ship, which need the user, which were deferred, and what is still running.
argument-hint: "[run id]"
---

# Where is the run

The CLI is `scripts/agentloop.py` in this plugin, two directories above this SKILL.md:
`AL="python3 <plugin root>/scripts/agentloop.py"`.

```bash
$AL status [run id]          # the latest run when no id is given; --json for everything
```

While it runs, say which stage each ticket is in and how long it has been going (the last lines of
the log the output names). When `finished` is set, report:
- **Ready to ship:** ref, name, commit message and the fix rounds it took.
- **Needs you:** ref and the exact question or blocker. The handoff note in the agent-loop folder
  (`handoffs/<id>.md`) and the stage logs in the run folder say what happened.
- **Deferred:** ref and why.

Point at the ship skill for the ready ones. A run that died mid-way (no `finished`, and its `pid`
no longer running) resumes with `$AL run --resume <run id> --detach`: finished agent calls are
reused, the rest run again.
