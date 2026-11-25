"""The sprint loop against a fake adapter: batching, the fix loop, resume, the CLI end to end, and
stacking during the sprint on real git repos.

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
sys.stdin = io.StringIO("")  # hooks read stdin; never block on the runner's
import agentloop as al  # noqa: E402
import runner  # noqa: E402

FILES = {"1": ["a.py"], "2": ["a.py", "b.py"], "3": ["alembic/versions/x.py"], "4": ["alembic/versions/y.py"],
         "5": ["c.py"], "6": ["q.py"], "7": ["d.py"]}


class FakeAdapter:
    """Ticket 6 is infeasible, #1 passes after one fix, #5 never passes; everything else passes first time.

    The correctness reviewer raises the blocking findings; the skeptic confirms them unless `refute` is set.
    """

    def __init__(self, fail=False, refute=False):
        self.calls, self.fail, self.refute = [], fail, refute

    def __call__(self, *, tool, prompt, schema, cwd, mode, log_path, **_):
        tid, label = Path(log_path).name.split(".")[:2]
        self.calls.append((tid, label, tool, mode))
        if self.fail:
            raise RuntimeError("adapter should not be called")
        if label == "plan":
            if tid == "6":
                return {"feasible": False, "question": "which tenant?", "summary": "", "files": [], "steps": [], "risks": [],
                        "planMarkdown": "", "knowledge": []}
            return {"feasible": True, "question": None, "summary": "s", "files": FILES[tid], "steps": [], "risks": [],
                    "planMarkdown": f"# Plan for {tid}\n", "knowledge": []}
        if label == "implement" or label.startswith("fix"):
            return {"summary": "ok", "checks": [{"cmd": "make lint", "ok": True}], "stuck": None}
        if label.startswith("review"):
            round_, lens = label[len("review"):].split("-")
            bad = tid == "5" or (tid == "1" and round_ == "")
            if lens == "correctness" and bad:
                return {"findings": [{"severity": "major", "file": "a.py", "line": 3, "issue": "x", "evidence": "e", "fix": None}]}
            if lens == "standards":
                return {"findings": [{"severity": "minor", "file": "a.py", "line": 9, "issue": "Mysterious Name", "evidence": "e", "fix": None}]}
            return {"findings": []}
        if label.startswith("verify"):
            return {"verdict": "refuted" if self.refute else "confirmed", "severity": "major", "reason": "checked"}
        if label == "handoff":
            return {"summary": f"Did {tid}.", "decisions": ["Keep it simple."], "followUps": [], "openFindings": [],
                    "commitMessage": f"feat: ticket {tid}", "prTitle": f"Ticket {tid}", "prChanges": ["Adds it"]}
        raise AssertionError(label)


def ticket(tid):
    return {"id": tid, "ref": f"#{tid}", "name": f"Ticket {tid}", "url": f"u/{tid}", "desc": "d",
            "worktree": f"/w/{tid}", "branch": f"feat/{tid}-x", "base": "origin/main"}


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project = {"root": str(self.tmp), "dir": str(self.tmp), "briefPath": str(self.tmp / "brief.md"),
                        "checks": ["make lint"], "exclusive": ["alembic/versions/"], "maxParallel": 10, "maxFixRounds": 2}

    def make_run(self, ids, plan_only=False, agents=None, **project):
        d = self.tmp / "runs" / "r1"
        d.mkdir(parents=True, exist_ok=True)
        run = runner.Run(d, {**self.project, **project}, [ticket(i) for i in ids],
                         agents or {"default": "codex", "review": "claude"}, plan_only=plan_only)
        run.save()
        return run

    def execute(self, run, adapter, checker=lambda worktree, checks: []):
        with redirect_stdout(io.StringIO()):
            return runner.Loop(run, adapter, checker).execute()

    def test_full_run(self):
        fake = FakeAdapter()
        summary = self.execute(self.make_run(["1", "2", "3", "4", "5", "6"]), fake)
        self.assertEqual([(r["ref"], r["rounds"]) for r in summary["ready"]], [("#1", 1), ("#3", 0)])
        self.assertEqual(summary["ready"][0]["commitMessage"], "feat: ticket 1")
        self.assertEqual([(n["ref"], n["question"]) for n in summary["needsHuman"]],
                         [("#6", "which tenant?"), ("#5", "the same issue survived a fix (x in a.py); rethink the approach")])
        self.assertEqual([(d["ref"], d["reason"]) for d in summary["deferred"]],
                         [("#2", "overlaps #1 on a.py"), ("#4", "overlaps #3 on alembic/versions/")])
        # per-stage tools, and read-only stages flagged
        self.assertIn(("1", "review-spec", "claude", "check"), fake.calls)
        self.assertIn(("1", "verify-1", "claude", "check"), fake.calls)          # the skeptic runs with the reviewer's tool
        self.assertIn(("1", "review1-correctness", "claude", "check"), fake.calls)
        self.assertIn(("1", "plan", "codex", "read"), fake.calls)
        self.assertIn(("1", "implement", "codex", "write"), fake.calls)
        self.assertIn(("1", "fix1", "codex", "write"), fake.calls)
        # the runner, not the agents, writes plans and handoffs
        self.assertEqual((self.tmp / "plans" / "1.md").read_text(), "# Plan for 1\n")
        note = al.read_handoff(self.tmp / "handoffs" / "1.md")
        self.assertEqual((note["status"], al.section(note["body"], "Commit message")), ("ready", "feat: ticket 1"))
        self.assertEqual(al.section(note["body"], "Decisions"), "- Keep it simple.")
        self.assertEqual(al.read_handoff(self.tmp / "handoffs" / "5.md")["status"], "needs-human")
        state = json.loads((self.tmp / "runs" / "r1" / "run.json").read_text())
        self.assertEqual(state["tickets"]["1"]["rounds"], 1)
        self.assertEqual(state["tickets"]["2"]["status"], "deferred")
        self.assertTrue(state["finished"])
        self.assertIn("Ready to ship: #1, #3", runner.status_text(state))

    def test_resume_replays_finished_calls(self):
        first = self.execute(self.make_run(["1", "3", "5"]), FakeAdapter())
        again = self.execute(runner.Run.load(self.tmp / "runs" / "r1"), FakeAdapter(fail=True))
        self.assertEqual(again["ready"], first["ready"])
        self.assertEqual(again["needsHuman"], first["needsHuman"])

    def test_plan_only(self):
        fake = FakeAdapter()
        summary = self.execute(self.make_run(["1", "2", "6"], plan_only=True), fake)
        self.assertEqual(summary["stoppedAfter"], "plan")
        self.assertEqual([p["ref"] for p in summary["plans"]], ["#1"])
        self.assertEqual({c[1] for c in fake.calls}, {"plan"})
        run = runner.Run.load(self.tmp / "runs" / "r1")   # what `run --resume R --build` does
        run.plan_only = False
        again = FakeAdapter()
        full = self.execute(run, again)
        self.assertEqual([r["ref"] for r in full["ready"]], ["#1"])
        self.assertNotIn("plan", {c[1] for c in again.calls})  # the plans were reused

    def test_max_parallel_caps_the_batch(self):
        summary = self.execute(self.make_run(["1", "3", "7"], maxParallel=2), FakeAdapter())
        self.assertEqual(summary["deferred"], [{"id": "7", "ref": "#7", "reason": "over maxParallel (2)"}])

    def test_failed_agent_does_not_stop_others(self):
        def flaky(**kw):
            if Path(kw["log_path"]).name.startswith("3.implement"):
                raise RuntimeError("boom")
            return FakeAdapter()(**kw)
        summary = self.execute(self.make_run(["3", "7"]), flaky)
        self.assertEqual([r["ref"] for r in summary["ready"]], ["#7"])
        self.assertEqual(summary["needsHuman"][0]["question"], "implementer failed")

    def test_refuted_findings_do_not_block(self):
        fake = FakeAdapter(refute=True)
        summary = self.execute(self.make_run(["1", "5"]), fake)
        self.assertEqual([(r["ref"], r["rounds"]) for r in summary["ready"]], [("#1", 0), ("#5", 0)])
        self.assertNotIn("fix1", {c[1] for c in fake.calls})

    def test_without_verify_findings_block_directly(self):
        fake = FakeAdapter(refute=True)
        summary = self.execute(self.make_run(["1"], review={"lenses": ["correctness"], "verify": False}), fake)
        self.assertEqual(summary["ready"][0]["rounds"], 1)
        self.assertFalse(any(c[1].startswith("verify") for c in fake.calls))
        self.assertEqual({c[1] for c in fake.calls if c[1].startswith("review")}, {"review-correctness", "review1-correctness"})

    def test_failing_check_goes_to_the_fixer_with_its_output(self):
        prompts = {}
        fake = FakeAdapter()

        def recording(**kw):
            prompts[Path(kw["log_path"]).name.split(".")[1]] = kw["prompt"]
            return fake(**kw)

        state = {"n": 0}

        def checker(worktree, checks):
            state["n"] += 1
            return [{"cmd": "make test", "ok": state["n"] > 1, "tail": "FAILED test_greet - AssertionError"}]

        summary = self.execute(self.make_run(["3"]), recording, checker)
        self.assertEqual(summary["ready"][0]["rounds"], 1)
        self.assertIn("[failing check] `make test`", prompts["fix1"])
        self.assertIn("FAILED test_greet - AssertionError", prompts["fix1"])

    def recorder(self, fake):
        prompts = {}

        def recording(**kw):
            prompts[Path(kw["log_path"]).name.split(".")[1]] = kw["prompt"]
            return fake(**kw)
        return recording, prompts

    def test_red_checks_go_to_the_fixer_before_any_reviewer(self):
        fake = FakeAdapter()
        recording, prompts = self.recorder(fake)
        state = {"n": 0}

        def checker(worktree, checks):
            state["n"] += 1
            return [{"cmd": "make test", "ok": state["n"] > 1, "tail": "FAILED test_x"}]

        summary = self.execute(self.make_run(["3"]), recording, checker)
        labels = [c[1] for c in fake.calls]
        self.assertFalse([x for x in labels if x.startswith(("review-", "verify-"))], labels)  # round 0 skipped review
        self.assertIn("no reviewer has read this version of the change", prompts["fix1"])
        self.assertIn("FAILED test_x", prompts["fix1"])
        self.assertIn("review1-spec", labels)
        self.assertNotIn("## Scope: re-review", prompts["review1-correctness"])  # the first review reads the whole change
        self.assertEqual(summary["ready"][0]["rounds"], 1)

    def test_checks_that_stay_red_need_you(self):
        fake = FakeAdapter()
        summary = self.execute(self.make_run(["3"]), fake,
                               lambda worktree, checks: [{"cmd": "make test", "ok": False, "tail": "boom"}])
        self.assertEqual(summary["needsHuman"][0]["question"], "checks still failing after 2 fix round(s): make test")
        self.assertFalse([c for c in fake.calls if c[1].startswith(("review", "verify"))])

    def test_checks_that_break_after_a_review_keep_its_findings_for_the_handoff(self):
        fake = FakeAdapter()
        recording, prompts = self.recorder(fake)
        state = {"n": 0}

        def checker(worktree, checks):
            state["n"] += 1
            return [{"cmd": "make test", "ok": state["n"] == 1, "tail": "boom"}]  # green, then red after the fix

        summary = self.execute(self.make_run(["1"]), recording, checker)
        self.assertEqual(summary["needsHuman"][0]["question"], "checks still failing after 2 fix round(s): make test")
        self.assertIn("this version of the change", prompts["fix2"])
        self.assertIn("the last fix was not reviewed", prompts["handoff"])
        self.assertIn("- [major] a.py x", prompts["handoff"])                # round 0's finding, not "raised nothing"

    def test_files_outside_the_plan_go_to_the_spec_reviewer(self):
        wt = self.tmp / "wt"
        wt.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
        g("init", "-q")
        (wt / "a.py").write_text("x = 0\n")
        g("add", "-A")
        g("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm", "i")
        base = g("rev-parse", "HEAD")
        for f, text in (("a.py", "x = 1\n"), ("extra.py", "y = 1\n"), ("poetry.lock", "lock\n")):
            (wt / f).write_text(text)
        g("add", "-A")
        run = self.make_run(["1"])
        run.tickets[0].update(worktree=str(wt), base=base)
        run.save()
        fake = FakeAdapter()
        recording, prompts = self.recorder(fake)

        def fixing(**kw):
            if Path(kw["log_path"]).name.split(".")[1] == "fix1":  # the fix spreads into a new file
                (wt / "other.py").write_text("z = 1\n")
                g("add", "-A")
            return recording(**kw)

        summary = self.execute(run, fixing)
        self.assertEqual(summary["ready"][0]["rounds"], 1)
        self.assertIn("- `extra.py`", prompts["review-spec"])
        self.assertIn("class `scope`", prompts["review-spec"])
        self.assertNotIn("poetry.lock", prompts["review-spec"])            # lockfiles are ignored
        self.assertNotIn("Files outside the plan", prompts["review-correctness"])
        self.assertIn("- `other.py`", prompts["review1-spec"])               # the fix's new file is judged
        self.assertNotIn("extra.py", prompts["review1-spec"])                  # the fix did not touch it again
        self.assertEqual(al.read_handoff(self.tmp / "handoffs" / "1.md")["outOfPlan"], "extra.py, other.py")

    def test_out_of_plan(self):
        self.assertEqual(runner.out_of_plan(["a.py", "b.py", "web/package-lock.json", "x.lock", ""], ["a.py"],
                                            runner.DEFAULT_SCOPE_IGNORE), ["b.py"])

    def test_dedupe_keeps_the_worst_per_place(self):
        merged = runner.dedupe([
            {"lens": "standards", "severity": "minor", "file": "a.py", "line": 3, "issue": "name"},
            {"lens": "correctness", "severity": "blocker", "file": "a.py", "line": 3, "issue": "crash"},
            {"lens": "spec", "severity": "major", "file": None, "line": None, "issue": "Missing ValueError"},
        ])
        self.assertEqual(len(merged), 2)
        worst = next(f for f in merged if f["file"] == "a.py")
        self.assertEqual((worst["severity"], worst["lens"]), ("blocker", "correctness, standards"))

    def test_prompts_render_without_leftover_placeholders(self):
        t = ticket("1")
        loop = runner.Loop(self.make_run(["1"]), FakeAdapter())
        for name, extra in (("plan", {}), ("implement", {"plan": "p"}),
                            ("review", {"roundNote": "", "lens": "spec", "lensBrief": runner.render("lenses/spec")}),
                            ("verify", {"lens": "spec", "severity": "major", "finding": "x"}),
                            ("fix", {"round": 1, "findings": "- x"}), ("handoff", {"status": "ready", "plan": "p"})):
            text = runner.render(name, **loop.values(t, **extra))
            self.assertNotIn("{{", text, name)
            self.assertIn("#1", text)


class RunCommandTest(unittest.TestCase):
    """`agentloop.py run` end to end on a real clone with the file tracker and a fake adapter."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        seed, origin, self.clone = self.tmp / "seed", self.tmp / "origin.git", self.tmp / "app"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "a.py").write_text("")
        subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"], check=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(self.clone)], check=True)
        state = self.clone / ".git" / "agent-loop"
        state.mkdir()
        cfg = al.merged(al.DEFAULT_CONFIG, {"repo": "", "commitAtHandoff": False,  # the fake adapter stages nothing to commit
                                            "tracker": {"type": "file", "columns": {"ready": "Todo"}, "show": ["Todo"], "done": ["Done"]}})
        (state / "config.json").write_text(json.dumps(cfg))
        (state / "tickets.json").write_text(json.dumps([
            {"id": 1, "name": "One", "status": "Todo", "desc": "Do one."},
            {"id": 6, "name": "Six", "status": "Todo"},
            {"id": 7, "name": "Seven", "status": "Todo", "blockers": [9]},
            {"id": 9, "name": "Nine", "status": "In Progress"},
        ]))
        self.cwd = os.getcwd()
        os.chdir(self.clone)
        import adapters
        self.adapters, self._orig = adapters, adapters.run_agent
        adapters.run_agent = FakeAdapter()

    def tearDown(self):
        self.adapters.run_agent = self._orig
        os.chdir(self.cwd)

    def test_run_and_status(self):
        out = io.StringIO()
        with redirect_stdout(out):
            al.main(["run"])
        printed = out.getvalue()
        summary = json.loads(printed[printed.index('{\n  "run"'):])
        self.assertEqual([r["ref"] for r in summary["ready"]], ["#1"])          # #7 is blocked by #9, not run
        self.assertEqual(summary["needsHuman"][0]["ref"], "#6")
        self.assertTrue((self.tmp / "app-worktrees" / "1" / "a.py").is_file())
        self.assertNotIn("commit", al.read_handoff(self.clone / ".git" / "agent-loop" / "handoffs" / "1.md"))  # commitAtHandoff off
        status = io.StringIO()
        with redirect_stdout(status):
            al.main(["status"])
        self.assertIn("Ready to ship: #1", status.getvalue())
        moved = io.StringIO()
        with redirect_stdout(moved):
            al.main(["move", "1", "Done"])
        tickets = json.loads((self.clone / ".git" / "agent-loop" / "tickets.json").read_text())
        self.assertEqual(tickets[0]["status"], "Done")


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


