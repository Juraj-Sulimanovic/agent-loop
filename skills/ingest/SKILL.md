---
name: ingest
description: Add client material to the project knowledge (meeting transcripts, client documents, sample and test files, exports) so every agent on the project can find it, then distil what it decides into the project's own docs and tickets after the user approves. Use when the user shares or points at transcripts, specs, reports or test data for the project.
argument-hint: "<file or folder> [...]"
---

# Add client material to the project knowledge

Raw material goes into the knowledge folder, never into the brief: the brief lists one line per
item, the planner picks the items a ticket needs, and agents read only those. What the material
*decides* goes where the project already keeps its truth, after the user agrees.

The CLI is `scripts/agentloop.py` in this plugin, two directories above this SKILL.md:
`AL="python3 <plugin root>/scripts/agentloop.py"`. The material is client-confidential: it stays
in the project's agent-loop folder, which is never committed. Never paste it into tickets, PRs,
commits, code comments or anything published.

## 1. Ingest

```bash
$AL ingest "<path>" ["<path>" ...]      # one item per file or folder; --kind transcript|doc|data|other, --title, --date
```

Each path becomes one item: the files are copied, identical files (already in the knowledge
folder, or repeated in the folder) are skipped and reported, and readable text versions are
written for `.docx`, `.xlsx`, `.csv`, `.pdf` (when `pdftotext` is installed) and text files.
Tell the user in one line per item what was added and what was skipped as a duplicate.

## 2. Describe each item

Read each item's text versions (paths in `$AL knowledge`; for a large workbook the sheet list and
first rows are enough), then:

```bash
$AL annotate <id> --title "<what it is>" --kind <kind> --date <YYYY-MM-DD> \
  --summary "<one or two sentences: what it contains>" --use "<what an agent should read it for>"
```

The date is when the meeting happened or the document is from (a transcript usually states it),
not the download date. `--use` is what makes the planner pick it: name the questions it answers
("the working-day rule", "sample deals to test the HubSpot import against"). Flag personal data
(names, emails, phone numbers) in the summary so agents treat the file with care.

## 3. Distil into the project's docs and tickets

For transcripts and client documents, list what they settle: rules and definitions, acceptance
tests, changes of scope, decisions and open questions. Then check each against where the project
already records such things, and drop what is already there:

- **Repository docs** (the config's `contextFiles`: `CONTEXT.md`, `docs/notes/`, ADRs and the like)
  for domain rules and definitions agents must follow. Propose the edit as a change in the main
  checkout for the user to review and commit like any other; follow the repository's conventions
  for those files.
- **Tickets** for new or changed work: propose new tickets or edits to existing ones (the
  team's ticket conventions apply; for Trello boards, the trello-tickets skill if installed).
- **`<agent-loop dir>/requirements.md`** only for a project that keeps one (it exists and is not
  the template): outcomes and constraints with no other home.

Show each proposal with its source in brackets (`[daily sync, 2026-10-20]`), quote the client's
own words for rules, and say where two sources disagree and ask which stands. Things that are
project management rather than agent work (approvals pending, commercial questions) need no home;
the transcript in the knowledge folder is enough.

Make the edits only after the user approves them, never committing or touching the board without
that. Then mark each distilled item:

```bash
$AL annotate <id> --status distilled
```

Data and test files need no distilling; mark them distilled once described. Finish with
`$AL brief` and show the "Project knowledge" section.
