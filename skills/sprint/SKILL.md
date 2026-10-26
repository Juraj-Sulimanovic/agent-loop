---
name: sprint
description: Plan, implement, review and fix every ready Sprint Backlog ticket in parallel, one agent chain per ticket in its own worktree, ending with staged changes and a handoff note per ticket.
argument-hint: "[card ids...] [--plan]"
disable-model-invocation: true
---

# Run the sprint loop

This command is the user's authorization to run the `agent-loop:sprint` workflow. It does
everything up to staged, reviewed changes; it never commits, pushes or opens a PR. That is
`/agent-loop:ship`, after the user confirms.

The CLI is `scripts/agentloop.py` in this plugin, two directories above this skill's base
directory: `AL="python3 <base>/../../scripts/agentloop.py"`.

## 1. Pick the tickets

```bash
$AL brief --json          # refreshes .agent-loop/brief.md; note its path
$AL tickets $IDS          # the ids given, else every ready Sprint Backlog card
```

`$IDS` are the card ids in `$ARGUMENTS`, if any. Without ids you get the cards whose
blockers are resolved and which have no worktree or open PR yet. A ticket stacked on an open
blocker PR is ready, with that PR's branch as its base. Say in one line which tickets will
run and which were skipped and why (`$AL tickets --all` shows the skipped ones), then go on
without waiting. Zero tickets: say so and stop.

## 2. Prepare the worktrees

```bash
$AL prepare <id> <id> ...
```

It creates each worktree at `<root>/<id>` on `type/<id>-slug` from the ticket's base, or reuses
the existing one, then runs the config's `setup` commands. Drop any ticket that comes back with
`error` or a failed setup step and say why.

## 3. Run the workflow

Read the project config at `<root>/.agent-loop/config.json` and call:

```
Workflow({
  name: "agent-loop:sprint",
  args: {
    project: {root, briefPath, checks, exclusive, maxParallel, maxFixRounds},   // from the config and step 1
    tickets: <the prepared ticket objects from step 2>,
    stamp: "<current ISO date-time>",
    stopAfter: "plan"            // only with --plan
  }
})
```

With `--plan` the workflow stops after planning. Show each plan's summary and files, then
offer to run again without `--plan` using `resumeFromRunId` so the plans are reused.

## 4. Report

When it returns, report three short lists:
- **Ready to ship:** id, name, commit message, fix rounds used.
- **Needs you:** id and the exact question or blocker.
- **Deferred:** id and the reason (overlaps, maxParallel). The user reruns `/agent-loop:sprint`
  for these after the overlapping tickets merge.

Then point at `/agent-loop:ship` for the ready ones. Handoffs are in `<root>/.agent-loop/handoffs/`.
