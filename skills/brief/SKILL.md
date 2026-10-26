---
name: brief
description: Refresh and read the project brief (board state, sprint goal, client requirements, open and merged PRs, local worktrees, and what other agents did). Use when the user asks for the state of the project, the board or the sprint, or when the brief from session start is stale.
---

# Project brief

The CLI is `scripts/agentloop.py` in this plugin, two directories above this skill's base
directory. Call it `$AL` below:

```bash
AL="python3 <this skill's base directory>/../../scripts/agentloop.py"
$AL brief
```

It prints the brief and saves it to `.agent-loop/brief.md` in the project root, where agents
read it. Outside a project (no `.agent-loop/config.json` above the current directory) it
says so; offer `$AL init --root <project root>`.

When the board is unavailable the brief says why. Usually `TRELLO_KEY` or `TRELLO_TOKEN` is
not exported in the shell that started the session; the user exports both and restarts, or
runs `! export TRELLO_TOKEN=...` (a token from
`https://trello.com/1/authorize?expiration=30days&scope=read,write&response_type=token&name=agent-loop&key=<KEY>`).
Never write a token into a file.

Answer the user's question from the brief; do not paste the brief back at them.
