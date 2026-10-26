# agent-loop

A Claude Code plugin that removes the two chores of working with agents: re-explaining the
project to every new session, and carrying one agent's work over to the next.

- **Every session starts briefed.** A SessionStart hook injects the board (Sprint Backlog
  with each card's readiness, In Progress, Code Review…), the sprint goal, the client
  requirements, open and recently merged PRs, local worktrees, and the handoff notes earlier
  agents left behind.
- **One command runs the loop.** `/agent-loop:sprint` takes every ready ticket, plans each in
  its own worktree, keeps tickets that touch the same files apart, then runs implement →
  independent review → fix (up to N rounds) per ticket in parallel, and writes a handoff note.
- **One confirmation ships it.** `/agent-loop:ship` shows the exact commit, push and
  `gh pr create` commands for every ready ticket, asks once, runs them, and moves the cards to
  Code Review. Merging stays with you.

The handoff notes are the shared memory: each agent writes what it did, the decisions others
should follow and the follow-ups it found to `.agent-loop/handoffs/<card>.md`, and the next
brief carries them.

## Install

```
/plugin marketplace add ~/repos/agent-loop
/plugin install agent-loop@agent-loop
```

## Set up a project

```bash
python3 ~/repos/agent-loop/scripts/agentloop.py init --root <project root>
```

Then edit `<root>/.agent-loop/`:

| File | What it holds |
|---|---|
| `config.json` | repo, Trello board and list names, worktree setup commands, checks, exclusive areas |
| `sprint.md` | the sprint goal, must-ship cards, what is parked |
| `requirements.md` | what the client asked for and how they judge it |

`.agent-loop/` lives in the project root, outside any worktree, so it is never committed and
every worktree under the root shares it. Worktrees are created at `<root>/<card id>`.

Export Trello credentials in the shell you start `claude` from (never in a file):

```bash
export TRELLO_KEY=...  TRELLO_TOKEN=...
```

Without them the brief still carries PRs, worktrees, handoffs and the sprint files, and says
the board is unavailable.

## Commands

| Command | Does |
|---|---|
| `/agent-loop:brief` | refresh the brief mid-session |
| `/agent-loop:sprint [ids] [--plan]` | the parallel loop over ready tickets (or the ids given); `--plan` stops after planning |
| `/agent-loop:ticket <id> [--plan]` | the same loop on one card, even a blocked one |
| `/agent-loop:ship [ids]` | commit, push, open PRs and move cards, after one confirmation |

## How tickets are chosen

A Sprint Backlog card is **ready** when it has no worktree or open PR yet and every `#id` in
its `### Blockers` section is resolved (card in a `done` list, archived, or its branch merged).
A card whose single open blocker already has a PR is ready too, **stacked** on that PR's branch.
Branches are `{type}/{card url slug}`, the type coming from `typeByLabel` or `defaultType`.

Within a run, tickets whose plans touch the same file, or the same `exclusive` prefix (for
example `alembic/versions/`), are not built together: the later one is deferred and reported.

## Tests

```bash
python3 -m unittest discover -s tests
```
