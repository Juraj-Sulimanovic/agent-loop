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
from test_stacking import Repo, git  # noqa: E402  (Repo has no tests: importing it does not run test_stacking twice)

import agentloop as al  # noqa: E402
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
    setUp_ = Repo.setUp
    tearDown = Repo.tearDown
    cli = Repo.cli
    start = Repo.start
    worktree = Repo.worktree
    finish_ready = Repo.finish_ready

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
        Repo.setUp(self, commits=False)
        self.configure(stack="sprint")
        self.start()
        self.finish_ready("1")
        self.finish_ready("3", change="three = 3\n")
        out = self.stack("build")
        self.assertEqual([t["status"] for t in out["stacks"][0]["tickets"]], ["built", "built"])
        self.assertEqual(git("-C", str(self.worktree("3")), "log", "-2", "--format=%s"), "feat: ticket 3\nfeat: ticket 1")

    def test_a_ticket_with_nothing_to_ship_stops_its_stack(self):
        self.tearDown()
        Repo.setUp(self, commits=False)
        self.configure(stack="sprint")
        self.start("1", "3")
        self.finish_ready("1")
        self.finish_ready("3", change="three = 3\n")
        stacking.set_meta(self.state / "handoffs" / "3.md", "updated", "2099-01-01T00:00:00")
        git("-C", str(self.worktree("1")), "reset", "-q")  # someone unstaged #1: no commit, nothing staged
        out = self.stack("build")
        s = out["stacks"][0]
        self.assertEqual([(t["id"], t["status"]) for t in s["tickets"]], [("1", "stopped"), ("3", "waiting")])
        self.assertIn("nothing to ship", s["tickets"][0]["reason"])
        self.assertEqual(s["commands"], [])

    def test_plan_counts_the_commits_of_a_ticket_rebased_by_hand(self):
        self.start("1", "3")
        self.finish_ready("1")
        self.finish_ready("3", change="three = 3\n")
        stacking.set_meta(self.state / "handoffs" / "3.md", "updated", "2099-01-01T00:00:00")
        wt = str(self.worktree("3"))
        git("-C", wt, "rebase", "-q", "--onto", "feat/1-one", "origin/main")  # its handoff commit is gone
        Path(wt, "more.py").write_text("m = 1\n")
        git("-C", wt, "add", "more.py")
        git("-C", wt, "commit", "-qm", "fix: more")
        out = self.stack("plan")
        self.assertEqual([(x["id"], x["commits"]) for x in out["squash"]], [("3", 2)])

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


    # A handoff's `base:` is what the loop wrote; the branch may sit somewhere else by now.

    def rebased_by_hand(self):
        """#3 was finished on the trunk, then the developer rebased it onto #1's branch."""
        self.start("1", "3")
        self.finish_ready("1")
        self.finish_ready("3", change="three = 3\n")
        git("-C", str(self.worktree("3")), "rebase", "-q", "feat/1-one")
        self.assertIn("base: main", (self.state / "handoffs" / "3.md").read_text())

    def test_a_ticket_rebased_onto_another_ticket_ships_on_it(self):
        self.configure(stack="chains")
        self.rebased_by_hand()
        note = self.state / "handoffs" / "3.md"
        out = self.stack("plan")
        self.assertEqual(out["warnings"], ["#3: its handoff says base `main`, but its branch sits on `feat/1-one`; ship uses `feat/1-one`"])
        self.assertEqual([(t["id"], t["parent"]) for t in out["stacks"][0]["tickets"]], [("1", "main"), ("3", "feat/1-one")])
        self.assertIn("base: main", note.read_text())  # plan changes nothing
        built = self.stack("build")
        self.assertEqual([t["status"] for t in built["stacks"][0]["tickets"]], ["built", "built"])
        self.assertIn("base: feat/1-one", note.read_text())
        self.assertIn("--base feat/1-one", next(c for c in built["stacks"][0]["commands"] if "--head feat/3-three" in c))
        self.assertEqual(self.stack("plan")["warnings"], [])  # the note follows git now

    def on_my_open_pr(self):
        """#1 is shipped: its branch is pushed, and it is my open PR #81. #3 sits on it, rebased by hand."""
        self.rebased_by_hand()
        git("-C", str(self.worktree("1")), "push", "-q", "origin", "feat/1-one")
        stacking.set_meta(self.state / "handoffs" / "1.md", "status", "shipped")
        cfg = json.loads((self.state / "config.json").read_text())
        (self.state / "config.json").write_text(json.dumps({**cfg, "repo": "o/app"}))
        return [{"number": 81, "headRefName": "feat/1-one", "baseRefName": "main", "author": {"login": "me"}, "url": "u"}]

    def ship(self, prs, *args):
        with mock.patch.object(al, "gh_json", lambda *a: prs), mock.patch.object(al, "gh", lambda *a: "me\n"):
            return self.stack("build", None, *args)

    def test_a_ticket_whose_pr_is_open_ships_again_onto_the_pr_below_it(self):
        """Run again for review feedback, the ticket is ready while its PR is open. Its own PR is
        the top of my open stack: the ticket must not go on top of itself."""
        prs = self.on_my_open_pr()
        prs.append({"number": 96, "headRefName": "feat/3-three", "baseRefName": "feat/1-one", "author": {"login": "me"}, "url": "u"})
        [s] = self.ship(prs)["stacks"]
        self.assertEqual(s["anchor"], {"kind": "own", "branch": "feat/1-one", "prs": [81]})
        self.assertEqual([(t["id"], t["status"], t["parent"]) for t in s["tickets"]], [("3", "built", "feat/1-one")])
        self.assertIn("gh pr edit 96 -R o/app --base feat/1-one", s["commands"])
        self.assertTrue(s["commands"][-1].endswith("gh stack link 81 feat/3-three"), s["commands"][-1])

    def test_a_ticket_on_local_changes_of_an_open_pr_waits_for_their_push(self):
        """I amended the branch of my open PR and put the ticket on it, without pushing. Ship must not
        move the ticket back onto the pushed copy, nor publish it on top of commits GitHub lacks."""
        prs = self.on_my_open_pr()
        wt1, wt3 = self.worktree("1"), self.worktree("3")
        git("-C", str(wt1), "commit", "-q", "--amend", "-m", "feat: ticket 1, after review")
        git("-C", str(wt3), "rebase", "-q", "--onto", "feat/1-one", "HEAD^")
        before = git("-C", str(wt3), "rev-parse", "HEAD")
        [s] = self.ship(prs)["stacks"]
        t3 = s["tickets"][0]
        self.assertEqual(t3["status"], "stopped", t3)
        self.assertIn("`feat/1-one` has 1 local commit(s) that are not on origin", t3["reason"])
        self.assertIn(f"git -C {wt3} push --force-with-lease origin feat/1-one", t3["reason"])
        self.assertEqual(git("-C", str(wt3), "rev-parse", "HEAD"), before)
        self.assertEqual(s["commands"], [])
        git("-C", str(wt1), "push", "-q", "--force", "origin", "feat/1-one")  # pushed: the ticket ships where it is
        [s] = self.ship(prs)["stacks"]
        self.assertEqual((s["tickets"][0]["status"], s["tickets"][0]["parent"]), ("built", "feat/1-one"))
        self.assertEqual(git("-C", str(wt3), "rev-parse", "HEAD"), before)

    def test_a_ticket_rebased_onto_my_open_pr_joins_that_stack(self):
        """The ticket's handoff says the trunk; its branch is one commit on top of an open PR's branch.
        It must not count that PR's commits as its own and ask to squash them."""
        prs = self.on_my_open_pr()
        wt = self.worktree("3")
        before = git("-C", str(wt), "rev-parse", "HEAD")
        out = self.ship(prs)
        [s] = out["stacks"]
        self.assertEqual(s["anchor"], {"kind": "own", "branch": "feat/1-one", "prs": [81]})
        self.assertEqual(out["squash"], [])
        self.assertEqual([(t["id"], t["status"], t["parent"]) for t in s["tickets"]], [("3", "built", "feat/1-one")])
        self.assertIn("sits on `feat/1-one` (PR #81)", out["warnings"][0])
        self.assertEqual(git("-C", str(wt), "rev-parse", "HEAD"), before)  # already there: nothing moved
        self.assertIn("--base feat/1-one", "\n".join(s["commands"]))

    def test_a_branch_ship_does_not_know_is_never_squashed_into_the_ticket(self):
        self.start("3")
        self.finish_ready("3", change="three = 3\n")
        git("-C", str(self.clone), "checkout", "-q", "-b", "spike")
        (self.clone / "spike.py").write_text("s = 1\n")
        git("-C", str(self.clone), "add", "spike.py")
        git("-C", str(self.clone), "commit", "-qm", "spike")
        git("-C", str(self.clone), "checkout", "-q", "main")
        wt = self.worktree("3")
        git("-C", str(wt), "rebase", "-q", "spike")
        before = git("-C", str(wt), "rev-parse", "HEAD")
        out = self.stack("build", None, "--squash")
        t3 = out["stacks"][0]["tickets"][0]
        self.assertEqual(t3["status"], "stopped")
        self.assertIn("include the branch `spike`", t3["reason"])
        self.assertIn(f"rebase --onto origin/main spike", t3["reason"])
        self.assertEqual(git("-C", str(wt), "rev-parse", "HEAD"), before)
        self.assertEqual(git("-C", str(wt), "log", "-2", "--format=%s"), "feat: ticket 3\nspike")

    def test_a_ticket_stays_on_a_branch_that_changed_under_it(self):
        """#2 was built on #1; #1's commit was amended since. #2 still belongs on #1, not on the trunk."""
        self.finished(["1", "2", "3"])
        self.stack("build")
        git("-C", str(self.worktree("1")), "commit", "-q", "--amend", "-m", "feat: ticket 1, reworded")
        out = self.stack("plan")
        self.assertEqual(out["warnings"], [])
        self.assertEqual([t["parent"] for t in out["stacks"][0]["tickets"]], ["main", "feat/1-one", "feat/2-two"])

    def test_the_risk_paths_are_what_the_ticket_changes_on_its_parent(self):
        cfg = json.loads((self.state / "config.json").read_text())
        (self.state / "config.json").write_text(json.dumps({**cfg, "sensitivePaths": ["secrets/"]}))
        self.start("1", "3")
        (self.worktree("1") / "secrets").mkdir()
        (self.worktree("1") / "secrets" / "k.py").write_text("k = 1\n")
        self.finish_ready("1")
        self.finish_ready("3", change="three = 3\n")
        one, three = self.state / "handoffs" / "1.md", self.state / "handoffs" / "3.md"
        stacking.set_meta(three, "updated", "2099-01-01T00:00:00")
        stacking.set_meta(three, "risk", "high")               # what a diff against the wrong base found:
        stacking.set_meta(three, "sensitive", "secrets/k.py")  # #1's file
        out = self.stack("build")
        t1, t3 = out["stacks"][0]["tickets"]
        self.assertEqual((t1["risk"], t1["sensitive"]), ("high", "secrets/k.py"))
        self.assertEqual((t3["risk"], t3.get("sensitive")), ("normal", None))
        self.assertIn("risk: high\n", one.read_text())
        self.assertIn("sensitive: secrets/k.py\n", one.read_text())
        self.assertIn("risk: normal", three.read_text())
        self.assertNotIn("sensitive:", three.read_text())


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

    def test_a_tickets_own_open_pr_is_not_part_of_the_stack_below_it(self):
        mine = {"number": 13, "headRefName": "feat/1", "baseRefName": "feat/c", "author": {"login": "me"}}
        [s] = stacking.plan([self.t("1", "feat/c", "1")], "main", "sprint", self.PRS + [mine], "me", True)
        self.assertEqual(s["anchor"], {"kind": "own", "branch": "feat/c", "prs": [10, 11, 12]})
        self.assertEqual(s["tickets"][0]["parent"], "feat/c")

    def test_a_merged_base_goes_onto_the_trunk(self):
        stacks = stacking.plan([self.t("1", "feat/gone", "1")], "main", "sprint", self.PRS, "me", True)
        self.assertEqual((stacks[0]["anchor"]["kind"], stacks[0]["tickets"][0]["parent"]), ("trunk", "main"))


if __name__ == "__main__":
    unittest.main()
