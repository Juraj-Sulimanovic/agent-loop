Ticket {{ref}} "{{name}}" ({{url}}) has been implemented in the worktree `{{worktree}}` (your working directory; branch `{{branch}}`, base `{{base}}`). Status: {{status}}. Do not edit any file.

Read the staged diff (`git diff --cached {{base}}`) and `AGENTS.md` (or `CLAUDE.md`) for the commit message and pull request conventions. The plan was:

{{plan}}

{{lastReview}}
{{whyNote}}

Return the handoff the next agents on this project will read:
- `summary`: two or three sentences, what the change does, in a reviewer's words{{summaryNote}}.
- `decisions`: choices another agent on this codebase should know and follow (a shape, a name, a place for things); empty is fine.
- `followUps`: work this ticket surfaced but did not do; empty is fine.
- `openFindings`: unresolved findings from the last review; empty when none.
- `commitMessage`: one line, per the repository conventions.
- `prTitle`: a sentence, per the repository conventions.
- `prChanges`: three to five bullets, one behaviour each.
