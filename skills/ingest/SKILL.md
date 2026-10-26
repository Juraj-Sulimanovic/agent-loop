---
name: ingest
description: Add client material to the project knowledge (meeting transcripts, client documents, sample and test files, exports) so every agent on the project can find it, then distil what it decides into the client requirements after the user approves. Use when the user shares or points at transcripts, specs, reports or test data for the project.
argument-hint: "<file or folder> [...]"
---

# Add client material to the project knowledge

Raw material goes into the knowledge folder, never into the brief: the brief lists one line per
item, the planner picks the items a ticket needs, and agents read only those. What the material
*decides* goes into `requirements.md`, the short always-loaded part, after the user agrees.

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

## 3. Distil into the requirements

For transcripts and client documents, list what they settle: requirements, rules and
definitions, acceptance tests, changes of scope, decisions and open questions. Then show the user
the proposed edits to `<agent-loop dir>/requirements.md`, each with its source in brackets
(`[daily sync, 2026-10-20]`), and say what the current file already covers. Prefer the client's
own words for rules. Where two sources disagree, say so and ask which stands.

Write the edits only after the user approves them, keeping `requirements.md` short: outcomes,
constraints, rules, acceptance tests, open questions. Then mark each distilled item:

```bash
$AL annotate <id> --status distilled
```

Data and test files need no distilling; mark them distilled once described. Finish with
`$AL brief` and show the "Project knowledge" section.
