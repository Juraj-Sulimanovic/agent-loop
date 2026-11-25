---
name: sprint
description: Plan, implement, review and fix every ready ticket on the board in parallel, one agent chain per ticket in its own worktree, ending with a reviewed change (committed locally by default) and a handoff note per ticket. Runs in the background; check it with the agent-loop status skill.
argument-hint: "[ticket ids...] [--plan]"
disable-model-invocation: true
---

# Run the sprint loop

When the user asks for this skill, start the loop. The loop stops at reviewed changes. With
`commitAtHandoff` on (the default), it commits each ready ticket locally on its branch; otherwise
the change stays staged. It never pushes or opens a PR. The ship skill does that, after the user
approves.

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

## 2. Start the run

**In Claude Code (you have the Workflow tool),** run the loop as a Workflow. Claude Code then
shows its own progress view: the phases, each agent, and keys to look inside and to stop or pause.

1. Prepare the tickets and the run folder:

   ```bash
   $AL wf start <ids...> > <scratch>/wf-args.json        # add --plan if the user asked for --plan
   ```

   It writes skipped tickets to stderr. Report them.
2. Read `<scratch>/wf-args.json` and start the workflow with its content as the arguments (the
   JSON object itself, not a string):

   ```
   Workflow({ name: "agent-loop:sprint", args: <the JSON object> })
   ```

   Asking for this skill is the user's approval to run it.

   The tool returns at once with a `Run ID` and a `Transcript dir`. Record them, so the
   agent-loop panel can show what each agent does while the run continues:

   ```bash
   $AL wf attach <run> --wf <Run ID> --transcripts <Transcript dir>
   ```
3. When the workflow returns, write its result to `<runDir>/summary.json` (`runDir` is in the
   arguments) and run `$AL wf finish <run>`. The status skill, `findings` and the panel then see
   the run.

**Without the Workflow tool (Codex, Cursor),** run the loop in the background:

```bash
$AL run <ids...> --detach            # add --plan if the user asked for --plan
```

The command prepares a worktree for each ticket, runs the setup commands and refreshes the brief.
Then it starts the loop in the background and prints `{run, pid, log, tickets}`. It writes
skipped tickets to stderr. Report them. A run can take from a few minutes to one hour. Do not wait
for it in this turn. Tell the user the run id and that the status skill shows the progress.

## 3. When the run is complete

Report from `$AL status <run>` (or `--json`):

- **Ready to ship:** ref, name, commit message, number of fix rounds.
- **Needs you:** ref, and the exact question or blocker.
- **Deferred:** ref and the reason. These tickets run in a later sprint. With `commitAtHandoff`
  on (the default), a ticket that overlaps the files of a different ticket, waits for one, or is
  over `maxParallel` is not deferred: it waits in the same run and builds on the finished
  ticket's commit. It is deferred only when the ticket it waits on did not finish ready. With
  `commitAtHandoff` off, an overlap or a ticket over `maxParallel` is deferred.

Then refer the user to the ship skill for the ready tickets.

After a `--plan` run, show the summary and the files of each plan (`summary.plans`). Ask if the
user wants to continue. If yes:

- **Workflow:** start the workflow again with the same arguments, `planOnly` set to `false`, and
  `resumeFromRunId` set to the first workflow's run id. It reuses the plans and starts the
  implementation.
- **Runner:** run `$AL run --resume <run> --build --detach`.
