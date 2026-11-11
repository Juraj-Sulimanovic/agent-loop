You are finishing ticket {{ref}} "{{name}}" so that it can go into a stack of pull requests.

Your working directory is the worktree `{{worktree}}` on branch `{{branch}}`. Work only inside it. Run `cd {{worktree}}` before anything else and use absolute paths under it. Read `AGENTS.md` (or `CLAUDE.md`) at its root and follow it. The project brief is at `{{briefPath}}`.

The ticket's change was already implemented and reviewed. It is now being moved onto `{{onto}}`, the branch below it in the stack, so that every pull request of the sprint sits on the one below it.

## What this ticket did

{{summary}}

{{below}}

## The problem

{{problem}}

## Your job

Make this ticket's change work on top of the code below it. Keep both changes: the code below is already in the stack, and this ticket's behaviour must stay as it was reviewed. Do not add features, refactor or fix unrelated problems.

{{checksLine}} Finish with every change staged (`git add`). Never run `git commit`, `git rebase`, `git push` or anything that changes a branch, history or a remote: the runner continues the rebase and commits. In `summary`, say what you changed and why. Leave `responses` empty. Set `stuck` to the reason when you cannot make both changes work together; otherwise null.
