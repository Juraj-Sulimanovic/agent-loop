---
name: brief
description: Refresh and read the project brief (tracker board state, sprint goal, client requirements, open and merged PRs, local worktrees, and what other agents did). Use when the user asks for the state of the project, the board or the sprint, or when the brief from session start is stale.
---

# Project brief

The CLI is `scripts/agentloop.py` in this plugin, two directories above this skill's base
directory. Call it `$AL` below:

```bash
AL="python3 <this skill's base directory>/../../scripts/agentloop.py"
$AL brief
```

It prints the brief and saves it as `brief.md` in the project's agent-loop folder, where
agents read it.
Outside a configured repository it says so; offer `/agent-loop:setup`.

When the board is unavailable the brief says why. Usually the tracker's credentials are not
exported in the shell that started the session (`TRELLO_KEY` + `TRELLO_TOKEN`, or
`JIRA_EMAIL` + `JIRA_API_TOKEN`), or `gh` lacks the `project` scope for a GitHub board
(`gh auth refresh -s project`). The user can export them into this session with
`! export NAME=...`. Never write a credential into a file.

Answer the user's question from the brief; do not paste the brief back at them.
