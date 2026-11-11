"""The ship skill's PR stacks: `agentloop.py stack plan|build` on real git repos, with a fake agent.

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_stacking import StackingTest, git  # noqa: E402

import stacking  # noqa: E402


class Agent:
    """A scripted agent: `resolve(prompt, cwd)` plays the resolve agent; reviewers find nothing."""

    def __init__(self, resolve=None, findings=None):
        self.resolve, self.findings, self.calls = resolve, findings or [], []

    def __call__(self, *, tool, prompt, schema, cwd, mode, **_):
        self.calls.append(mode)
        if mode == "write":
            if self.resolve is None:
                return {"summary": "", "checks": [], "stuck": "no idea", "responses": []}
            self.resolve(prompt, Path(cwd))
            return {"summary": "combined both", "checks": [], "stuck": None, "responses": []}
        if "verdict" in json.dumps(schema):
            return {"verdict": "confirmed", "severity": "major", "reason": "yes"}
        return {"findings": list(self.findings)}


class ShipStackTest(unittest.TestCase):
    setUp_ = StackingTest.setUp
    tearDown = StackingTest.tearDown
    cli = StackingTest.cli
    start = StackingTest.start
    worktree = StackingTest.worktree
    finish_ready = StackingTest.finish_ready

    def setUp(self):
        self.setUp_()
        self.configure(stack="sprint")
        self.patch = mock.patch.object(stacking, "gh_stack_installed", lambda: True)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def configure(self, **pr):
        cfg = json.loads((self.state / "config.json").read_text())
        cfg["pr"].update(pr)
        cfg.update({k: v for k, v in pr.items() if k == "checks"})
        cfg["pr"].pop("checks", None)
        (self.state / "config.json").write_text(json.dumps(cfg))

    def finished(self, order):
        """Ready tickets 1, 2 (stacked on 1) and 3, finished in `order`."""
        self.start()
        self.finish_ready("1")
        self.cli("wf", "stack", self.run_id, "--id", "2", "--onto", "1")
        self.finish_ready("2", change="two = 2\n")
        self.finish_ready("3", change="three = 3\n")
        for n, tid in enumerate(order):
            stacking.set_meta(self.state / "handoffs" / f"{tid}.md", "updated", f"2026-01-01T00:00:0{n}")

    def stack(self, action, agent=None, *args):
        with mock.patch("adapters.run_agent", agent or Agent()):
            return json.loads(self.cli("stack", action, *args))

    def ids(self, out):
        return [[t["id"] for t in s["tickets"]] for s in out["stacks"]]

    def test_one_stack_per_sprint_keeps_chains_and_finish_order(self):
        self.finished(["3", "1", "2"])
        out = self.stack("plan")
        self.assertEqual(self.ids(out), [["3", "1", "2"]])
        self.assertEqual([t["parent"] for t in out["stacks"][0]["tickets"]], ["main", "feat/3-three", "feat/1-one"])

    def test_build_puts_every_ticket_on_the_one_below(self):
        self.finished(["1", "2", "3"])
        out = self.stack("build")
        s = out["stacks"][0]
        self.assertEqual([t["status"] for t in s["tickets"]], ["built"] * 3)
        wt3 = self.worktree("3")
        self.assertTrue(git("-C", str(wt3), "merge-base", "--is-ancestor", "feat/2-two", "HEAD") == "")
        self.assertEqual(git("-C", str(wt3), "rev-parse", "HEAD^"), git("-C", str(self.clone), "rev-parse", "feat/2-two"))
        self.assertIn("base: feat/2-two", (self.state / "handoffs" / "3.md").read_text())
        self.assertIn(f"commit: {git('-C', str(wt3), 'rev-parse', 'HEAD')}", (self.state / "handoffs" / "3.md").read_text())
        self.assertTrue(s["commands"][-1].endswith("gh stack link feat/1-one feat/2-two feat/3-three"), s["commands"][-1])
        self.assertIn("--base feat/2-two", next(c for c in s["commands"] if "--head feat/3-three" in c))
        # again: nothing moves, nothing runs
        before = git("-C", str(wt3), "rev-parse", "HEAD")
        agent = Agent()
        again = self.stack("build", agent)
        self.assertEqual([t["status"] for t in again["stacks"][0]["tickets"]], ["built"] * 3)
        self.assertEqual(git("-C", str(wt3), "rev-parse", "HEAD"), before)
        self.assertEqual(agent.calls, [])

    def test_a_ticket_an_agent_combined_during_the_sprint_is_reviewed(self):
        self.finished(["1", "2", "3"])
        self.stack("build")
        note = self.state / "handoffs" / "1.md"
        stacking.set_meta(note, "resolved", "moved onto #3 during the sprint, and an agent combined the two")
        agent = Agent()
        out = self.stack("build", agent)
        self.assertEqual([t["status"] for t in out["stacks"][0]["tickets"]], ["built"] * 3)
        self.assertEqual(agent.calls, ["check"])  # one combine review of #1 only
        self.assertNotIn("resolved:", note.read_text())

    def test_chains_mode_leaves_independent_tickets_on_the_trunk(self):
        self.configure(stack="chains")
        self.finished(["1", "2", "3"])
        out = self.stack("build")
        self.assertEqual(self.ids(out), [["1", "2", "3"]])
        self.assertEqual([t["parent"] for t in out["stacks"][0]["tickets"]], ["main", "feat/1-one", "main"])
        self.assertFalse(out["link"])
        self.assertNotIn("gh stack link", "\n".join(out["stacks"][0]["commands"]))

    def test_staged_tickets_are_committed_with_their_message(self):
        self.tearDown()
        StackingTest.setUp(self, commits=False)
        self.configure(stack="sprint")
        self.start()
        self.finish_ready("1")
        self.finish_ready("3", change="three = 3\n")
        out = self.stack("build")
        self.assertEqual([t["status"] for t in out["stacks"][0]["tickets"]], ["built", "built"])
        self.assertEqual(git("-C", str(self.worktree("3")), "log", "-2", "--format=%s"), "feat: ticket 3\nfeat: ticket 1")

    def conflicting(self):
        """Tickets 1 and 3 both change a.py, each in its one handoff commit; 3 finishes last, so it
        goes on top of 1."""
        self.start("1", "3")
        (self.worktree("1") / "a.py").write_text("x = 1\n")
        self.finish_ready("1")
        (self.worktree("3") / "a.py").write_text("x = 3\n")
        self.finish_ready("3", change="three = 3\n")
        stacking.set_meta(self.state / "handoffs" / "3.md", "updated", "2099-01-01T00:00:00")

    def resolve_both(self, prompt, wt):
        self.assertIn("conflicts in: `a.py`", prompt)
        (wt / "a.py").write_text("x = 1\ny = 3\n")
        git("-C", str(wt), "add", "a.py")

    def test_a_conflict_goes_to_one_resolve_agent_then_one_combine_review(self):
        self.conflicting()
        agent = Agent(self.resolve_both)
        out = self.stack("build", agent)
        t3 = out["stacks"][0]["tickets"][1]
        self.assertEqual(t3["status"], "built", t3)
        self.assertEqual(t3["resolved"], ["resolved the conflicts in a.py with feat/1-one"])
        self.assertEqual(agent.calls, ["write", "check"])  # the resolve agent, then the one combine review
        wt = self.worktree("3")
        self.assertEqual((wt / "a.py").read_text(), "x = 1\ny = 3\n")
        self.assertEqual(git("-C", str(wt), "rev-parse", "HEAD^"), git("-C", str(self.clone), "rev-parse", "feat/1-one"))
        self.assertEqual(git("-C", str(wt), "log", "-1", "--format=%s"), "feat: ticket 3")
        self.assertNotIn("resolved:", (self.state / "handoffs" / "3.md").read_text())

    def test_an_unresolved_conflict_aborts_the_rebase_and_the_rerun_starts_over(self):
        self.conflicting()
        wt = self.worktree("3")
        before = git("-C", str(wt), "rev-parse", "HEAD")
        out = self.stack("build", Agent())  # the agent is stuck
        s = out["stacks"][0]
        self.assertEqual([t["status"] for t in s["tickets"]], ["built", "stopped"])
        self.assertIn("The rebase was aborted", s["tickets"][1]["reason"])
        self.assertEqual(s["tickets"][1]["conflicts"], ["a.py"])
        self.assertFalse(stacking.rebasing(wt))
        self.assertEqual(git("-C", str(wt), "rev-parse", "HEAD"), before)  # as it was before this ship
        self.assertEqual(s["stoppedAt"], "3")
        self.assertEqual(s["commands"], [])  # nothing is published for a stopped stack
        # the developer rebases by hand; the next ship runs from the start and checks the combine
        stacking.git(wt, "rebase", "--onto", "feat/1-one", "HEAD^")  # stops on the conflict
        (wt / "a.py").write_text("x = 1\n")
        git("-C", str(wt), "add", "a.py")
        git("-C", str(wt), "-c", "core.editor=true", "rebase", "--continue")
        again = self.stack("build", Agent())
        self.assertEqual([t["status"] for t in again["stacks"][0]["tickets"]], ["built", "built"])
        self.assertEqual(git("-C", str(wt), "rev-parse", "HEAD^"), git("-C", str(self.clone), "rev-parse", "feat/1-one"))

    FINDING = {"severity": "major", "file": "a.py", "line": 2, "issue": "lost y = 3",
               "evidence": "the combine kept only x = 1", "fix": None, "introduced": True, "class": "combine"}

    def test_combine_findings_are_shown_and_do_not_stop_the_stack(self):
        self.conflicting()
        agent = Agent(self.resolve_both, findings=[self.FINDING])
        out = self.stack("build", agent)
        s = out["stacks"][0]
        t3 = s["tickets"][1]
        self.assertEqual(t3["status"], "built", t3)
        self.assertEqual(t3["findings"], [self.FINDING])
        self.assertTrue(s["commands"])
        self.assertIsNone(s["stoppedAt"])
        self.assertNotIn("resolved:", (self.state / "handoffs" / "3.md").read_text())

    def test_combine_findings_persist_until_the_commit_changes(self):
        self.conflicting()
        self.stack("build", Agent(self.resolve_both, findings=[self.FINDING]))
        agent = Agent()
        again = self.stack("build", agent)
        t3 = again["stacks"][0]["tickets"][1]
        self.assertEqual(t3["status"], "built")
        self.assertEqual(t3["findings"], [self.FINDING])
        self.assertEqual(agent.calls, [])
        wt = self.worktree("3")
        (wt / "a.py").write_text("x = 1\ny = 3\nz = 1\n")
        git("-C", str(wt), "add", "a.py")
        amended = self.stack("build", Agent())
        t3 = amended["stacks"][0]["tickets"][1]
        self.assertEqual(t3["status"], "built")
        self.assertEqual(t3["findings"], [])
        self.assertNotIn("combineFindings:", (self.state / "handoffs" / "3.md").read_text())

    def test_the_tickets_below_a_stop_publish_when_built_on_their_own(self):
        self.conflicting()
        self.stack("build", Agent())
        out = self.stack("build", Agent(), "1")
        s = out["stacks"][0]
        self.assertEqual([(t["id"], t["status"]) for t in s["tickets"]], [("1", "built")])
        self.assertIsNone(s["stoppedAt"])
        self.assertTrue(s["commands"])
        self.assertTrue(any("feat/1-one" in c for c in s["commands"]))
        self.assertFalse(any("feat/3-three" in c for c in s["commands"]))


class PlanTest(unittest.TestCase):
    """Which stack the tickets join: the trunk, the user's own open stack, or someone else's branch."""

    def t(self, tid, base, updated):
        return {"id": tid, "branch": f"feat/{tid}", "base": base, "updated": updated, "worktree": "/x"}

    PRS = [
        {"number": 10, "headRefName": "feat/a", "baseRefName": "main", "author": {"login": "me"}},
        {"number": 11, "headRefName": "feat/b", "baseRefName": "feat/a", "author": {"login": "me"}},
        {"number": 12, "headRefName": "feat/c", "baseRefName": "feat/b", "author": {"login": "me"}},
        {"number": 20, "headRefName": "feat/theirs", "baseRefName": "main", "author": {"login": "them"}},
    ]

    def test_building_on_the_middle_of_my_open_stack_goes_on_its_top(self):
        stacks = stacking.plan([self.t("1", "feat/b", "1"), self.t("2", "main", "2")], "main", "sprint", self.PRS, "me", True)
        self.assertEqual(len(stacks), 1)
        s = stacks[0]
        self.assertEqual(s["anchor"], {"kind": "own", "branch": "feat/c", "prs": [10, 11, 12]})
        self.assertEqual([(t["id"], t["parent"]) for t in s["tickets"]], [("1", "feat/c"), ("2", "feat/1")])
        self.assertTrue(s["link"])

    def test_no_connection_to_an_open_stack_is_a_new_stack(self):
        stacks = stacking.plan([self.t("1", "main", "1")], "main", "sprint", self.PRS, "me", True)
        self.assertEqual(stacks[0]["anchor"]["kind"], "trunk")
        self.assertFalse(stacks[0]["link"])  # a single PR is not a stack

    def test_someone_elses_branch_is_only_the_base(self):
        stacks = stacking.plan([self.t("1", "feat/theirs", "1"), self.t("2", "main", "2")], "main", "sprint", self.PRS, "me", True)
        kinds = {s["anchor"]["kind"]: s for s in stacks}
        self.assertEqual(set(kinds), {"foreign", "trunk"})
        foreign = kinds["foreign"]
        self.assertEqual([(t["parent"], t["rebase"]) for t in foreign["tickets"]], [("feat/theirs", False)])
        self.assertFalse(foreign["link"])

    def test_a_merged_base_goes_onto_the_trunk(self):
        stacks = stacking.plan([self.t("1", "feat/gone", "1")], "main", "sprint", self.PRS, "me", True)
        self.assertEqual((stacks[0]["anchor"]["kind"], stacks[0]["tickets"][0]["parent"]), ("trunk", "main"))


if __name__ == "__main__":
    unittest.main()
