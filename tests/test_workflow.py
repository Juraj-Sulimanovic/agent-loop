"""workflows/sprint.js under Node with a fake agent: the Claude Code Workflow path of the loop.

    python3 -m unittest discover -s tests

Skipped when Node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import runner  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def args(ids, plan_only=False, **project):
    """What `agentloop.py wf start` prints, for fake tickets."""
    tickets = [{"id": i, "ref": f"#{i}", "name": f"Ticket {i}", "url": "", "desc": "d", "worktree": f"/w/{i}",
                "branch": f"feat/{i}", "base": "origin/main", "reviewFile": i == "1"} for i in ids]
    prompts = {f.relative_to(runner.PROMPTS).with_suffix("").as_posix(): f.read_text() for f in runner.PROMPTS.rglob("*.md")}
    return {"run": "r1", "runDir": "/state/runs/r1", "al": "python3 /plugin/scripts/agentloop.py", "stamp": "now",
            "planOnly": plan_only, "tickets": tickets, "prompts": prompts, "schemas": runner.SCHEMAS, "knowledge": [],
            "project": {"root": "/repo", "dir": "/state", "briefPath": "/state/brief.md", "checks": ["make check"],
                        "exclusive": [], "maxParallel": 10, "maxFixRounds": 2, "knowledgeIndex": "",
                        "review": {"lenses": ["spec", "correctness", "standards"], "verify": True, "votes": 3, "file": "REVIEW.md"},
                        "sensitivePaths": ["text.py"], **project}}


@unittest.skipUnless(NODE, "node is not installed")
class WorkflowTest(unittest.TestCase):
    def run_workflow(self, a, scenario="plain"):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(a, f)
        out = subprocess.run([NODE, str(ROOT / "tests" / "workflow_harness.mjs"), f.name, scenario],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_full_run(self):
        out = self.run_workflow(args(["1", "2", "5", "6"]))
        r = out["result"]
        self.assertEqual([(x["ref"], x["rounds"], x["preExisting"]) for x in r["ready"]], [("#1", 1, 1)])
        self.assertEqual(r["deferred"], [{"id": "2", "ref": "#2", "reason": "overlaps #1 on a.py"}])
        self.assertEqual([n["ref"] for n in r["needsHuman"]], ["#6", "#5"])
        self.assertIn("the same issue survived a fix (same bug in c.py)", r["needsHuman"][1]["question"])
        # the native view's rows
        for label in ("plan:#1", "build:#1", "checks:#1", "spec:#1", "correctness:#1", "standards:#1", "verify:#1·1",
                      "fix1:#1", "checks1:#1", "correctness1:#1", "handoff:#1"):
            self.assertIn(label, out["calls"])
        self.assertNotIn("verify:#1·2", out["calls"])                     # the pre-existing blocker skips the skeptic

    def test_prompts(self):
        out = self.run_workflow(args(["1"]))
        p = out["prompts"]
        for label, text in p.items():
            self.assertNotIn("{{", text, label)
        self.assertIn("Run `cd /w/1` before anything else", p["build:#1"])
        self.assertIn("python3 /plugin/scripts/agentloop.py wf checks /w/1 --run r1 --id 1 --files 'a.py'", p["checks:#1"])
        self.assertIn("`git diff tree-1-checks tree-1-checks1`", p["correctness1:#1"])  # re-review sees only the fix
        self.assertIn("-- fixer: fixed: guarded", p["correctness1:#1"])
        self.assertIn("`REVIEW.md` at the worktree root", p["spec:#1"])
        self.assertIn("fix every instance of that class", p["fix1:#1"])
        handoff = p["handoff:#1"]
        self.assertIn("Write `/state/runs/r1/1.handoff.json`", handoff)
        self.assertIn("python3 /plugin/scripts/agentloop.py wf handoff r1 --id 1", handoff)
        self.assertIn('"status": "ready"', handoff)
        self.assertIn("old crash", handoff)                                # pre-existing goes to the note

    def test_high_risk_gets_security_and_votes(self):
        out = self.run_workflow(args(["3"], maxFixRounds=0), scenario="risky")
        self.assertIn("security:#3", out["calls"])
        self.assertEqual(sorted(c for c in out["calls"] if c.startswith("verify")), ["verify:#3·1·v1", "verify:#3·1·v2", "verify:#3·1·v3"])
        self.assertEqual(out["result"]["needsHuman"][0]["ref"], "#3")      # 2 of 3 confirmed: it blocks

    def test_plan_only(self):
        out = self.run_workflow(args(["1", "2"], plan_only=False) | {"planOnly": True})
        self.assertEqual(out["result"]["stoppedAfter"], "plan")
        self.assertEqual({c.split(":")[0] for c in out["calls"]}, {"plan"})


if __name__ == "__main__":
    unittest.main()
