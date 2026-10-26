---
name: ship
description: Commit, push and open the pull request for tickets the sprint loop left ready, after the user confirms the exact commands once, then move the tickets to the review column.
argument-hint: "[ticket ids...]"
disable-model-invocation: true
---

# Ship the ready tickets

The loop leaves changes staged, never committed: many repositories let agents only stage and
propose, with `git commit`, `git push` and `gh pr create` run after the user confirmed the exact
command. This skill gathers those commands for every ready ticket and asks once.

The CLI is `scripts/agentloop.py` in this plugin, two directories above this SKILL.md:
`AL="python3 <plugin root>/scripts/agentloop.py"`.

## 1. Gather

```bash
$AL show-config                # repo, pr.template, pr.ticketLine, pr.draft, dir
$AL handoffs --status ready
```

Keep only the ids the user gave, if any. For each handoff, in its `worktree`:

```bash
git -C <worktree> status --short          # something must be staged, nothing unstaged
git -C <worktree> diff --cached --stat
```

An unstaged change means someone touched the worktree after the loop; show it and leave that
ticket out until the user says how to handle it.

## 2. Propose

Build each PR body from the worktree's copy of `pr.template` when the config names one: its
first line becomes `pr.ticketLine` with `{ref}`, `{id}`, `{url}` and `{name}` filled from the
handoff (`card` is the URL), and its changes section takes the handoff's **PR changes**
bullets. Follow the repository's own PR conventions where it has them (a `/pr` skill, the
AGENTS.md git section). Without a template the body is the ticket line plus the bullets.
Write each body to `<dir>/plans/<id>.pr.md`.

For each ticket show the diff stat, the handoff's **Open review findings**, and the exact
commands:

```bash
git -C <worktree> commit -m "<handoff commit message>"
git -C <worktree> push -u origin <branch>
gh pr create -R <repo> --head <branch> --base <base> --title "<PR title>" --body-file <body file>   # add --draft when pr.draft is true
```

Ask the user once which tickets to ship (all ready ones listed; a multi-select question when you
have a question tool). Shipping a ticket means running exactly its three commands.

## 3. Ship

For each chosen ticket, run its three commands in order; stop that ticket at the first failure
and report it. After the PR opens:

```bash
$AL mark <id> shipped --pr <pr url>
$AL move <id> review
```

Report the PR links. Merging stays with the user.
