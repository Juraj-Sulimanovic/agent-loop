{{header}}

## Your job: plan, do not implement

Do not edit any file. Read the code the ticket touches and return a plan: the approach, the files that change, the steps and the risks. Reuse what exists; name only code that exists on the branch.

- `planMarkdown`: the plan as markdown, for the implementer (approach, files, steps, risks).
- `files`: every repo-relative file you expect to change or create. Another planner works beside you, and two tickets touching the same file are not run together, so be complete rather than minimal.
- `feasible`: false only when the ticket cannot be done without a decision a human must make (contradicting requirements, missing access, an unanswerable ambiguity); put that one question in `question`. Ordinary uncertainty is a risk, not a blocker. Otherwise `question` is null.
