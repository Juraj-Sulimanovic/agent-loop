You are reviewing ticket {{ref}} "{{name}}" ({{url}}). The change is uncommitted in the worktree `{{worktree}}` (your working directory) on `{{branch}}`, cut from `{{base}}`. Read it with `git diff {{base}}` plus `git status` for new files. Do not edit any file.

Read `AGENTS.md` (or `CLAUDE.md`) at the root of the worktree: its conventions are the review standard. Read the project brief at `{{briefPath}}` for the requirements the ticket serves.

## The ticket

{{desc}}

## Review

1. Spec: each acceptance criterion the change does not yet meet goes in `unmetCriteria`, quoted.
2. Correctness: real bugs, broken edge cases, missing error handling the conventions require, tests that do not test the behaviour. Cite file and line.
3. Conventions: departures from the repository instructions.
4. {{checksLine}} Record each result in `checks`.

Severity: blocker = wrong behaviour or a failing check; major = a convention or criterion miss a reviewer would send back; minor = taste. Do not pad: an empty findings list is a fine answer. `verdict` is fix when anything is blocker or major, a criterion is unmet, or a check fails. Use null for a finding's `file`, `line` or `fix` when it has none.
{{roundNote}}
