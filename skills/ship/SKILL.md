---
name: ship
description: Commit, push and open the pull request for tickets the sprint loop left ready, after the user confirms the exact commands once, then move their cards to Code Review.
argument-hint: "[card ids...]"
disable-model-invocation: true
---

# Ship the ready tickets

The repository rule is that agents stage and propose, and `git commit`, `git push` and
`gh pr create` run only after the user confirmed the exact command. This skill gathers those
commands for every ready ticket and asks once.

The CLI is `scripts/agentloop.py` in this plugin, two directories above this skill's base
directory: `AL="python3 <base>/../../scripts/agentloop.py"`.

## 1. Gather

```bash
$AL handoffs --status ready
```

Keep the ids in `$ARGUMENTS` if any were given. For each handoff, in its `worktree`:

```bash
git -C <worktree> status --short          # something must be staged, nothing unstaged
git -C <worktree> diff --cached --stat
```

An unstaged change means someone touched the worktree after the loop; show it and leave that
ticket out until the user says how to handle it.

## 2. Propose

The PR body is `.github/PULL_REQUEST_TEMPLATE.md` from the worktree, filled per the
repository's own `/pr` conventions: the ticket line `#### Ticket [<id>](<card url>)`, then
`## Changes` with the handoff's **PR changes** bullets. Write each body to
`<root>/.agent-loop/plans/<id>.pr.md`.

For each ticket show the diff stat, the handoff's **Open review findings**, and the exact
commands:

```bash
git -C <worktree> commit -m "<handoff commit message>"
git -C <worktree> push -u origin <branch>
gh pr create -R <repo> --head <branch> --base <base> --title "<PR title>" --body-file <body file>   # add --draft when config prDraft is true
```

Ask with one multi-select question which tickets to ship (all ready ones listed, recommended
first). Shipping a ticket means running exactly its three commands.

## 3. Ship

For each chosen ticket, run its three commands in order; stop that ticket at the first failure
and report it. After the PR opens:

```bash
$AL mark <id> shipped --pr <pr url>
$AL move <id> review
```

Report the PR links. Merging stays with the user.