class Stop(BaseException):
    """The process dies in the middle of a run."""


class GitAdapter:
    """Every ticket passes review. The implementer writes the ticket's files and stages them.

    files   what each ticket's plan claims (default: its own `t<id>.py`)
    edits   what its implementer writes (default: `t<id>.py`)
    stuck   tickets whose implementer gives up
    stop    (ticket, label): the run dies in that call
    """

    def __init__(self, files=None, edits=None, stuck=(), stop=None, resolve=None):
        self.files, self.edits, self.stuck, self.stop, self.resolve = files or {}, edits or {}, stuck, stop, resolve or {}
        self.calls, self.saw = [], {}

    def __call__(self, *, tool, prompt, schema, cwd, mode, log_path, **_):
        tid, label = Path(log_path).name.split(".")[:2]
        self.calls.append((tid, label))
        if (tid, label) == self.stop:
            raise Stop()
        if label.startswith("plan"):
            return {"feasible": True, "question": None, "summary": "s", "files": self.files.get(tid, [f"t{tid}.py"]), "steps": [],
                    "risks": [], "planMarkdown": f"# Plan for {tid}\n", "knowledge": []}
        if label == "implement":
            if tid in self.stuck:
                return {"summary": "", "checks": [], "stuck": "the API is not there"}
            self.saw[tid] = sorted(f.name for f in Path(cwd).glob("*.py"))  # the code it builds on
            for name, text in self.edits.get(tid, {f"t{tid}.py": f"y = {tid}\n"}).items():
                (Path(cwd) / name).write_text(text)
            git("-C", str(cwd), "add", "-A")
            return {"summary": "ok", "checks": [], "stuck": None}
        if label.startswith("resolve"):
            for name, text in self.resolve.items():
                (Path(cwd) / name).write_text(text)
            git("-C", str(cwd), "add", "-A")
            return {"summary": "combined", "checks": [], "stuck": None, "responses": []}
        if label.startswith("review"):
            return {"findings": []}
        if label == "handoff":
            return {"summary": f"Did {tid}.", "decisions": [], "followUps": [], "openFindings": [],
                    "commitMessage": f"feat: ticket {tid}", "prTitle": f"Ticket {tid}", "prChanges": []}
        raise AssertionError(label)


