"""Stacking in a Workflow run: waiting tickets, the handoff commit and `wf stack`, on real git repos.

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


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class StackingTest(unittest.TestCase):
    def setUp(self, commits=True):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        seed, origin, self.clone = self.tmp / "seed", self.tmp / "origin.git", self.tmp / "app"
        git("init", "-q", "-b", "main", str(seed))
        (seed / "a.py").write_text("x = 0\n")
        git("-C", str(seed), "add", "-A")
        git("-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm", "i")
        git("clone", "-q", "--bare", str(seed), str(origin))
        git("clone", "-q", str(origin), str(self.clone))
        for key, value in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            git("-C", str(self.clone), "config", key, value)  # worktrees share it; the user's global signing stays out
        self.state = self.clone / ".git" / "agent-loop"
        self.state.mkdir()
        cfg = al.merged(al.DEFAULT_CONFIG, {"repo": "", "commitAtHandoff": commits,
                                            "tracker": {"type": "file", "columns": {"ready": "Todo"}, "show": ["Todo"], "done": ["Done"]}})
        (self.state / "config.json").write_text(json.dumps(cfg))
        (self.state / "tickets.json").write_text(json.dumps([
            {"id": 1, "name": "One", "status": "Todo"},
            {"id": 2, "name": "Two", "status": "Todo", "blockers": [1]},
            {"id": 3, "name": "Three", "status": "Todo"},
        ]))
        self.cwd = os.getcwd()
        os.chdir(self.clone)

    def tearDown(self):
        os.chdir(self.cwd)

    def cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            al.main(list(argv))
        return out.getvalue()

    def start(self, *ids):
        args = json.loads(self.cli("wf", "start", *ids))
        self.run_id = args["run"]
        self.run_dir = Path(args["runDir"])
        return args

    def worktree(self, tid):
        return self.tmp / "app-worktrees" / tid

    def finish_ready(self, tid, change="y = 1\n", message=None):
        """What a ticket's loop leaves behind: a staged change and a ready handoff."""
        wt = self.worktree(tid)
        (wt / f"t{tid}.py").write_text(change)
        git("-C", str(wt), "add", "-A")
        (self.run_dir / f"{tid}.handoff.json").write_text(json.dumps({
            "summary": "s", "decisions": [], "followUps": [], "openFindings": [], "commitMessage": message or f"feat: ticket {tid}",
            "prTitle": "T", "prChanges": [], "testing": "", "risk": "", "revert": "", "status": "ready", "why": "", "rounds": 0,
            "sensitive": [], "preExisting": [], "findings": []}))
        return json.loads(self.cli("wf", "handoff", self.run_id, "--id", tid))

    def test_a_ticket_blocked_by_one_in_the_run_waits_for_it(self):
        args = self.start()
        by = {t["id"]: t for t in args["tickets"]}
        self.assertEqual(sorted(by), ["1", "2", "3"])
        self.assertEqual(by["2"]["waitFor"], ["1"])
        self.assertNotIn("waitFor", by["1"])
        self.assertTrue(args["project"]["commitAtHandoff"])

    def test_the_handoff_commits_a_ready_ticket(self):
        self.start("1")
        out = self.finish_ready("1")
        self.assertEqual(out["status"], "ready")
        wt = self.worktree("1")
        self.assertEqual(out["commit"], git("-C", str(wt), "rev-parse", "HEAD"))
        self.assertEqual(git("-C", str(wt), "log", "-1", "--format=%s"), "feat: ticket 1")
        self.assertEqual(git("-C", str(wt), "status", "--porcelain", "--untracked-files=no"), "")
        note = (self.state / "handoffs" / "1.md").read_text()
        self.assertIn(f"commit: {out['commit']}", note)
        state = json.loads((self.run_dir / "run.json").read_text())["tickets"]["1"]
        self.assertEqual((state["status"], state["commit"]), ("ready", out["commit"]))

    def test_a_second_handoff_keeps_the_commit(self):
        """A resumed run replays the handoff: the ticket stays ready on the commit it has."""
        self.start("1")
        first = self.finish_ready("1")["commit"]
        again = json.loads(self.cli("wf", "handoff", self.run_id, "--id", "1"))
        self.assertEqual((again["status"], again["commit"]), ("ready", first))

    def test_a_failing_commit_hook_makes_the_ticket_need_you(self):
        self.start("1")
        hook = Path(git("-C", str(self.clone), "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'lint says no' >&2\nexit 1\n")
        hook.chmod(0o755)
        out = self.finish_ready("1")
        self.assertEqual((out["status"], out["commit"]), ("needs-human", ""))
        self.assertIn("the handoff commit failed: lint says no", (self.state / "handoffs" / "1.md").read_text())

    def test_stack_moves_a_waiting_ticket_onto_its_blockers_commit(self):
        self.start()
        commit = self.finish_ready("1")["commit"]
        out = json.loads(self.cli("wf", "stack", self.run_id, "--id", "2", "--onto", "1"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["base"], "feat/1-one")
        self.assertEqual(git("-C", str(self.worktree("2")), "rev-parse", "HEAD"), commit)
        self.assertEqual(git("-C", str(self.worktree("2")), "branch", "--show-current"), "feat/2-two")  # its own branch
        prepared = {t["id"]: t for t in json.loads((self.run_dir / "run.json").read_text())["prepared"]}
        self.assertEqual(prepared["2"]["base"], "feat/1-one")
        # its handoff then records the stacked base, which the ship skill uses as the PR base
        self.finish_ready("2")
        self.assertIn("base: feat/1-one", (self.state / "handoffs" / "2.md").read_text())

    def test_stack_guards(self):
        self.start()
        out = json.loads(self.cli("wf", "stack", self.run_id, "--id", "2", "--onto", "1"))
        self.assertEqual((out["ok"], out["reason"]), (False, "#1 is not committed and ready"))
        self.finish_ready("1")
        (self.worktree("2") / "a.py").write_text("changed\n")
        out = json.loads(self.cli("wf", "stack", self.run_id, "--id", "2", "--onto", "1"))
        self.assertEqual((out["ok"], out["reason"]), (False, "#2 has changes in its worktree"))

    def stack(self, tid, onto, needs=""):
        return json.loads(self.cli("wf", "stack", self.run_id, "--id", tid, "--onto", onto, *(["--needs", needs] if needs else [])))

    def test_stack_puts_blockers_on_separate_branches_in_a_line(self):
        self.start()
        self.finish_ready("1")
        three = self.finish_ready("3", change="z = 3\n")["commit"]
        out = self.stack("2", "1", "3")
        self.assertTrue(out["ok"], out)
        self.assertEqual((out["base"], out["resolve"]), ("feat/1-one", None))
        one = git("-C", str(self.clone), "rev-parse", "feat/1-one")
        self.assertEqual(git("-C", str(self.clone), "rev-parse", "feat/1-one^"), three)  # #1 moved onto #3
        self.assertEqual(git("-C", str(self.worktree("2")), "rev-parse", "HEAD"), one)
        note = (self.state / "handoffs" / "1.md").read_text()
        self.assertIn("base: feat/3-three", note)
        self.assertIn(f"commit: {one}", note)
        self.assertNotIn("resolved:", note)  # a clean rebase: nothing for ship to review
        state = json.loads((self.run_dir / "run.json").read_text())
        self.assertEqual(state["tickets"]["1"]["commit"], one)
        self.assertEqual({t["id"]: t["base"] for t in state["prepared"]}["1"], "feat/3-three")

    def test_stack_gives_a_conflict_to_the_resolve_agent_and_carries_on(self):
        self.start()
        (self.worktree("1") / "a.py").write_text("x = 1\n")
        self.finish_ready("1")
        (self.worktree("3") / "a.py").write_text("x = 3\n")
        self.finish_ready("3", change="z = 3\n")
        out = self.stack("2", "1", "3")
        self.assertFalse(out["ok"])
        self.assertEqual(out["resolve"]["id"], "1")
        self.assertIn("conflicts in: `a.py`", out["resolve"]["prompt"])
        self.assertIn(str(self.worktree("1")), out["resolve"]["prompt"])
        # the agent resolves and stages; the second call continues the rebase and stacks #2
        (self.worktree("1") / "a.py").write_text("x = 1\ny = 3\n")
        git("-C", str(self.worktree("1")), "add", "a.py")
        out = self.stack("2", "1", "3")
        self.assertTrue(out["ok"], out)
        self.assertEqual((self.worktree("2") / "a.py").read_text(), "x = 1\ny = 3\n")
        self.assertIn("resolved: moved onto #3 during the sprint", (self.state / "handoffs" / "1.md").read_text())

    def test_stack_gives_up_after_two_unresolved_conflicts(self):
        self.start()
        (self.worktree("1") / "a.py").write_text("x = 1\n")
        self.finish_ready("1")
        (self.worktree("3") / "a.py").write_text("x = 3\n")
        self.finish_ready("3", change="z = 3\n")
        self.assertIsNotNone(self.stack("2", "1", "3")["resolve"])
        self.assertIsNotNone(self.stack("2", "1", "3")["resolve"])
        out = self.stack("2", "1", "3")
        self.assertEqual(out["resolve"], None)
        self.assertIn("conflicts with #3 in a.py; the rebase is left in progress", out["reason"])

    def test_stack_never_moves_a_branch_another_ticket_builds_on(self):
        self.start()
        self.finish_ready("1")
        self.finish_ready("3", change="z = 3\n")
        state = json.loads((self.run_dir / "run.json").read_text())
        for t in state["prepared"]:
            if t["id"] == "3":
                t["base"] = "feat/1-one"
        (self.run_dir / "run.json").write_text(json.dumps(state))
        out = self.stack("2", "1", "3")
        self.assertFalse(out["ok"])
        self.assertIn("#1 cannot move onto #3: #3 build on it", out["reason"])
        self.assertEqual(git("-C", str(self.worktree("2")), "rev-parse", "HEAD"), git("-C", str(self.clone), "rev-parse", "origin/main"))

    def test_finish_trusts_run_json_over_the_workflow_summary(self):
        self.start("1")
        hook = Path(git("-C", str(self.clone), "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.finish_ready("1")
        (self.run_dir / "summary.json").write_text(json.dumps({"ready": [{"id": "1", "ref": "#1", "name": "One"}], "needsHuman": [], "deferred": []}))
        self.cli("wf", "finish", self.run_id)
        summary = json.loads((self.run_dir / "run.json").read_text())["summary"]
        self.assertEqual(summary["ready"], [])
        self.assertIn("the handoff commit failed", summary["needsHuman"][0]["question"])


class WithoutCommitsTest(StackingTest):
    """With commitAtHandoff off, nothing changes: no commit, blocked tickets stay out of the run."""

    def setUp(self):
        super().setUp(commits=False)

    def test_a_ticket_blocked_by_one_in_the_run_waits_for_it(self):
        args = self.start()
        self.assertEqual(sorted(t["id"] for t in args["tickets"]), ["1", "3"])

    def test_the_handoff_commits_a_ready_ticket(self):
        self.start("1")
        out = self.finish_ready("1")
        self.assertEqual((out["status"], out["commit"]), ("ready", ""))
        self.assertIn("t1.py", git("-C", str(self.worktree("1")), "diff", "--cached", "--name-only"))  # still staged

    def test_a_failing_commit_hook_makes_the_ticket_need_you(self):
        pass  # no commit, so no hook

    def test_a_second_handoff_keeps_the_commit(self):
        pass

    def test_stack_moves_a_waiting_ticket_onto_its_blockers_commit(self):
        pass

    def test_stack_guards(self):
        pass

    def test_stack_puts_blockers_on_separate_branches_in_a_line(self):
        pass

    def test_stack_gives_a_conflict_to_the_resolve_agent_and_carries_on(self):
        pass

    def test_stack_gives_up_after_two_unresolved_conflicts(self):
        pass

    def test_stack_never_moves_a_branch_another_ticket_builds_on(self):
        pass

    def test_finish_trusts_run_json_over_the_workflow_summary(self):
        pass


if __name__ == "__main__":
    unittest.main()
