---
name: setup
description: Set up or reconfigure agent-loop for the repository the session is in. Detects the repo layout, checks, setup commands and PR template, asks for the tracker (Trello, GitHub Projects or Jira) and its columns, writes the config and runs the doctor.
argument-hint: "[tracker board URL]"
disable-model-invocation: true
---

# Set up agent-loop for this repository

A short wizard: detect what the repository already says, ask only what it cannot know, write
the config, prove it works. The CLI is `scripts/agentloop.py` in this plugin, two directories
above this skill's base directory: `AL="python3 <base>/../../scripts/agentloop.py"`.

Ask with the question tool, one round per step, up to four questions per round. Put the
detected value first and mark it "(Recommended)". Never ask what detection already settled
unless it looked wrong.

## 1. Detect

```bash
$AL detect                         # or: $AL detect --tracker-url "<url from $ARGUMENTS>"
```

It prints `facts` (layout, main checkout, default branch, remote, Make targets, PR template,
whether AGENTS.md forbids agent commits, which tracker credentials are exported, any existing
config) and a suggested `config`. With `facts.existingConfig` set this is a reconfigure: read
that file and treat its values as the defaults instead of the suggestion.

Tell the user in three or four lines what you found: repo, layout, where the state will live
(`facts.store`; inside `.git/` for a clone, so it is never committed), checks and setup.

## 2. Tracker

If no board URL was given, ask which tracker the team uses: Trello, GitHub Projects or Jira.
Then ask for the board's URL and run `$AL detect --tracker-url "<url>"` again, which fills the
tracker block:

| Tracker | URL looks like | Credentials (environment only, never a file) |
|---|---|---|
| Trello | `https://trello.com/b/<id>/<name>` | `TRELLO_KEY`, `TRELLO_TOKEN` |
| GitHub Projects | `https://github.com/orgs/<org>/projects/<n>` or `/users/<user>/projects/<n>` | `gh auth refresh -s project` |
| Jira | `https://<site>.atlassian.net/jira/software/projects/<KEY>/boards/<n>` | `JIRA_EMAIL`, `JIRA_API_TOKEN` (an Atlassian API token) |

Jira also takes a `jql` for which issues are in play; the default is the project's open
sprints. Ask only if the team runs Kanban (no sprints); then suggest
`project = KEY AND statusCategory != Done ORDER BY Rank`. A GitHub project whose column field
is not called `Status` needs `statusField`.

When the credentials are missing, give the user the one command to run with `!` (for example
`! export JIRA_EMAIL=... JIRA_API_TOKEN=...`) and wait. A shell export only reaches this
session through `!`; also tell them to add it to the shell they start `claude` from.

## 3. Columns

Write the config so far to a scratch file, then read the board's real column names:

```bash
$AL --config <scratch file> statuses
```

Ask, offering those names as the options:
- **Ready:** the column agents pick tickets from (often Sprint Backlog, Todo, Selected for Development).
- **Doing** and **Review:** where tickets move when work starts and when the PR opens.
- **Done** (multi-select): the columns that mean the work is merged, so a blocker there is resolved.

`show` is the columns the brief lists, in board order from Ready through review (skip
backlog and done columns).

## 4. Loop settings

One round:
- **Checks** (multi-select): the detected lint, typecheck and test commands. Warn when a test
  command needs a shared database or service: parallel agents will collide on it, so prefer a
  unit-only target.
- **Setup** (multi-select): the detected per-worktree commands (copy `.env`, install deps).
- **Parallel tickets:** 3, 5 or 10 (each ticket costs four to six agents).
- **Pull requests:** ready for review, or draft.

Keep the detected `exclusive` paths (lockfiles, manifests, migration folders: two tickets that
both touch one are never built together) and `contextFiles`. Leave `pr.ticketLine` as
`#### Ticket [{ref}]({url})` unless the PR template shows the team writes the ticket another
way; then match it (`{ref}`, `{id}`, `{url}` and `{name}` are filled in).

## 5. Write and prove

```bash
$AL --config <scratch file> init          # installs it where facts.store says; keeps sprint.md and requirements.md
$AL doctor
```

Fix every FAIL line with the user (wrong column name, missing make target, missing scope) and
rerun the doctor until it passes. Then run `$AL brief` and show its first lines.

Finish by telling the user:
- the two files to fill in now, `<dir>/sprint.md` and `<dir>/requirements.md` (offer to draft
  `requirements.md` from the repository's docs if they want);
- new sessions anywhere in this repository now start with the brief;
- `/agent-loop:ticket <id> --plan` is the safe first run, then `/agent-loop:sprint`.
