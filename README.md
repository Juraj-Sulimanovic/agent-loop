# agent-loop

A Claude Code plugin that removes the two chores of working with agents: re-explaining the
project to every new session, and carrying one agent's work over to the next.

- **Every session starts briefed.** A SessionStart hook injects the board (the Ready column
  with each ticket's readiness, In Progress, Review…), the sprint goal, the client
  requirements, open and recently merged PRs, local worktrees, and the handoff notes earlier
  agents left behind.
- **One command runs the loop.** `/agent-loop:sprint` takes every ready ticket, plans each in
  its own worktree, keeps tickets that touch the same files apart, then runs implement →
  independent review → fix (up to N rounds) per ticket in parallel, and writes a handoff note.
- **One confirmation ships it.** `/agent-loop:ship` shows the exact commit, push and
  `gh pr create` commands for every ready ticket, asks once, runs them, and moves the tickets
  to the review column. Merging stays with you.

The handoff notes are the shared memory: each agent writes what it did, the decisions others
should follow and the follow-ups it found, and the next brief carries them.

Works with **Trello**, **GitHub Projects** and **Jira** boards, GitHub repositories, and both a
plain clone and a bare repository with sibling worktrees.

## Install

```
/plugin marketplace add Juraj-Sulimanovic/agent-loop
/plugin install agent-loop@agent-loop
```

## Set up a repository

Start Claude Code inside the repository and run:

```
/agent-loop:setup [board URL]
```

The wizard detects the layout, default branch, lint/test commands, per-worktree setup (`.env`,
dependency install), lockfiles and migration folders, PR template and docs; asks for the
tracker, its columns, which checks to run and how many tickets to run at once; writes the
config; and runs `doctor` until everything passes.

Credentials stay in the environment, never in a file:

| Tracker | Needs |
|---|---|
| Trello | `TRELLO_KEY`, `TRELLO_TOKEN` |
| GitHub Projects | `gh auth refresh -s project` |
| Jira | `JIRA_EMAIL`, `JIRA_API_TOKEN` (an Atlassian API token) |

Then fill in the two files the brief quotes in every session: `sprint.md` (goal, must-ship,
parked) and `requirements.md` (what the client asked for and how they judge it).

### Where things live

| Layout | State (config, sprint, requirements, plans, handoffs) | Ticket worktrees |
|---|---|---|
| Plain clone | `<clone>/.git/agent-loop/`: shared by every worktree, never committed, never in `git status` | `<clone>/../<repo>-worktrees/<id>` |
| Bare repo with sibling worktrees | `<root>/.agent-loop/` | `<root>/<id>` |

Both are settings (`worktreePath`, `init --store root|git`).

## Commands

| Command | Does |
|---|---|
| `/agent-loop:setup [url]` | configure or reconfigure the repository |
| `/agent-loop:brief` | refresh the brief mid-session |
| `/agent-loop:sprint [ids] [--plan]` | the parallel loop over ready tickets (or the ids given); `--plan` stops after planning |
| `/agent-loop:ticket <id> [--plan]` | the same loop on one ticket, even a blocked one |
| `/agent-loop:ship [ids]` | commit, push, open PRs and move tickets, after one confirmation |

The skills drive `scripts/agentloop.py`, which also works on its own: `detect`, `init`,
`doctor`, `statuses`, `brief`, `tickets`, `prepare`, `handoffs`, `mark`, `move`
(`python3 scripts/agentloop.py --help`).

## How tickets are chosen

A ticket in the Ready column is **ready** when it has no worktree or open PR yet and its
blockers are resolved (in a done column, closed or archived, or with their branch merged). A
ticket whose single open blocker already has a PR is ready too, **stacked** on that PR's branch.

Blockers come from the tracker where it has them (Jira "is blocked by" links, GitHub issue
dependencies) and otherwise from the description: `#123` refs under a `Blockers` heading
(`tracker.blockerSection`) or after "Blocked by" / "Depends on".

Branches are `{type}/{id}-{title words}`, the type from `typeByLabel` (`bug` → `fix`) or
`defaultType`. Within a run, tickets whose plans touch the same file, or the same `exclusive`
path (lockfiles, manifests, migration folders), are not built together: the later one is
deferred and reported.

## Tests

```bash
python3 -m unittest discover -s tests
```
