"""Review that converges and scales with risk: pre-existing findings, scoped re-reviews,
the fixer's answers, majority votes on sensitive paths, the findings report, handoff sections.

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
sys.stdin = io.StringIO("")
import agentloop as al  # noqa: E402
import runner  # noqa: E402


def sh(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


class Agent:
    """A scripted fake agent: `findings[(round, lens)]` and `votes[label]` decide what it says."""

    def __init__(self, worktree: Path, findings=None, votes=None):
        self.wt, self.findings, self.votes = worktree, findings or {}, votes or {}
        self.prompts, self.calls = {}, []

    def __call__(self, *, prompt, log_path, mode, **_):
        label = Path(log_path).name.split(".")[1]
        self.prompts[label], _ = prompt, self.calls.append(label)
        if label == "plan":
            return {"feasible": True, "question": None, "summary": "s", "files": ["app.py"], "steps": [], "risks": [],
                    "planMarkdown": "p", "knowledge": []}
        if label in ("implement",) or label.startswith("fix"):
            (self.wt / "app.py").write_text(f"x = {len(self.calls)}\n")   # each build step changes the tree
            sh("git", "-C", str(self.wt), "add", "-A")
            return {"summary": "ok", "checks": [], "stuck": None,
                    "responses": [{"n": 1, "action": "disputed", "note": "the guard already covers it"}]}
        if label.startswith("review"):
            round_, lens = label[len("review"):].split("-")
            return {"findings": self.findings.get((round_ or "0", lens), [])}
        if label.startswith("verify"):
            return {"verdict": self.votes.get(label, "confirmed"), "severity": "major", "reason": "looked"}
        return {"summary": "Did it.", "decisions": [], "followUps": [], "openFindings": [], "commitMessage": "feat: x",
                "prTitle": "X", "prChanges": ["Adds x"], "testing": "make check passed", "risk": "Low; touches app.py",
                "revert": "Plain revert."}


def finding(issue, cls, introduced=True, severity="major", file="app.py", line=1):
    return {"severity": severity, "file": file, "line": line, "issue": issue, "evidence": "e", "fix": None,
            "introduced": introduced, "class": cls}


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.wt = self.tmp / "wt"
        sh("git", "init", "-q", "-b", "main", str(self.wt))
        (self.wt / "app.py").write_text("x = 0\n")
        sh("git", "-C", str(self.wt), "add", "-A")
        sh("git", "-C", str(self.wt), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")
        self.state = self.tmp / "state"
        self.state.mkdir()

    def run_loop(self, agent, **project):
        d = self.state / "runs" / "r1"
        d.mkdir(parents=True, exist_ok=True)
        proj = {"root": str(self.tmp), "dir": str(self.state), "briefPath": "", "checks": [], "exclusive": [],
                "maxParallel": 2, "maxFixRounds": 2, **project}
        t = {"id": "1", "ref": "#1", "name": "One", "url": "", "desc": "d", "worktree": str(self.wt), "branch": "main", "base": "HEAD"}
        run = runner.Run(d, proj, [t], {"default": "claude"})
        run.save()
        with redirect_stdout(io.StringIO()):
            summary = runner.Loop(run, agent, lambda w, c: []).execute()
        return summary, run

    def test_pre_existing_findings_never_block(self):
        agent = Agent(self.wt, {("0", "correctness"): [finding("Old crash in parse()", "unchecked None", introduced=False, severity="blocker")]})
        summary, _ = self.run_loop(agent)
        self.assertEqual([(r["ref"], r["rounds"], r["preExisting"]) for r in summary["ready"]], [("#1", 0, 1)])
        self.assertFalse(any(c.startswith("verify") for c in agent.calls))          # no skeptic for old code
        note = (self.state / "handoffs" / "1.md").read_text()
        self.assertIn("## Pre-existing issues\n- app.py:1: Old crash in parse() [unchecked None]", note)
        self.assertIn("Old crash in parse()", agent.prompts["handoff"])

    def test_re_review_is_scoped_and_carries_the_fixers_answers(self):
        agent = Agent(self.wt, {("0", "correctness"): [finding("Blank name accepted", "missing ValueError")]})
        summary, run = self.run_loop(agent)
        self.assertEqual(summary["ready"][0]["rounds"], 1)
        trees = run.state()["tickets"]["1"]["trees"]
        prompt = agent.prompts["review1-correctness"]
        self.assertIn(f"git diff {trees['implement']} {trees['fix1']}", prompt)
        self.assertIn("1. [major, missing ValueError] app.py:1: Blank name accepted -- fixer: disputed: the guard already covers it", prompt)
        self.assertIn("Report nothing else", prompt)
        self.assertIn("fix every instance of that class", agent.prompts["fix1"])
        self.assertNotIn("Scope: re-review", agent.prompts["review-correctness"])
        self.assertEqual((run.dir / "1.review1-correctness.prompt.md").read_text(), prompt)   # saved for debugging

    def test_same_class_surviving_a_fix_stops_early(self):
        same = [finding("Blank name accepted", "missing ValueError")]
        agent = Agent(self.wt, {("0", "correctness"): same, ("1", "correctness"): [dict(same[0], line=2)]})
        summary, _ = self.run_loop(agent)
        self.assertIn("the same issue survived a fix (missing ValueError in app.py)", summary["needsHuman"][0]["question"])
        self.assertNotIn("fix2", agent.calls)

    def test_sensitive_paths_add_security_review_and_votes(self):
        bug = [finding("Token logged", "secret in logs")]
        agent = Agent(self.wt, {("0", "security"): bug}, votes={"verify-1-v3": "refuted"})
        summary, run = self.run_loop(agent, sensitivePaths=["app.py"], maxFixRounds=0)
        self.assertIn("review-security", agent.calls)
        self.assertEqual(sorted(c for c in agent.calls if c.startswith("verify")), ["verify-1-v1", "verify-1-v2", "verify-1-v3"])
        self.assertEqual(summary["needsHuman"][0]["ref"], "#1")                  # 2 of 3 confirmed: it blocks
        r0 = json.loads((run.dir / "1.round0.json").read_text())
        self.assertEqual((r0["findings"][0]["votes"], r0["sensitive"]), ("2/3", ["app.py"]))
        note = (self.state / "handoffs" / "1.md").read_text()
        self.assertIn("risk: high\nsensitive: app.py", note)
        self.assertIn("HIGH RISK", runner.status_text(run.state()))

    def test_majority_refutes(self):
        agent = Agent(self.wt, {("0", "security"): [finding("Token logged", "secret in logs")]},
                      votes={"verify-1-v1": "refuted", "verify-1-v2": "refuted"})
        summary, _ = self.run_loop(agent, sensitivePaths=["app.py"], maxFixRounds=0)
        self.assertEqual(summary["ready"][0]["risk"], "high")

    def test_normal_risk_keeps_three_reviewers_and_one_skeptic(self):
        agent = Agent(self.wt, {("0", "spec"): [finding("Missing criterion", "unmet criterion")]})
        self.run_loop(agent, sensitivePaths=["deploy/**"])
        self.assertNotIn("review-security", agent.calls)
        self.assertIn("verify-1", agent.calls)

    def test_handoff_has_testing_risk_and_revert(self):
        self.run_loop(Agent(self.wt))
        note = (self.state / "handoffs" / "1.md").read_text()
        for part in ("## Testing\nmake check passed", "## Risk\nLow; touches app.py", "## How to revert\nPlain revert.", "risk: normal"):
            self.assertIn(part, note)

    def test_review_file_reaches_reviewers_and_skeptics(self):
        (self.wt / "REVIEW.md").write_text("Do not flag naming.\n")
        agent = Agent(self.wt, {("0", "spec"): [finding("Missing criterion", "unmet criterion")]})
        self.run_loop(agent)
        self.assertIn("`REVIEW.md` at the worktree root is the code owners' guidance", agent.prompts["review-spec"])
        self.assertIn("`REVIEW.md` at the worktree root", agent.prompts["verify-1"])


class FindingsReportTest(unittest.TestCase):
    def test_recurring_classes_across_runs(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        repo = tmp / "app"
        sh("git", "init", "-q", "-b", "main", str(repo))
        state = repo / ".git" / "agent-loop"
        (state / "runs" / "r1").mkdir(parents=True)
        (state / "runs" / "r2").mkdir()
        cfg = al.merged(al.DEFAULT_CONFIG, {"tracker": {"type": "file"}})
        (state / "config.json").write_text(json.dumps(cfg))
        hit = dict(finding("Blank name accepted", "Missing ValueError"), lens="spec")
        (state / "runs" / "r1" / "1.round0.json").write_text(json.dumps({"findings": [hit], "preExisting": []}))
        (state / "runs" / "r2" / "4.round0.json").write_text(json.dumps({"findings": [dict(hit, file="b.py")], "preExisting": []}))
        (state / "runs" / "r2" / "5.round0.json").write_text(json.dumps({"findings": [dict(hit, issue="Typo", **{"class": "naming"})], "preExisting": []}))
        cwd = os.getcwd()
        os.chdir(repo)
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                al.main(["findings"])
        finally:
            os.chdir(cwd)
        text = out.getvalue()
        self.assertIn("- missing valueerror: 2 tickets (confirmed; spec)", text)
        self.assertIn("files: app.py, b.py", text)
        self.assertIn("the ticket template", text)
        self.assertNotIn("naming", text)                                        # seen once: not recurring


if __name__ == "__main__":
    unittest.main()
