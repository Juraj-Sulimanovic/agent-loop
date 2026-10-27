---
name: ship
description: Commit, push and open the pull request for tickets the sprint loop left ready, after the user confirms the exact commands once, then move the tickets to the review column.
argument-hint: "[ticket ids...]"
disable-model-invocation: true
---

# Ship the ready tickets

The loop stages its changes. It does not commit them. Many repositories let an agent run
`git commit`, `git push` and `gh pr create` only after the user approves the exact command. This
skill collects these commands for all ready tickets and asks the user one time.

The CLI is `scripts/agentloop.py` in this plugin, two directories above this file:
`AL="python3 <plugin root>/scripts/agentloop.py"`.

## 1. Collect

```bash
$AL show-config                # repo, pr.template, pr.ticketLine, pr.draft, dir
$AL handoffs --status ready
```

If the user gave ticket ids, keep only those tickets. For each handoff, in its `worktree`:

```bash
git -C <worktree> status --short          # staged changes must exist; unstaged changes must not
git -C <worktree> diff --cached --stat
```

An unstaged change means that a person changed the worktree after the loop. Show the change.
Do not ship that ticket until the user tells you what to do.

## 2. Prepare

Write the PR body of each ticket to `<dir>/plans/<id>.pr.md`:

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
- Show the full diff of those files, not only the stat.

For each ticket, show the diff stat, the **Open review findings**, the **Pre-existing issues**
(offer to make tickets for them) and the exact commands:

```bash
git -C <worktree> commit -m "<handoff commit message>"
git -C <worktree> push -u origin <branch>
gh pr create -R <repo> --head <branch> --base <base> --title "<PR title>" --body-file <body file>   # add --draft when pr.draft is true
```

Ask the user one time which tickets to ship. List all ready tickets. Use a multiple-choice
question when you have a question tool. To ship a ticket is to run exactly its three commands.

## 4. Ship

For each selected ticket, run its three commands in sequence. If a command fails, stop that
ticket and report the failure. When the PR is open, run:

```bash
$AL mark <id> shipped --pr <pr url>
$AL move <id> review
```

Report the PR links. The user merges.
