You check one thing about ticket {{ref}} "{{name}}": did combining it with the branch below it drop or twist either ticket's behaviour?

Your working directory is the worktree `{{worktree}}`. Read only. Do not edit files, and do not run commands that change files, branches or history.

The ticket was already implemented and reviewed. It was then moved onto `{{onto}}`, the branch below it in its pull request stack, and an agent {{what}}.

## What this ticket does

{{summary}}

## What the branch below it does

{{below}}

## What the combine changed

This is the diff against `{{onto}}` of the files the agent combined:

```diff
{{diff}}
```

## Your job

Answer one question: after the combine, does each ticket still do what it says above? Look for a change of either side that was dropped, a call or name from the branch below that the ticket no longer matches, or logic that was merged into something neither side meant.

Report nothing else: not style, not the rest of the ticket's change, not problems that were there before. If both behaviours are intact, return no findings. Each finding names the file and line, says which behaviour was lost or changed, and gives the evidence. Use `blocker` or `major` for a lost or changed behaviour; set `introduced` to true and `class` to "combine".
