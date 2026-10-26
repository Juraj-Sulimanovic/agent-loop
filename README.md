# agent-loop

A plugin for **Claude Code, Codex and Cursor** that removes the two chores of working with
agents: re-explaining the project to every new session, and carrying one agent's work over to
the next.

- **Every session starts briefed.** A session-start hook injects the board (the Ready column
  with each ticket's readiness, In Progress, Review…), the sprint goal, the decisions log, the
  project knowledge index, open and recently merged PRs, local worktrees, and the handoff notes
  earlier agents left behind.
- **One command runs the loop.** The sprint skill takes every ready ticket, plans each in its own
  worktree, keeps tickets that touch the same files apart, then runs implement → independent
  review → fix (up to N rounds) per ticket in parallel, and writes a handoff note. It runs in the
  background; the status skill reports on it.
- **Any tool, per stage.** Each stage runs headless in Claude Code, Codex or Cursor, as the config
  says: for example Codex implements and Claude reviews, so the review is independent.
- **One confirmation ships it.** The ship skill shows the exact commit, push and `gh pr create`
  commands for every ready ticket, asks once, runs them, and moves the tickets to the review
  column. Merging stays with you.

Works with **Trello**, **GitHub Projects**, **Jira** or a local **tickets file**, GitHub
repositories, and both a plain clone and a bare repository with sibling worktrees.

## Install

The repository is private: your git credentials must be able to read it.

**Claude Code**

```
/plugin marketplace add Juraj-Sulimanovic/agent-loop
/plugin install agent-loop@agent-loop
```

**Codex**

```bash
codex plugin marketplace add Juraj-Sulimanovic/agent-loop
codex plugin add agent-loop
```

Then approve the plugin's session hook once in `/hooks`.

**Cursor**

```bash
git clone https://github.com/Juraj-Sulimanovic/agent-loop ~/.cursor/plugins/local/agent-loop
```

Then reload Cursor (**Developer: Reload Window**) and check the plugin under **Customize**. It
must be a real folder there; a symlink to elsewhere does not load. On Enterprise plans an admin
first enables **Allow Local Plugin Imports**. A team can instead import the repository as a team
marketplace (**Dashboard → Plugins & MCPs → Team Marketplaces → Import from Repo**; not yet tried
with this plugin). The session hook does not run in Cursor's cloud agents.

The loop drives the tools headless, so the ones you use need to be installed and logged in:
`claude`, `codex` (`codex login`) and `cursor-agent` (`cursor-agent login`). `doctor` checks
them.

## Set up a repository

Start a session inside the repository and ask for the **setup** skill (`/agent-loop:setup` in
Claude Code), optionally with the board's URL.

The wizard detects the layout, default branch, lint/test commands, per-worktree setup (`.env`,
dependency install), lockfiles and migration folders, PR template and docs; asks for the
tracker, its columns, which checks to run, how many tickets to run at once, and which tool runs
each stage; writes the config; and runs `doctor` until everything passes.

Credentials stay in the environment, never in a file:

| Tracker | Needs |
|---|---|
| Trello | `TRELLO_KEY`, `TRELLO_TOKEN` |
| GitHub Projects | `gh auth refresh -s project` |
| Jira | `JIRA_EMAIL`, `JIRA_API_TOKEN` (an Atlassian API token) |
| File | nothing: `tickets.json` in the agent-loop folder, a list of `{id, name, status, desc, labels, blockers}` |

Then fill in `sprint.md` (goal, must-ship, parked), which the brief quotes in every session. The
client's requirements live where your team already keeps them, in the tickets and the
repository's docs; an optional `requirements.md` in the agent-loop folder is quoted too, for
projects that have no other home for them.

### Where things live

| Layout | State (config, sprint, knowledge, decisions, plans, handoffs, runs) | Ticket worktrees |
|---|---|---|
| Plain clone | `<clone>/.git/agent-loop/`: shared by every worktree, never committed, never in `git status` | `<clone>/../<repo>-worktrees/<id>` |
| Bare repo with sibling worktrees | `<root>/.agent-loop/` | `<root>/<id>` |

Both are settings (`worktreePath`, `init --store root|git`).

## Skills

| Skill | Does |
|---|---|
| `setup [url]` | configure or reconfigure the repository |
| `brief` | refresh the brief mid-session |
| `sprint [ids] [--plan]` | start the loop over ready tickets (or the ids given) in the background; `--plan` stops after planning |
| `ticket <id> [--plan]` | the same loop on one ticket, even a blocked one |
| `status [run]` | where a run is: ready to ship, needs you, deferred |
| `ingest <path>` | add transcripts, documents or test files to the project knowledge, and distil what they settle into the repo docs and tickets |
| `ship [ids]` | commit, push, open PRs and move tickets, after one confirmation |

In Claude Code they are `/agent-loop:<skill>`; Codex and Cursor pick them up by name or
description.

The skills drive `scripts/agentloop.py`, which also works on its own: `detect`, `init`,
`doctor`, `statuses`, `brief`, `tickets`, `prepare`, `run`, `status`, `ingest`, `knowledge`, `annotate`, `handoffs`, `mark`, `move`
(`python3 scripts/agentloop.py --help`).

## Project knowledge and the decisions log

Client material lives next to the project's agent-loop state, never in the repository:

```
knowledge/INDEX.md                 one line per item; the brief lists it
knowledge/items/<id>/original/     the files as given
knowledge/items/<id>/text/         readable versions of .docx, .xlsx, .csv, .pdf
decisions.md                       what shipped tickets decided, newest first
```

The **ingest** skill adds a transcript, document or folder of test files: it copies them (skipping
files already there), writes text versions, describes each item, and proposes the requirement
changes the material settles, with sources, for you to approve: edits to the repository's docs
(committed like any change), new or changed tickets, or `requirements.md` where a project keeps
one. The CLI
behind it: `agentloop.py ingest <path>`, `knowledge`, `annotate <id> --summary … --use …`.

How it reaches agents:
- **Every session:** the brief quotes `sprint.md` (and `requirements.md` if used) and the decisions log, and lists
  the knowledge index (one line per item, not the content).
- **Every ticket:** the planner reads the index and picks the items the ticket needs; implement,
  review and fix get those items' paths.
- **Every shipped ticket:** `mark <id> shipped` (the ship skill runs it) appends the handoff's
  decisions to `decisions.md`, so they outlive the 12 newest handoffs the brief shows.

## Agents per stage

```json
"agents": {"default": "codex", "review": "claude"},
"agentModels": {"claude": "opus"},
"agentArgs": {"codex": ["--ignore-user-config"]},
"agentTimeoutMinutes": 30
```

A stage without its own entry (`plan`, `implement`, `review`, `fix`, `handoff`) uses `default`.
Planning and the handoff run read-only; review may run the checks but not edit (the runner
flags a reviewer that changed the worktree); implement and fix edit. Agents never write outside
their worktree: the runner writes the plans and handoff notes from what they return.

## Runs

`agentloop.py run [ids] [--plan] [--detach]` prepares the worktrees, refreshes the brief and runs
the loop. Each run keeps its state in `<agent-loop folder>/runs/<run id>/`: `run.json` (where each
ticket is, the summary when done), `log.txt`, and every agent call's result and output. A run that
stopped resumes with `run --resume <run id>`, reusing every finished call; a `--plan` run carries
on into implementation with `run --resume <run id> --build`.

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
