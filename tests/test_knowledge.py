"""Project knowledge: ingest, text extraction, the index, the decisions log, and how both reach agents.

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.stdin = io.StringIO("")
import agentloop as al  # noqa: E402
import knowledge  # noqa: E402
import runner  # noqa: E402

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def make_docx(path: Path) -> None:
    body = (f'<w:document {W}><w:body>'
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Daily sync</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Client: a working day needs more than </w:t></w:r><w:r><w:t>four hours.</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Level</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '</w:body></w:document>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", body)


def make_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {S} {R}><sheets><sheet name="Roster" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst {S}><si><t>Employee</t></si><si><t>Ahmod, K</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet {S}><dimension ref="A1:B2"/><sheetData>'
                   '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>Hours</t></is></c></row>'
                   '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>7.5</v></c></row>'
                   '</sheetData></worksheet>')


class KnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        src = self.tmp / "Sep Report"
        src.mkdir()
        make_docx(src / "SOP.docx")
        make_xlsx(src / "Roster.xlsx")
        (src / "deals.csv").write_text("Record ID,Deal Name\n1,Acme\n2,Globex\n")
        (src / "deals copy.csv").write_text("Record ID,Deal Name\n1,Acme\n2,Globex\n")   # identical: a duplicate
        self.src = src

    def test_ingest_folder(self):
        it = knowledge.ingest(self.state, self.src, date="2026-10-01")
        self.assertEqual((it["id"], it["kind"], it["date"]), ("sep-report", "doc", "2026-10-01"))
        self.assertEqual(sorted(f["name"] for f in it["files"]), ["Roster.xlsx", "SOP.docx", "deals copy.csv"])
        self.assertEqual(it["duplicates"], [{"name": "deals.csv", "sameAs": "sep-report/deals copy.csv"}])
        texts = {f["name"]: Path(f["text"]).read_text() for f in it["files"]}
        self.assertIn("# Daily sync", texts["SOP.docx"])
        self.assertIn("Client: a working day needs more than four hours.", texts["SOP.docx"])
        self.assertIn("Name | Level", texts["SOP.docx"])
        self.assertIn("## Sheet: Roster (2 rows, A1:B2)", texts["Roster.xlsx"])
        self.assertIn("Ahmod, K\t7.5", texts["Roster.xlsx"])
        self.assertIn("2 data rows, 2 columns", texts["deals copy.csv"])
        self.assertTrue((self.state / "knowledge" / "items" / "sep-report" / "original" / "SOP.docx").is_file())
        index = (self.state / "knowledge" / "INDEX.md").read_text()
        self.assertIn("**Sep Report**", index)
        self.assertIn("duplicate of sep-report/deals copy.csv", index)

    def test_reingest_and_annotate(self):
        knowledge.ingest(self.state, self.src)
        with self.assertRaises(ValueError):          # everything already known
            knowledge.ingest(self.state, self.src)
        transcript = self.tmp / "AI Daily sync.docx"
        make_docx(transcript)
        with self.assertRaises(ValueError):          # same bytes as SOP.docx
            knowledge.ingest(self.state, transcript)
        (self.tmp / "notes.md").write_text("call notes")
        it = knowledge.ingest(self.state, self.tmp / "notes.md", kind="transcript", title="Kickoff call")
        self.assertEqual(it["id"], "kickoff-call")
        knowledge.annotate(self.state, "kickoff-call", summary="Scope agreed.", useFor="what is in scope", status="distilled")
        line = knowledge.index_line(knowledge.resolve(self.state, ["kickoff-call"])[0])
        self.assertIn("**Kickoff call**: Scope agreed. Use for: what is in scope", line)
        self.assertNotIn("not yet distilled", line)

    def test_wide_rows_are_capped(self):
        wide = self.tmp / "wide.csv"
        wide.write_text(",".join(f"c{i}" for i in range(100)) + "\n" + ",".join("1" * 100) + "\n")
        self.assertIn("… 60 more columns", knowledge.csv_text(wide))

    def test_decisions_log_newest_first(self):
        self.assertFalse(knowledge.log_decisions(self.state, "#1", "One", ["None"], None))
        self.state.mkdir(parents=True, exist_ok=True)
        knowledge.log_decisions(self.state, "#1", "One", ["Use PaginationPage."], "https://pr/1")
        knowledge.log_decisions(self.state, "#2", "Two", ["Rates come from the CAR sheet."], None)
        text = (self.state / "decisions.md").read_text()
        self.assertTrue(text.startswith("# Decisions\n"))
        self.assertLess(text.index("#2 Two"), text.index("#1 One (https://pr/1)"))
        self.assertEqual(text.count("# Decisions"), 1)


class KnowledgeInTheLoopTest(unittest.TestCase):
    """Knowledge and decisions reach the brief and the agents."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        repo = self.tmp / "app"
        os.system(f"git init -q -b main {repo} && git -C {repo} -c user.name=t -c user.email=t@t commit -q --allow-empty -m i")
        state = repo / ".git" / "agent-loop"
        state.mkdir()
        cfg = al.merged(al.DEFAULT_CONFIG, {"tracker": {"type": "file", "columns": {"ready": "Todo"}, "show": ["Todo"], "done": ["Done"]}})
        (state / "config.json").write_text(json.dumps(cfg))
        (state / "tickets.json").write_text(json.dumps([{"id": 1, "name": "One", "status": "Todo"}]))
        (self.tmp / "sync.md").write_text("Working day means more than four hours.")
        knowledge.ingest(state, self.tmp / "sync.md", kind="transcript", title="Daily sync", item_id="daily-sync")
        knowledge.annotate(state, "daily-sync", summary="Working-day rule.", useFor="working days")
        (state / "handoffs").mkdir()
        (state / "handoffs" / "1.md").write_text("---\nid: 1\ntitle: One\nstatus: ready\n---\n\n## Summary\nx\n\n## Decisions\n- Count weekends as hours only.\n")
        self.state, self.cwd = state, os.getcwd()
        os.chdir(repo)

    def tearDown(self):
        os.chdir(self.cwd)

    def cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            al.main(list(argv))
        return out.getvalue()

    def test_brief_lists_knowledge_and_decisions(self):
        self.cli("mark", "1", "shipped", "--pr", "https://pr/9")
        text = self.cli("brief")
        self.assertIn("## Project knowledge", text)
        self.assertIn("`daily-sync` (transcript", text)
        self.assertIn("[not yet distilled]", text)
        self.assertNotIn("Client requirements", text)       # optional, and this project has none
        (self.state / "requirements.md").write_text((al.PLUGIN_ROOT / "templates" / "requirements.md").read_text())
        self.assertNotIn("Client requirements", self.cli("brief"))  # the untouched template counts as none
        (self.state / "requirements.md").write_text("## Outcomes\n\n- Deals stay in line with their sheets.\n")
        self.assertIn("Deals stay in line with their sheets.", self.cli("brief"))
        self.assertIn("## Decisions log (newest first)", text)
        self.assertIn("### ", text.split("## Decisions log (newest first)")[1])
        self.assertIn("- Count weekends as hours only.", text)

    def test_planner_picks_reach_later_stages(self):
        prompts = {}

        def fake(*, prompt, log_path, **_):
            label = Path(log_path).name.split(".")[1]
            prompts[label] = prompt
            if label == "plan":
                return {"feasible": True, "question": None, "summary": "s", "files": ["a.py"], "steps": [], "risks": [],
                        "planMarkdown": "p", "knowledge": ["daily-sync", "no-such-item"]}
            if label == "implement":
                return {"summary": "ok", "checks": [], "stuck": None}
            if label == "review":
                return {"verdict": "pass", "findings": [], "unmetCriteria": [], "checks": []}
            return {"summary": "s", "decisions": [], "followUps": [], "openFindings": [], "commitMessage": "feat: x",
                    "prTitle": "X", "prChanges": []}

        p = al.find_project()
        project = al.run_project(p)
        d = self.state / "runs" / "r"
        d.mkdir(parents=True)
        t = {"id": "1", "ref": "#1", "name": "One", "url": "", "desc": "d", "worktree": str(self.tmp / "app"), "branch": "b", "base": "HEAD"}
        run = runner.Run(d, project, [t], {"default": "claude"})
        run.save()
        with redirect_stdout(io.StringIO()):
            runner.Loop(run, fake).execute()
        self.assertIn(str(knowledge.kdir(self.state) / "INDEX.md"), prompts["plan"])
        self.assertIn("## Project knowledge for this ticket", prompts["implement"])
        self.assertIn("items/daily-sync/text/sync.md.txt", prompts["implement"])
        self.assertIn("**Daily sync**", prompts["review"])
        self.assertNotIn("no-such-item", prompts["implement"])


if __name__ == "__main__":
    unittest.main()
