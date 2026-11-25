# Changelog

All notable changes. Versions follow [semantic versioning](https://semver.org/); before 1.0, a
minor version can change configuration keys, and its entry says how to update.

## 0.10.0 (2026-11-25)

The first public release.

### The loop

- A **session brief** at session start: the board, the sprint goal, decisions, project
  knowledge, open and merged pull requests, local worktrees and what earlier agents did.
- **Parallel tickets** in their own git worktrees: plan, implement, review, fix, handoff. Tickets
  that change the same files, or that the tracker says are blocked by another ticket in the run,
  wait. With `commitAtHandoff` (on by default) they then build on the finished ticket in the same
  run.
- In Claude Code, the loop runs as a **Workflow**, with Claude Code's own progress view.
  Codex, Cursor and terminal runs use the Python runner (experimental).
- Trackers: Trello, GitHub Projects, Jira and a local `tickets.json` file.

### Review

- Your **checks run first**. While a check fails, no reviewer runs: the fixer gets the check
  output, and the review starts once the checks pass.
- **Spec, correctness and standards** reviewers in parallel, and a skeptic for each blocking
  finding. Problems in code the change did not touch are listed as pre-existing and never block.
- A **scope check**: the staged files that the plan did not name go to the spec reviewer, who
  flags each one unless the ticket needs it. `review.scopeIgnore` leaves out lockfiles. The
  handoff note lists them (`outOfPlan:`).
- **Converging fix rounds**: the fixer answers each finding, a re-review reads only what the fix
  changed, and the loop stops when the same problem survives a fix.
- **High-risk paths** (`sensitivePaths`) get a security reviewer and three skeptics per finding.
- A `REVIEW.md` at the repository root tells the reviewers what matters.

### Shipping

- The **ship** skill builds pull request stacks locally (`pr.stack`: `chains` or `sprint`),
  resolves rebase conflicts with one agent and reviews that combine, then shows the push and
  `gh pr create` commands for one approval. Optional `gh stack` linking.
- Handoff notes carry testing, risk and how to revert, for the pull request body.

### Updating from an earlier version

Run `/agent-loop:setup` again to see the new settings (`pr.stack`, `pr.ghStack`,
`review.scopeIgnore`). Existing configuration keeps working: missing keys take their defaults.

## Before 0.10

The project was developed privately before 0.10, and its history was squashed into one commit for
the public release. In short: 0.2 added the Trello, GitHub Projects and Jira trackers; 0.4 the
project knowledge folder; 0.5 the review lenses and skeptics; 0.6 converging review and risk
levels; 0.8 the Claude Code Workflow; 0.9 stacking of waiting tickets.
