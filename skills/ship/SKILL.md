---
name: ship
description: Put the tickets the sprint loop left ready into pull request stacks, then push and open the pull requests after the user confirms the exact commands once, and move the tickets to the review column.
argument-hint: "[ticket ids...]"
disable-model-invocation: true
---

# Ship the ready tickets

The loop leaves each ready ticket staged, or committed locally on its branch when
`commitAtHandoff` is on. Nothing is pushed. This skill builds the pull request stacks locally,
then shows the user the exact push and PR commands and asks one time.

The CLI is `scripts/agentloop.py` in this plugin, two directories above this file:
`AL="python3 <plugin root>/scripts/agentloop.py"`.

`pr.stack` in the configuration sets the shape:

- `sprint`: one stack for the whole sprint. Tickets that build on each other keep their order;
  independent tickets go on top, in the order they finished. When a ticket builds on a branch of
  the user's own open stack, the sprint's stack goes on top of that stack.
- `chains`: only tickets that build on another ticket are stacked. The rest target the trunk.

## 1. Build the stacks

```bash
$AL show-config                # repo, pr.template, pr.ticketLine, pr.draft, dir
$AL stack plan [ids...]        # the ids the user gave, else every ready ticket; changes nothing
```

If `squash` in its output lists tickets, warn the user before anything changes, for each one:
"<ref> has <commits> commits in its worktree. To go into the PR stack, agent-loop squashes them
into one commit named after the handoff commit message (the other commit subjects go in its
body)." Ask once whether to continue. If yes, add `--squash` below. If no, leave those tickets out
of the ids.

```bash
$AL stack build [ids...] [--squash]
```

`stack build` changes only local branches. It commits each staged ticket with its handoff commit
message, and in `sprint` mode it rebases each ticket, in its own worktree, onto the ticket below
it. Each ticket is one commit, so its rebase stops at most once. A conflict goes to one resolve
agent. Then the checks run, and when an agent changed code, one read-only combine review checks
that neither ticket's behaviour was dropped or changed. This can take a few minutes. It prints:

- `warnings`: report them (for example, `gh stack` is not installed, so the PRs are chained
  without a stack on GitHub).
- `stacks`, each with `anchor` (`trunk`, `own`: on top of the user's open stack, `foreign`:
  on someone else's branch, which is only the base), its `tickets` in stack order, and
  `commands`.
- each ticket's `status`:
  - `built`: ready to publish. `parent` is its PR base. `resolved` says what an agent changed
    after the rebase, and `stat` is its diff. `findings` on a `built` ticket are advisory.
  - `stopped`: it could not be finished. `reason` says why and what to do. `conflicts` lists the
    files the resolve agent could not resolve.
  - `waiting`: above a stopped ticket. It is published when that ticket is fixed.

When a ticket stopped, give the user its `reason`, which names the commands, and say what to do
in its `worktree`:

- conflicts the agent could not resolve: the rebase was aborted, and the branch is as it was.
  Rebase it onto its `parent` by hand, resolve the conflicts and finish the rebase.
- failing checks: the branch is left rebased, with the conflicts resolved. Fix the problem and
  `git add` the fix; ship puts it into the ticket's one commit.

If something went wrong, the user can go back: `git -C <worktree> reflog` lists the earlier
states of the branch, and `git -C <worktree> reset --hard <commit>` returns to one. The `reason`
names the commit from before this ship.

Then run the ship skill again. It starts from the beginning: the tickets that already sit on the
one below do not move, and the fixed ticket gets its checks and, if an agent changed it, its
combine review again. Nothing in the stack is published until the stopped ticket is fixed, and
`commands` is empty for it.

### Offer the tickets below a stop

Tell the user, in plain text and with no commands: "Nothing is published while a ticket is
stopped. If you want the tickets below it in PRs now, say so and I'll build and publish only
those." If the user says yes, run `$AL stack build` for the ids of the tickets below the stop,
in this same chat, and continue with Prepare, Ask and Ship for those. Never print or run push or
`gh` commands for a stopped stack, and never offer to publish without the user saying yes.

## 2. Prepare

For each built ticket, read its handoff note (`path`) and write its PR body to
`<dir>/plans/<id>.pr.md`, the file its `gh pr create` command reads:

1. Start from the worktree copy of `pr.template`, if the configuration names one.
2. Replace the first line with `pr.ticketLine`. Fill `{ref}`, `{id}`, `{url}` and `{name}` from the
   handoff (`card` is the URL).
3. Put the **PR changes** bullets of the handoff in the changes section.
4. If the template has Testing, Risk or revert sections, fill them from **Testing**, **Risk** and
   **How to revert**. If it does not, add these three below the changes for a high-risk ticket
   only.
5. Follow the PR conventions of the repository, for example a `/pr` skill or the git section of
   `AGENTS.md`.

Without a template, the body is the ticket line and the bullets.

## 3. Ask

Show the high-risk tickets first (`risk: high` in the handoff). For each one:

- Say that it needs a careful human review.
- Name the sensitive paths that it changes (`sensitive:`).
- Show the full diff of those files (`git -C <worktree> diff <parent> HEAD -- <paths>`).

Then show each stack, bottom to top: each ticket's ref, name, PR base and diff stat, what an agent
resolved after the rebase (show that diff too), and its combine `findings` right below that diff,
as plain text (file:line, what changed or was lost, evidence). These findings are advisory: the
user may fix them in the worktree and run ship again, or publish as is. Also show its **Open
review findings** and **Pre-existing issues** (offer to make tickets for them). Show the stack's
`commands` exactly.

Ask the user one time: publish these stacks? When the user wants to leave tickets out, run
`$AL stack build <the other ids>` again and ask again: the tickets above a left-out one move.

## 4. Ship

Run each stack's `commands` in order. If a command fails, stop that stack and report the
failure. When a ticket's PR is open, run:

```bash
$AL mark <id> shipped --pr <pr url>
$AL move <id> review
```

Report the PR links. The user reviews and merges. Tell the user, in one line: when a branch of the
stack changes, keep the stack in one straight line with **Rebase stack** on GitHub, or remove the
ticket worktrees and run `gh stack rebase` and `gh stack sync` from the main checkout. Those
commands check the branches out, and a branch that a worktree has checked out cannot be checked out
anywhere else.
