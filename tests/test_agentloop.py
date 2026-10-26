"""Tests for agentloop.py against a fake board, a fake gh and a real bare repo with worktrees.

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import agentloop as al  # noqa: E402

LISTS = [{"id": "L1", "name": "Sprint Backlog"}, {"id": "L2", "name": "In Progress"},
         {"id": "L3", "name": "Code Review"}, {"id": "L9", "name": "Done"}]


def card(n, name, lst, blockers="", labels=()):
    desc = f"### Context\n\nWhy.\n\n### Acceptance Criteria\n\n- It works.\n\n### Blockers\n\n{blockers or 'None.'}"
    slug = name.lower().replace(" ", "-")
    return {"id": f"c{n}", "idShort": n, "idList": lst, "name": name, "desc": desc,
            "url": f"https://trello.com/c/x{n}/{n}-{slug}", "labels": [{"name": x} for x in labels]}


CARDS = [
    card(901, "List the runs", "L1"),
    card(902, "Show the run detail", "L1", "#901."),          # blocker open, no PR -> blocked
    card(903, "Export the run", "L1", "#904."),               # blocker has an open PR -> stacked
    card(904, "Store run files", "L3"),
    card(905, "Fix the crash", "L1", "#900, #899.", ["Bug"]),  # #900 done, #899 archived -> ready
    card(900, "Old work", "L9"),
    card(906, "Already started", "L1"),                       # has a worktree -> in-flight
    card(907, "Dead ref", "L1", "#123."),                     # dead reference -> ready
]
OPEN_PRS = [{"number": 40, "title": "Store run files", "headRefName": "feat/904-store-run-files",
             "baseRefName": "main", "isDraft": False, "reviewDecision": "", "url": "u"}]


def fake_trello(method, path, **params):
    if path.endswith("/lists"):
        return LISTS
    if path.endswith("/cards/closed"):
        return [{"idShort": 899}]
    if path.endswith("/cards"):
        return CARDS
    raise AssertionError(path)


def fake_gh(*args):
    return OPEN_PRS if "open" in args else []


def sh(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        origin = self.tmp / "origin.git"
        seed = self.tmp / "seed"
        sh("git", "init", "-q", "-b", "main", str(seed))
        (seed / "AGENTS.md").write_text("rules\n")
        sh("git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "init", "--allow-empty")
        sh("git", "-C", str(seed), "add", "-A")
        sh("git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "agents")
        sh("git", "clone", "-q", "--bare", str(seed), str(origin))
        # Project laid out like dijit: a bare repo whose worktrees are siblings named by card id.
        self.root = self.tmp / "proj"
        sh("git", "clone", "-q", "--bare", str(origin), str(self.root))
        sh("git", "-C", str(self.root), "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        sh("git", "-C", str(self.root), "fetch", "-q", "origin")
        sh("git", "-C", str(self.root), "worktree", "add", "-q", str(self.root / "main"), "main")
        sh("git", "-C", str(self.root), "worktree", "add", "-q", "-b", "feat/906-already-started", str(self.root / "906"), "origin/main")
        (self.root / "main" / ".env").write_text("X=1\n")
        cfg = json.loads((al.PLUGIN_ROOT / "templates" / "config.json").read_text())
        cfg.update(name="proj", repo="o/proj", setup=["test -f .env || cp {main}/.env .env"],
                   exclusive=["alembic/versions/"])
        cfg["trello"].update(board="B", done=["Done"])
        with redirect_stdout(io.StringIO()):
            al.main(["init", "--root", str(self.root)])
        (self.root / ".agent-loop" / "config.json").write_text(json.dumps(cfg))
        self.cwd = os.getcwd()
        os.chdir(self.root / "main")
        os.environ.update(TRELLO_KEY="k", TRELLO_TOKEN="t")
        self._orig = al.trello, al.gh_json
        al.trello, al.gh_json = fake_trello, fake_gh

    def tearDown(self):
        al.trello, al.gh_json = self._orig
        os.chdir(self.cwd)

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            al.main(list(argv))
        return buf.getvalue()

    def test_project_found_from_any_worktree(self):
        os.chdir(self.root / "906")
        self.assertEqual(al.find_project().root, self.root.resolve())

    def test_ticket_classification(self):
        got = {t["id"]: t for t in json.loads(self.run_cli("tickets", "--all"))}
        self.assertEqual(got[901]["status"], "ready")
        self.assertEqual(got[901]["branch"], "feat/901-list-the-runs")
        self.assertEqual(got[901]["base"], "origin/main")
        self.assertEqual((got[902]["status"], got[902]["blockers"]), ("blocked", [901]))
        self.assertEqual((got[903]["status"], got[903]["base"]), ("ready", "origin/feat/904-store-run-files"))
        self.assertEqual((got[905]["status"], got[905]["branch"]), ("ready", "fix/905-fix-the-crash"))
        self.assertEqual(got[906]["status"], "in-flight")
        self.assertEqual(got[907]["status"], "ready")
        ready = [t["id"] for t in json.loads(self.run_cli("tickets"))]
        self.assertEqual(ready, [901, 903, 905, 907])

    def test_brief_contents(self):
        text = self.run_cli("brief")
        self.assertIn("### Sprint Backlog (6)", text)
        self.assertIn("#902 Show the run detail [blocked: waiting on #901]", text)
        self.assertIn("stacked on #904 (PR #40)", text)
        self.assertIn("#40 Store run files", text)
        self.assertIn("Dead blocker references: #123", text)
        self.assertIn("feat/906-already-started", text)
        self.assertIn("## Sprint goal", text)
        self.assertTrue((self.root / ".agent-loop" / "brief.md").is_file())

    def test_hook_output_and_silence_outside_project(self):
        out = json.loads(self.run_cli("brief", "--hook"))
        self.assertIn("Project brief: proj", out["hookSpecificOutput"]["additionalContext"])
        os.chdir(self.tmp)
        self.assertEqual(self.run_cli("brief", "--hook"), "")

    def test_brief_without_credentials(self):
        del os.environ["TRELLO_TOKEN"]
        text = self.run_cli("brief")
        self.assertIn("Board unavailable: export TRELLO_KEY and TRELLO_TOKEN", text)
        self.assertIn("#40 Store run files", text)

    def test_prepare_creates_worktree_and_runs_setup(self):
        got = json.loads(self.run_cli("prepare", "901", "902"))
        self.assertTrue(got[0]["created"])
        self.assertTrue(all(s["ok"] for s in got[0]["setup"]))
        wt = self.root / "901"
        self.assertEqual((wt / ".env").read_text(), "X=1\n")
        self.assertEqual(al.git(wt, "branch", "--show-current").strip(), "feat/901-list-the-runs")
        self.assertIn("waiting on #901", got[1]["error"])
        again = json.loads(self.run_cli("prepare", "901"))
        self.assertEqual(again[0]["status"], "in-flight")
        self.assertNotIn("created", again[0])
        forced = json.loads(self.run_cli("prepare", "902", "--force"))
        self.assertTrue(forced[0]["created"])

    def test_handoffs_and_mark(self):
        d = self.root / ".agent-loop" / "handoffs"
        (d / "901.md").write_text(
            "---\nid: 901\ntitle: List the runs\nstatus: ready\nbranch: feat/901-list-the-runs\nupdated: 2026-10-26T10:00\n---\n\n"
            "## Summary\nLists runs.\n\n## Decisions\n- Paginate with PaginationPage.\n\n## Commit message\nfeat: list the runs\n")
        notes = json.loads(self.run_cli("handoffs", "--status", "ready"))
        self.assertEqual([n["id"] for n in notes], [901])
        self.assertIn("Paginate with PaginationPage", self.run_cli("brief"))
        self.run_cli("mark", "901", "shipped", "--pr", "https://github.com/o/proj/pull/41")
        note = al.read_handoff(d / "901.md")
        self.assertEqual((note["status"], note["pr"]), ("shipped", "https://github.com/o/proj/pull/41"))
        self.assertEqual(json.loads(self.run_cli("handoffs", "--status", "ready")), [])


if __name__ == "__main__":
    unittest.main()
