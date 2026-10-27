# agent-loop

agent-loop lets coding agents do the routine part of a sprint, so that you do not have to explain
the project in each session or move work from one agent to the next.

- **Each session starts with a brief:** the board, the sprint goal, the decisions, the project
  knowledge, the open pull requests and the notes of earlier agents.
- **The loop does the tickets in parallel:** plan, implement, review, fix. Each ticket has its own
  git worktree.
- **You approve and merge.** The agents do not commit, push or merge.

It works with **Claude Code**, **Codex** and **Cursor**, and you can use a different tool for each
stage. Trackers: Trello, GitHub Projects, Jira or a local `tickets.json` file. Repositories: GitHub.

## How it works

```
session start ──► brief (board, sprint goal, decisions, knowledge, PRs, handoff notes)

sprint ──► plan all tickets ──► batch the tickets that do not overlap
             │
             └─ for each ticket, in parallel:
                  implement ──► checks + review ──► fix ──► re-review ──► handoff note
                                     (max. maxFixRounds fix rounds)

ship ──► you approve the commands ──► commit, push, pull request ──► you merge
```

The changes stay staged in the worktree until you approve them.

## Quick start

1. Install the plugin for your tool (see [Install](#install)).
2. Open a session in your repository.
3. Use the `setup` skill. Give it the URL of your board.
4. Write the sprint goal in `sprint.md`.
5. Use the `ticket` skill with `--plan` on one ticket. Read the plan.
6. Use the `sprint` skill to run all ready tickets.
7. Use the `status` skill to see the result.
8. Use the `ship` skill to open the pull requests.

## Install

### Claude Code

```
/plugin marketplace add Juraj-Sulimanovic/agent-loop
/plugin install agent-loop@agent-loop
/plugin install agent-loop-panel@agent-loop      # optional: the live progress pane
```

The panel shows the newest run of the project in a band above the prompt: the phases, and one
line for each ticket with its running agent calls. The band shows while a run continues, and for
30 minutes after it is complete. For the full view with the log, use `/agent-loop-panel`. Claude
Code puts that pane beside the chat in the full-screen layout, and above the prompt in the other
layout.

### Codex

```bash
codex plugin marketplace add Juraj-Sulimanovic/agent-loop
codex plugin add agent-loop@agent-loop
```

Approve the session hook of the plugin one time, in `/hooks`.

### Cursor

```bash
git clone https://github.com/Juraj-Sulimanovic/agent-loop ~/.cursor/plugins/local/agent-loop
```

Reload Cursor (**Developer: Reload Window**). Make sure that the plugin shows in **Customize**.

- The plugin must be a real folder. A symbolic link to a different location does not load.
- On Enterprise plans, an administrator must enable **Allow Local Plugin Imports**.
- The session hook does not run in Cursor cloud agents.

### Requirements

- Python 3.10 or later. agent-loop uses only the standard library.
- `git` and the GitHub CLI (`gh`), logged in.
- The agent tools that you use, installed and logged in: `claude`, `codex` (`codex login`),
  `cursor-agent` (`cursor-agent login`).

## Set up a repository

Open a session in the repository. Use the `setup` skill (in Claude Code: `/agent-loop:setup`).
It finds the layout, the branch, the checks and the setup commands. It asks for the tracker, its
columns and the tool for each stage. Then it writes the configuration and runs `doctor` until all
checks pass.

Keep the tracker credentials in environment variables. Do not write them in a file.

| Tracker | Credentials |
|---|---|
| Trello | `TRELLO_KEY`, `TRELLO_TOKEN` |
| GitHub Projects | `gh auth refresh -s project` |
| Jira | `JIRA_EMAIL`, `JIRA_API_TOKEN` |
| File | None. Put the tickets in `tickets.json` in the agent-loop folder. |

Then write the sprint goal in `sprint.md`. The brief shows it in each session.

### Where the files are

agent-loop keeps its state out of your commits.

| Layout | State folder | Ticket worktrees |
|---|---|---|
| Plain clone | `<clone>/.git/agent-loop/` | `<clone>/../<repo>-worktrees/<id>` |
| Bare repository with worktrees | `<root>/.agent-loop/` | `<root>/<id>` |

The state folder holds the configuration, `sprint.md`, the knowledge, the decisions log, the
plans, the handoff notes and the runs.

## Skills

| Skill | Use it to |
|---|---|
| `setup [url]` | Configure the repository. |
| `brief` | Refresh the brief during a session. |
| `sprint [ids] [--plan]` | Run the loop on all ready tickets, or on the tickets you name. |
| `ticket <id> [--plan]` | Run the loop on one ticket, also when it is blocked. |
| `status [run]` | See which tickets are ready, which need you, and which waited. |
| `ingest <path>` | Add transcripts, documents or test files to the project knowledge. |
| `ship [ids]` | Commit, push and open the pull requests, after your approval. |

In Claude Code, the skills are `/agent-loop:<skill>`. Codex and Cursor find them by name.

`--plan` stops after the planning step. To continue with the same plans, run
`agentloop.py run --resume <run> --build`.

The skills use `scripts/agentloop.py`. You can also use it directly. For the commands, run
`python3 scripts/agentloop.py --help`.

## Which tickets run

A ticket in the Ready column is **ready** when these conditions are true:

- It has no worktree with work in it and no open pull request.
- All its blockers are done, closed, archived or merged.

If one blocker has an open pull request, the ticket can start on the branch of that pull
request.

agent-loop reads the blockers from Jira links and GitHub issue dependencies. On other trackers,
it reads `#123` references below a `Blockers` heading, or after "Blocked by" or "Depends on".

Two tickets that change the same file do not run in the same batch. The same rule applies to the
`exclusive` paths, for example lockfiles and migration folders. agent-loop holds the second
ticket for a later run and tells you why.

## Review

Each ticket gets a review before it gets to you:

1. **Checks.** The runner runs the configured checks. A failed check is a blocker.
2. **Reviewers.** Three reviewers work in parallel: **spec** (each acceptance criterion),
   **correctness** (edge cases, errors, data handling, tests) and **standards** (the rules of the
   repository; code smells are notes only).
3. **Skeptics.** For each blocking finding, a skeptic tries to show that it is wrong. The fixer
   gets only the confirmed findings.
4. **Pre-existing problems** in code that the change did not touch do not block. The handoff note
   lists them, so that you can make tickets for them.

The fix rounds converge. The fixer answers each finding and fixes all instances of the bug class.
A re-review examines only the last fix. If the same problem stays after a fix, the loop stops and
the ticket needs your decision.

**Risk.** A change to a file in `sensitivePaths` (CI, migrations, deployment, authentication,
billing) is high risk. It gets a security reviewer and three skeptics for each finding. The
`ship` skill shows its full diff and asks for a careful human review.

**Guidance.** A `REVIEW.md` file at the root of the repository tells the reviewers what is
important and what to skip. `agentloop.py findings` shows findings that occur again across runs,
so that you can fix them at the source.

Each handoff note has **Testing**, **Risk** and **How to revert**. The `ship` skill puts them in the
pull request when the template has these sections.

## Project knowledge

Keep meeting transcripts, client documents and test files in the project knowledge. They stay
in the state folder. They never go into the repository.

To add an item, use the `ingest` skill. It copies the files, makes a text version of each
`.docx`, `.xlsx`, `.csv` and `.pdf` file, and writes a summary. Then it proposes changes to the
repository documents and the tickets. It makes the changes only after your approval.

The brief lists each item in one line, without the content. The planner selects the items that a
ticket needs, and the later steps get their paths.

When you ship a ticket, agent-loop adds its decisions to `decisions.md`. The brief shows this
log in each session.

## Permissions and cost

**Permissions.** The agents work only in the ticket worktrees. They get these permissions:

| Tool | Edit steps (implement, fix) | Review step | Read steps (plan, handoff) |
|---|---|---|---|
| Claude Code | `--permission-mode auto` | `auto`, without the edit tools | `auto`, without the edit tools |
| Codex | `-s workspace-write`, plus the git folder of the repository | Same as the edit steps | `-s read-only` |
| Cursor | `--force --trust` | `--force --trust` | `--mode plan --trust` |

During a review, the runner compares the worktree before and after each reviewer. If a reviewer
changed a file, the runner adds a finding.

**Cost.** Each agent call uses tokens from your account with the tool. For each ticket, expect
these calls:

- normal ticket: 1 plan, 1 implement, 3 reviewers, 1 skeptic for each blocking finding,
  1 handoff;
- each fix round: 1 fix, 3 reviewers, the skeptics;
- high-risk ticket: 4 reviewers, and 3 skeptics for each blocking finding.

To decrease the cost, decrease `review.lenses`, set `review.verify` to `false`, or decrease
`maxFixRounds`.

## Configuration

`setup` writes `config.json` in the state folder. These are the most important keys:

| Key | Default | Purpose |
|---|---|---|
| `repo` | from `origin` | The GitHub repository, `owner/name`. |
| `base` | `origin/main` | The branch that new work starts from. |
| `tracker` | Trello | The tracker type, its board and its columns (`ready`, `doing`, `review`, `done`). |
| `checks` | from the Makefile or `package.json` | The commands that must pass, for example `make lint`. |
| `setup` | from the lockfiles | The commands that prepare a new worktree. |
| `exclusive` | lockfiles, migrations | Paths that two tickets must not change in the same batch. |
| `sensitivePaths` | from the repository | Paths that make a ticket high risk. |
| `agents` | `{"default": "claude"}` | The tool for each stage: `plan`, `implement`, `review`, `fix`, `handoff`. |
| `agentModels` | `{}` | The model for each tool, for example `{"claude": "opus"}`. |
| `agentArgs` | `{}` | More command-line arguments for each tool. |
| `maxParallel` | `10` | The maximum number of tickets in one batch. |
| `maxFixRounds` | `2` | The maximum number of fix rounds for each ticket. |
| `review` | 3 lenses, verify, 3 votes | `lenses`, `verify`, `votes`, `file` (`REVIEW.md`). |
| `pr` | the repository template | The pull request template, the ticket line and draft mode. |

## Runs

`agentloop.py run` prepares the worktrees, refreshes the brief and runs the loop. With
`--detach`, it runs in the background.

Each run has a folder in `runs/<run id>/` in the state folder. It contains `run.json` (the stage
of each ticket and the summary), `log.txt`, the prompt and result of each agent call, and the
findings of each review round. If a run stops, run `agentloop.py run --resume <run id>`. It
continues with the results that it has already.

## Troubleshooting

| Problem | Solution |
|---|---|
| The brief says "Board unavailable". | Set the tracker credentials in the shell that starts the agent tool. |
| GitHub Projects: "missing required scopes". | Run `gh auth refresh -s project`. |
| Codex stops with a missing API key. | Your Codex configuration uses a custom provider. Set its key, or set `"agentArgs": {"codex": ["--ignore-user-config"]}`. |
| Cursor asks if you trust the folder. | Update agent-loop. The runner gives `--trust` to Cursor for each worktree. |
| A ticket stays "in flight". | It has work in its worktree or an open pull request. Ship it, or remove the worktree. |

## Development

Run the tests:

```bash
python3 -m unittest discover -s tests
```

The tests need only Python and `git`. They do not call an agent tool.

## License

MIT. See [LICENSE](LICENSE).