class StackingRunTest(unittest.TestCase):
    """`agentloop.py run` with the handoff commit: waiting tickets stack on the finished ones."""

    TICKETS = [{"id": 1, "name": "One", "status": "Todo"},
               {"id": 2, "name": "Two", "status": "Todo", "blockers": [1]},
               {"id": 3, "name": "Three", "status": "Todo"}]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        seed, origin, self.clone = self.tmp / "seed", self.tmp / "origin.git", self.tmp / "app"
        git("init", "-q", "-b", "main", str(seed))
        (seed / "a.py").write_text("x = 0\n")
        git("-C", str(seed), "add", "-A")
        git("-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm", "i")
        git("clone", "-q", "--bare", str(seed), str(origin))
        git("clone", "-q", str(origin), str(self.clone))
        for key, value in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            git("-C", str(self.clone), "config", key, value)
        self.state = self.clone / ".git" / "agent-loop"
        self.state.mkdir()
        self.configure()
        self.cwd = os.getcwd()
        os.chdir(self.clone)
        import adapters
        self.adapters, self._orig = adapters, adapters.run_agent

    def tearDown(self):
        self.adapters.run_agent = self._orig
        os.chdir(self.cwd)

    def configure(self, tickets=None, **cfg):
        cfg = al.merged(al.DEFAULT_CONFIG, {"repo": "", "tracker": {"type": "file", "columns": {"ready": "Todo"}, "show": ["Todo"],
                                                                    "done": ["Done"]}, **cfg})
        (self.state / "config.json").write_text(json.dumps(cfg))
        (self.state / "tickets.json").write_text(json.dumps(tickets or self.TICKETS))

    def run_cli(self, adapter, *argv):
        self.adapters.run_agent = adapter
        out = io.StringIO()
        with redirect_stdout(out):
            al.main(["run", *argv])
        printed = out.getvalue()
        return json.loads(printed[printed.index('{\n  "run"'):])

    def rev(self, name):
        return git("-C", str(self.clone), "rev-parse", name)

    def note(self, tid):
        return al.read_handoff(self.state / "handoffs" / f"{tid}.md")

    def test_waiting_tickets_build_on_the_finished_ones_commit(self):
        """#2 waits for #1 by the tracker, #3 because it overlaps #1's files. #5 waits for #4,
        whose handoff commit a hook refuses."""
        self.configure(self.TICKETS + [{"id": 4, "name": "Four", "status": "Todo"},
                                       {"id": 5, "name": "Five", "status": "Todo", "blockers": [4]}])
        hook = Path(git("-C", str(self.clone), "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks" / "commit-msg"
        hook.write_text("#!/bin/sh\nif grep -q 'ticket 4' \"$1\"; then echo 'lint says no' >&2; exit 1; fi\n")
        hook.chmod(0o755)
        fake = GitAdapter(files={"1": ["a.py"], "3": ["a.py"]})
        summary = self.run_cli(fake)
        self.assertEqual({r["ref"]: r["base"] for r in summary["ready"]},
                         {"#1": "origin/main", "#2": "feat/1-one", "#3": "feat/1-one"})
        self.assertEqual([(n["ref"], n["question"]) for n in summary["needsHuman"]], [("#4", "the handoff commit failed: lint says no")])
        self.assertEqual(self.note("4")["status"], "needs-human")
        self.assertEqual(summary["deferred"], [{"id": "5", "ref": "#5", "reason": "waits on #4, which did not finish ready"}])
        self.assertNotIn("5", {tid for tid, _ in fake.calls})      # a ticket the tracker holds back is not planned on the trunk
        self.assertNotIn(("2", "plan"), fake.calls)
        self.assertIn(("3", "plan"), fake.calls)                   # an overlap is only found by planning
        for tid, branch in (("2", "feat/2-two"), ("3", "feat/3-three")):
            self.assertEqual(self.rev(branch + "^"), self.rev("feat/1-one"))
            self.assertIn("t1.py", fake.saw[tid])                   # it was built on #1's code
            self.assertIn((tid, "plan2"), fake.calls)               # and planned there
            self.assertLess(fake.calls.index(("1", "handoff")), fake.calls.index((tid, "implement")))
            note = self.note(tid)
            self.assertEqual((note["status"], note["base"], note["commit"]), ("ready", "feat/1-one", self.rev(branch)))
        self.assertEqual(git("-C", str(self.clone), "log", "-1", "--format=%s", "feat/3-three"), "feat: ticket 3")
        state = json.loads((self.state / "runs" / summary["run"] / "run.json").read_text())
        self.assertEqual(state["tickets"]["3"]["commit"], self.rev("feat/3-three"))

    def test_two_blockers_are_put_in_a_line_and_a_conflict_goes_to_a_resolve_agent(self):
        self.configure([{"id": 1, "name": "One", "status": "Todo"}, {"id": 3, "name": "Three", "status": "Todo"},
                        {"id": 2, "name": "Two", "status": "Todo", "blockers": [1, 3]}])
        # #1 and #3 both change a.py, which neither plan named, so they ran side by side
        fake = GitAdapter(edits={"1": {"a.py": "x = 1\n", "t1.py": "y = 1\n"}, "3": {"a.py": "x = 3\n", "t3.py": "y = 3\n"}},
                          resolve={"a.py": "x = 1\nz = 3\n"})
        summary = self.run_cli(fake)
        self.assertEqual(sorted(r["ref"] for r in summary["ready"]), ["#1", "#2", "#3"])
        resolved = [c for c in fake.calls if c[1].startswith("resolve")]
        self.assertEqual(len(resolved), 1)
        moved = resolved[0][0]
        self.assertEqual(resolved[0][1], "resolve1")
        self.assertEqual((self.tmp / "app-worktrees" / "2" / "a.py").read_text(), "x = 1\nz = 3\n")
        self.assertIn("during the sprint", self.note(moved)["resolved"])
        state = json.loads((self.state / "runs" / summary["run"] / "run.json").read_text())
        self.assertEqual((state["tickets"][moved]["stage"], state["tickets"][moved]["status"]), ("done", "ready"))
        # a resumed run replays the handoffs and keeps the note for the ship skill
        self.run_cli(GitAdapter(stop=("x", "x")), "--resume", summary["run"])
        self.assertIn("during the sprint", self.note(moved)["resolved"])

    def test_resume_after_a_stop_in_the_middle(self):
        with self.assertRaises(Stop), redirect_stdout(io.StringIO()):
            self.adapters.run_agent = GitAdapter(stop=("2", "implement"))
            al.main(["run", "1", "2"])
        run_id = sorted(p.name for p in (self.state / "runs").iterdir())[-1]
        one = self.rev("feat/1-one")
        self.assertEqual(self.rev("feat/2-two"), one)               # moved onto #1, nothing built yet
        fake = GitAdapter()
        summary = self.run_cli(fake, "--resume", run_id)
        self.assertEqual([(r["ref"], r["base"]) for r in summary["ready"]], [("#1", "origin/main"), ("#2", "feat/1-one")])
        self.assertEqual(self.rev("feat/1-one"), one)               # #1 keeps its commit
        self.assertEqual(self.rev("feat/2-two^"), one)
        self.assertEqual([c for c in fake.calls if c[0] == "1"], [])
        self.assertEqual([label for tid, label in fake.calls if tid == "2"][0], "implement")  # its plan was kept
        # a second resume, of a finished run, calls no agent and changes no branch
        two = self.rev("feat/2-two")
        again = self.run_cli(GitAdapter(stop=("x", "x")), "--resume", run_id)
        self.assertEqual(again["ready"], summary["ready"])
        self.assertEqual((self.rev("feat/1-one"), self.rev("feat/2-two")), (one, two))


if __name__ == "__main__":
    unittest.main()
