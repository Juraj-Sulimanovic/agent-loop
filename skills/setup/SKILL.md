---
name: setup
description: Set up or reconfigure agent-loop for the repository the session is in. Detects the repo layout, checks, setup commands and PR template, asks for the tracker (Trello, GitHub Projects, Jira or a local file) and its columns and which agent tool (Claude Code, Codex, Cursor) runs each stage, writes the config and runs the doctor.
argument-hint: "[tracker board URL]"
disable-model-invocation: true
---

# Set up agent-loop for this repository

Find what the repository already tells you. Ask only for what you cannot find. Write the
configuration. Then make sure that it works.

The CLI is `scripts/agentloop.py` in this plugin, two directories above this file:
`AL="python3 <plugin root>/scripts/agentloop.py"`.

Ask the user one step at a time, with a few questions in each step. Use multiple choice when you
have a question tool. Put the detected value first and mark it "(Recommended)". Do not ask about
a value that detection found, unless the value looks wrong.

## 1. Detect

```bash
$AL detect                                   # or: $AL detect --tracker-url "<board URL>"
```

The output has two parts:

- `facts`: the layout, the main checkout, the default branch, the remote, the Make targets, the
  PR template, `reviewFile`, the exported tracker credentials and an existing configuration.
- `config`: a suggested configuration.

If `facts.existingConfig` has a value, this is a reconfiguration. Use the values in that file as
the defaults.

Tell the user in three or four lines what you found: the repository, the layout, the state folder
(`facts.store`) and the checks. For a plain clone, the state folder is in `.git/`. It is never
committed.

## 2. Tracker

If the user gave no board URL, ask which tracker the team uses:

| Tracker | URL | Credentials (environment only) |
|---|---|---|
| Trello | `https://trello.com/b/<id>/<name>` | `TRELLO_KEY`, `TRELLO_TOKEN` |
| GitHub Projects | `https://github.com/orgs/<org>/projects/<n>` or `/users/<user>/projects/<n>` | `gh auth refresh -s project` |
| Jira | `https://<site>.atlassian.net/jira/software/projects/<KEY>/boards/<n>` | `JIRA_EMAIL`, `JIRA_API_TOKEN` |
| File (no board yet) | none | none; the tickets go in `tickets.json` in the state folder: `[{id, name, status, desc, labels, blockers}]` |

Then run `$AL detect --tracker-url "<url>"` again. It fills in the tracker.

- **Jira:** the default `jql` takes the open sprints of the project. If the team uses Kanban,
  suggest `project = KEY AND statusCategory != Done ORDER BY Rank`.
- **GitHub Projects:** if the column field has a name other than `Status`, set `statusField`.

If the credentials are missing, give the user the command to run with `!`, for example
`! export JIRA_EMAIL=... JIRA_API_TOKEN=...`. Wait for the user. Tell the user to also add the
export to the shell that starts the agent tool.

## 3. Columns

Write the configuration to a temporary file. Then read the real column names of the board:

```bash
$AL --config <temporary file> statuses
```

Ask for these columns. Use the real names as the options:

- **Ready:** the column that the loop takes tickets from.
- **Doing** and **Review:** where a ticket goes when work starts, and when its PR opens.
- **Done** (multiple choice): the columns that mean "merged". A blocker in one of these columns
  is resolved.

Set `show` to the columns that the brief lists: from Ready to Review, in board order.

## 4. Loop settings

Ask these questions in one step:

- **Checks** (multiple choice): the detected lint, type and test commands. If a test command
  needs a shared database or service, tell the user that parallel agents will interfere. Suggest
  a unit-test-only target.
- **Setup** (multiple choice): the detected commands for a new worktree, for example "copy
  `.env`" and "install dependencies".
- **Parallel tickets:** 3, 5 or 10. Each ticket uses four to six agent calls.
- **Pull requests:** ready for review, or draft.
- **Agent tools:** the tool for each stage. Offer the installed tools (`claude`, `codex`,
  `cursor-agent`). The default is the tool of this session. Suggest a different tool for
  `review` than for `implement`, for an independent review. This sets `agents.default` and the
  stage overrides (`plan`, `implement`, `review`, `fix`, `handoff`).

If the Codex configuration of the team uses a custom provider that a headless run cannot reach,
set `agentArgs.codex` to `["--ignore-user-config"]`.

Then ask about review depth:

- **Sensitive paths:** confirm the detected `sensitivePaths` (CI workflows, migrations,
  deployment files, authentication and billing code). A change to these paths gets a security
  reviewer and three skeptic votes for each finding. The ship skill marks it for a careful human
  review.
- **REVIEW.md:** if `facts.reviewFile` is false, offer a short draft from `AGENTS.md` for the user
  to commit. It says what a failure is in this project, what not to report, which paths to skip,
  and that each finding needs a file and a line.

Keep the detected `exclusive` paths and `contextFiles`. Keep `pr.ticketLine` as
`#### Ticket [{ref}]({url})`, unless the PR template uses a different format. The placeholders
are `{ref}`, `{id}`, `{url}` and `{name}`.

## 5. Write and test

```bash
$AL --config <temporary file> init          # writes to facts.store; keeps an existing sprint.md
$AL doctor
```

Fix each FAIL line with the user, for example a wrong column name, a missing Make target or a
missing scope. Run `doctor` again until it passes. Then run `$AL brief` and show its first lines.

Tell the user:

- Write the sprint goal in `<state folder>/sprint.md`.
- Add meeting transcripts, specifications and test files with the ingest skill.
- Each new session in this repository starts with the brief.
- For the first run, use the ticket skill with `--plan`. Then use the sprint skill. A run
  continues in the background. The status skill shows its progress.
