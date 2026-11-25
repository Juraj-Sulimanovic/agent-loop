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


class Repo(unittest.TestCase):
    """A clone with the file tracker and three tickets; #2 is blocked by #1. Helpers only, no tests."""

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
            "plan": f"# Plan {tid}",
            "sensitive": [], "preExisting": [], "findings": []}))
        return json.loads(self.cli("wf", "handoff", self.run_id, "--id", tid))

    def add_tickets(self, *more, **cfg):
        path = self.state / "tickets.json"
        path.write_text(json.dumps(json.loads(path.read_text()) + list(more)))
        if cfg:
            config = self.state / "config.json"
            config.write_text(json.dumps({**json.loads(config.read_text()), **cfg}))

    def skipped(self):
        return {x["id"]: x["error"] for x in json.loads((self.run_dir / "run.json").read_text())["skipped"]}

    def stack(self, tid, onto, needs=""):
        return json.loads(self.cli("wf", "stack", self.run_id, "--id", tid, "--onto", onto, *(["--needs", needs] if needs else [])))


class StackingTest(Repo):
    def test_wf_checks_lists_the_files_outside_the_plan(self):
        self.start("1")
        wt = self.worktree("1")
        for f in ("t1.py", "extra.py", "uv.lock"):
            (wt / f).write_text("x\n")
        git("-C", str(wt), "add", "-A")
        checks = lambda *more: json.loads(self.cli("wf", "checks", str(wt), "--base", "origin/main", "--files", "t1.py", *more))  # noqa: E731
        first = checks()
        self.assertEqual((first["outOfPlan"], first["scopeFiles"]), (["extra.py"], ["extra.py"]))
        (wt / "t1.py").write_text("y\n")  # a fix that leaves extra.py alone and adds new.py
        (wt / "new.py").write_text("n\n")
        git("-C", str(wt), "add", "-A")
        again = checks("--since", first["tree"])
        self.assertEqual((again["outOfPlan"], again["scopeFiles"]), (["extra.py", "new.py"], ["new.py"]))

    def test_a_ticket_blocked_by_one_in_the_run_waits_for_it(self):
        self.add_tickets({"id": 7, "name": "Seven", "status": "Todo", "blockers": [2]})  # a chain: 7 -> 2 -> 1
        args = self.start()
        by = {t["id"]: t for t in args["tickets"]}
        self.assertEqual(sorted(by), ["1", "2", "3", "7"])
        self.assertEqual((by["2"]["waitFor"], by["7"]["waitFor"]), (["1"], ["2"]))
        self.assertNotIn("waitFor", by["1"])
        self.assertTrue(args["project"]["commitAtHandoff"])

    def test_a_ticket_whose_blocker_is_skipped_stays_out_of_the_run(self):
        """#5 is blocked by #4, which no run makes; #1's setup fails. A ticket that waits on a skipped
        one, directly or down a chain, is skipped too: nothing would hold it back from the trunk."""
        self.add_tickets({"id": 4, "name": "Four", "status": "Doing"},
                         {"id": 5, "name": "Five", "status": "Todo", "blockers": [4]},
                         {"id": 6, "name": "Six", "status": "Todo", "blockers": [5]},
                         {"id": 7, "name": "Seven", "status": "Todo", "blockers": [2]},
                         setup=["case {worktree} in */1) echo no deps; exit 1;; esac"])
        p = al.require_project()
        ids, waiting = al.select_tickets(p, al.snapshot(p), [])   # a sprint: #5 and #6 do not join
        self.assertEqual((ids, waiting), (["1", "3", "2", "7"], {"2": ["1"], "7": ["2"]}))
        args = self.start("1", "2", "3", "5", "6", "7")           # asked for by id: skipped, with the reason
        self.assertEqual([t["id"] for t in args["tickets"]], ["3"])
        skipped = self.skipped()
        self.assertEqual((skipped["5"], skipped["6"]), ("waiting on #4", "waiting on #5; #5 is not in this run (waiting on #4)"))
        self.assertIn("setup failed", skipped["1"])
        self.assertTrue(skipped["2"].startswith("waiting on #1; #1 is not in this run (setup failed"), skipped["2"])
        self.assertTrue(skipped["7"].startswith("waiting on #2; #2 is not in this run (waiting on #1"), skipped["7"])
        self.assertFalse(self.worktree("6").exists() or self.worktree("7").exists())

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
        self.assertEqual((self.state / "plans" / "1.md").read_text(), "# Plan 1\n")  # the workflow's plan reaches the plans folder

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
        two = self.finish_ready("2")["commit"]
        self.assertIn("base: feat/1-one", (self.state / "handoffs" / "2.md").read_text())
        # a resumed run asks again: the ticket has built by now and stays as it is
        again = json.loads(self.cli("wf", "stack", self.run_id, "--id", "2", "--onto", "1"))
        self.assertEqual((again["ok"], again["base"]), (True, "feat/1-one"))
        self.assertEqual(git("-C", str(self.worktree("2")), "rev-parse", "HEAD"), two)

    def test_stack_guards(self):
        self.start()
        out = json.loads(self.cli("wf", "stack", self.run_id, "--id", "2", "--onto", "1"))
        self.assertIn("#1 is not committed and ready; finish it first", out["reason"])
        self.finish_ready("1")
        (self.worktree("2") / "a.py").write_text("changed\n")
        out = json.loads(self.cli("wf", "stack", self.run_id, "--id", "2", "--onto", "1"))
        self.assertIn("#2 has changes in its worktree", out["reason"])
        self.assertIn("commit or discard them", out["reason"])
        git("-C", str(self.worktree("2")), "checkout", "-q", "a.py")
        # the base moved on after #1 was cut: each reason says how to unblock the ticket
        (self.clone / "new.py").write_text("n = 1\n")
        git("-C", str(self.clone), "add", "-A")
        git("-C", str(self.clone), "commit", "-qm", "main moves on")
        git("-C", str(self.clone), "push", "-q", "origin", "main")
        out = json.loads(self.cli("wf", "stack", self.run_id, "--id", "2", "--onto", "1"))
        self.assertIn("#1's branch is 1 commit(s) behind origin/main; rebase it", out["reason"])
        git("-C", str(self.worktree("2")), "commit", "-q", "--allow-empty", "-m", "old work")
        out = json.loads(self.cli("wf", "stack", self.run_id, "--id", "2", "--onto", "1"))
        self.assertIn("#2 has commits of its own: its branch is 1 ahead of origin/main", out["reason"])
        self.assertIn("reset --hard origin/main", out["reason"])

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
        self.assertIn("relined: moved onto #3 during the sprint (clean rebase, checks passed)", note)  # but ship says it moved
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
        """The workflow says both are ready: #1's commit failed, and #3 never wrote a handoff."""
        self.start("1", "3")
        hook = Path(git("-C", str(self.clone), "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.finish_ready("1")
        (self.run_dir / "summary.json").write_text(json.dumps({"ready": [{"id": i, "ref": f"#{i}", "name": i} for i in ("1", "3")],
                                                               "needsHuman": [], "deferred": []}))
        self.cli("wf", "finish", self.run_id)
        state = json.loads((self.run_dir / "run.json").read_text())
        self.assertEqual(state["summary"]["ready"], [])
        why = {n["id"]: n["question"] for n in state["summary"]["needsHuman"]}
        self.assertIn("the handoff commit failed", why["1"])
        self.assertIn("the handoff step did not finish", why["3"])
        self.assertEqual(state["tickets"]["3"]["status"], "needs-human")


class WithoutCommitsTest(Repo):
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


if __name__ == "__main__":
    unittest.main()
