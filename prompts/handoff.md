Ticket {{ref}} "{{name}}" ({{url}}) has been implemented in the worktree `{{worktree}}` (your working directory; branch `{{branch}}`, base `{{base}}`). Status: {{status}}. Do not edit any file in the worktree.{{cwdNote}}

Read `AGENTS.md` (or `CLAUDE.md`) for the commit message and pull request conventions. The staged change against `{{base}}`:

```
{{stat}}
```

```diff
{{diff}}
```

The plan was:

{{plan}}

{{lastReview}}
{{whyNote}}
{{preExistingNote}}
{{riskNote}}

Return the handoff the next agents on this project will read:
- `summary`: two or three sentences, what the change does, in a reviewer's words{{summaryNote}}.
- `decisions`: choices another agent on this codebase should know and follow (a shape, a name, a place for things); empty is fine.
- `followUps`: work this ticket surfaced but did not do; empty is fine.
- `openFindings`: unresolved findings from the last review; empty when none.
- `commitMessage`: one line, per the repository conventions.
- `prTitle`: a sentence, per the repository conventions.
- `prChanges`: three to five bullets, one behaviour each.
- `testing`: how the change was verified (the checks, the tests added, anything run by hand) and what it showed.
- `risk`: what could go wrong once it ships, and who or what would notice.
- `revert`: how to undo it (a plain revert, or the extra step a migration, data change or config needs).

{{saveNote}}
