"""Tests for agentloop.py against fake trackers, a fake gh and real git repositories.

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
import urllib.parse
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


def fake_http(method, url, headers=None, data=None):
    if "api.trello.com" in url:
        path = url.split("/1/", 1)[1].split("?")[0]
        if path.endswith("/lists"):
            return LISTS
        if path.endswith("/cards/closed"):
            return [{"idShort": 899}]
        if path.endswith("/cards"):
            return CARDS
    raise AssertionError(url)


def sh(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def make_origin(tmp: Path) -> Path:
    seed, origin = tmp / "seed", tmp / "origin.git"
    sh("git", "init", "-q", "-b", "main", str(seed))
    (seed / "AGENTS.md").write_text("rules\n")
    (seed / "Makefile").write_text("lint:\n\techo ok\ntest-unit:\n\techo ok\ntest:\n\techo ok\n")
    (seed / "uv.lock").write_text("")
    (seed / "pyproject.toml").write_text("")
    (seed / "alembic" / "versions").mkdir(parents=True)
    (seed / "alembic" / "versions" / ".keep").write_text("")
    (seed / ".github").mkdir()
    (seed / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text("#### Ticket [XXX]()\n\n## Changes\n- \n")
    sh("git", "-C", str(seed), "add", "-A")
    sh("git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    sh("git", "clone", "-q", "--bare", str(seed), str(origin))
    return origin


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.origin = make_origin(self.tmp)
        self.cwd = os.getcwd()
        self._orig = al.http_json, al.gh_json, al.gh
        self._env = dict(os.environ)
        al.http_json = fake_http
        al.gh_json = self.fake_gh
        al.gh = lambda *a: self.gh_calls.append(a) or ""
        self.gh_calls = []
        for k in ("TRELLO_KEY", "TRELLO_TOKEN", "JIRA_EMAIL", "JIRA_API_TOKEN"):
            os.environ.pop(k, None)

    def tearDown(self):
        al.http_json, al.gh_json, al.gh = self._orig
        os.environ.clear()
        os.environ.update(self._env)
        os.chdir(self.cwd)

    def fake_gh(self, *args):
        if args[:2] == ("pr", "list"):
            return OPEN_PRS if "open" in args else []
        if args[:2] == ("repo", "view"):
            return {"name": "x"}
        raise AssertionError(args)

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            al.main(list(argv))
        return buf.getvalue()


class TrelloBareLayoutTest(Base):
    """A bare repo whose worktrees are siblings named by ticket id."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "proj"
        sh("git", "clone", "-q", "--bare", str(self.origin), str(self.root / ".git"))
        sh("git", "-C", str(self.root / ".git"), "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        sh("git", "-C", str(self.root / ".git"), "fetch", "-q", "origin")
        sh("git", "-C", str(self.root / ".git"), "worktree", "add", "-q", str(self.root / "main"), "main")
        sh("git", "-C", str(self.root / ".git"), "worktree", "add", "-q", "-b", "feat/906-already-started", str(self.root / "906"), "origin/main")
        (self.root / "main" / ".env").write_text("X=1\n")
        # The 0.1 config shape: must keep working.
        legacy = {"name": "proj", "repo": "o/proj", "setup": ["test -f .env || cp {main}/.env .env"],
                  "exclusive": ["alembic/versions/"], "mainWorktree": "main",
                  "trello": {"board": "B", "lists": {"ready": "Sprint Backlog", "doing": "In Progress", "review": "Code Review"},
                             "show": ["Sprint Backlog", "In Progress", "Code Review"], "done": ["Done"]}}
        (self.root / ".agent-loop").mkdir()
        (self.root / ".agent-loop" / "config.json").write_text(json.dumps(legacy))
        with redirect_stdout(io.StringIO()):
            al.scaffold(self.root / ".agent-loop")
        os.chdir(self.root / "main")
        os.environ.update(TRELLO_KEY="k", TRELLO_TOKEN="t")

    def test_project_found_from_any_worktree(self):
        os.chdir(self.root / "906")
        p = al.find_project()
        self.assertEqual(p.root, self.root)
        self.assertEqual(p.worktree("12"), self.root / "12")   # legacy configs keep sibling worktrees

    def test_ticket_classification(self):
        got = {t["id"]: t for t in json.loads(self.run_cli("tickets", "--all"))}
        self.assertEqual(got["901"]["status"], "ready")
        self.assertEqual(got["901"]["branch"], "feat/901-list-the-runs")
        self.assertEqual(got["901"]["ref"], "#901")
        self.assertEqual((got["902"]["status"], got["902"]["blockers"]), ("blocked", ["901"]))
        self.assertEqual((got["903"]["status"], got["903"]["base"]), ("ready", "origin/feat/904-store-run-files"))
        self.assertEqual((got["905"]["status"], got["905"]["branch"]), ("ready", "fix/905-fix-the-crash"))
        self.assertEqual(got["906"]["status"], "in-flight")
        self.assertEqual(got["907"]["status"], "ready")
        ready = [t["id"] for t in json.loads(self.run_cli("tickets"))]
        self.assertEqual(ready, ["901", "903", "905", "907"])

    def test_brief_contents(self):
        text = self.run_cli("brief")
        self.assertIn("### Sprint Backlog (6)", text)
        self.assertIn("#902 Show the run detail [blocked: waiting on #901]", text)
        self.assertIn("stacked on #904 (PR #40)", text)
        self.assertIn("#40 Store run files", text)
        self.assertIn("Dead blocker references: #123", text)
        self.assertIn("feat/906-already-started", text)
        self.assertIn("(not filled in yet", text)
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

    def test_prepare_creates_worktree_and_prints_workflow_args(self):
        out = json.loads(self.run_cli("prepare", "901", "#902"))
        [t] = out["tickets"]
        self.assertTrue(t["created"])
        self.assertEqual(out["project"]["dir"], str(self.root / ".agent-loop"))
        self.assertEqual(out["project"]["exclusive"], ["alembic/versions/"])
        wt = self.root / "901"
        self.assertEqual((wt / ".env").read_text(), "X=1\n")
        self.assertEqual(al.git(wt, "branch", "--show-current").strip(), "feat/901-list-the-runs")
        self.assertIn("waiting on #901", out["skipped"][0]["error"])
        again = json.loads(self.run_cli("prepare", "901"))["tickets"][0]
        self.assertEqual(again["status"], "in-flight")
        self.assertNotIn("created", again)
        forced = json.loads(self.run_cli("prepare", "902", "--force"))["tickets"][0]
        self.assertTrue(forced["created"])

    def test_handoffs_and_mark(self):
        d = self.root / ".agent-loop" / "handoffs"
        (d / "901.md").write_text(
            "---\nid: 901\ntitle: List the runs\nstatus: ready\nbranch: feat/901-list-the-runs\nupdated: 2026-10-26T10:00\n---\n\n"
            "## Summary\nLists runs.\n\n## Decisions\n- Paginate with PaginationPage.\n\n## Commit message\nfeat: list the runs\n")
        notes = json.loads(self.run_cli("handoffs", "--status", "ready"))
        self.assertEqual([n["id"] for n in notes], ["901"])
        self.assertIn("Paginate with PaginationPage", self.run_cli("brief"))
        self.run_cli("mark", "#901", "shipped", "--pr", "https://github.com/o/proj/pull/41")
        note = al.read_handoff(d / "901.md")
        self.assertEqual((note["status"], note["pr"]), ("shipped", "https://github.com/o/proj/pull/41"))
        self.assertEqual(json.loads(self.run_cli("handoffs", "--status", "ready")), [])

    def test_detect_bare_layout(self):
        got = json.loads(self.run_cli("detect", "--tracker-url", "https://trello.com/b/M6yGKAhi/team"))
        f, c = got["facts"], got["config"]
        self.assertEqual(f["layout"], "bare repo with sibling worktrees")
        self.assertEqual(f["store"], str(self.root / ".agent-loop"))
        self.assertEqual((c["mainWorktree"], c["worktreePath"]), ("main", "{root}/{id}"))
        self.assertEqual(c["checks"], ["make lint", "make test-unit"])
        self.assertEqual(c["tracker"]["board"], "M6yGKAhi")
        self.assertIn("alembic/versions/", c["exclusive"])
        self.assertEqual(c["pr"]["template"], ".github/PULL_REQUEST_TEMPLATE.md")


ISSUES = [  # a GitHub project's items, as `gh project item-list --format json` prints them
    {"id": "I1", "status": "Todo", "labels": ["bug"], "content": {"type": "Issue", "number": 11, "title": "Fix login", "url": "https://github.com/o/app/issues/11", "body": "Broken.", "repository": "o/app"}},
    {"id": "I2", "status": "Todo", "content": {"type": "Issue", "number": 12, "title": "Add export", "url": "https://github.com/o/app/issues/12", "body": "Blocked by #11", "repository": "o/app"}},
    {"id": "I3", "status": "Todo", "content": {"type": "Issue", "number": 13, "title": "Add import", "url": "https://github.com/o/app/issues/13", "body": "Needs it.", "repository": "o/app"}},
    {"id": "I4", "status": "Todo", "content": {"type": "Issue", "number": 14, "title": "Other repo", "url": "https://github.com/o/web/issues/14", "body": "", "repository": "o/web"}},
    {"id": "I5", "status": "In review", "content": {"type": "PullRequest", "number": 40, "title": "PR", "url": "u", "repository": "o/app"}},
]


class GitHubCloneLayoutTest(Base):
    """A plain clone, a GitHub Projects board, state kept inside .git."""

    def fake_gh(self, *args):
        if args[:2] == ("project", "item-list"):
            return {"items": ISSUES}
        if args[:2] == ("project", "field-list"):
            return {"fields": [{"name": "Status", "options": [{"name": n} for n in ("Todo", "In progress", "In review", "Done")]}]}
        if args[:2] == ("issue", "list"):
            return [{"number": 9}] if "closed" in args else [{"number": n} for n in (11, 12, 13, 50)]
        if args[0] == "api" and args[1].endswith("/13/dependencies/blocked_by"):
            return [{"number": 50, "state": "open"}]
        if args[0] == "api":
            return []
        return super().fake_gh(*args)

    def setUp(self):
        super().setUp()
        self.clone = self.tmp / "app"
        sh("git", "clone", "-q", str(self.origin), str(self.clone))
        os.chdir(self.clone)
        cfg = al.detect(self.clone, "https://github.com/orgs/o/projects/7")["config"]
        cfg.update(repo="o/app", setup=[])  # the fixture's uv.lock is empty, so `uv sync` would fail
        cfg["tracker"].update(columns={"ready": "Todo", "doing": "In progress", "review": "In review"},
                              show=["Todo", "In progress", "In review"], done=["Done"])
        scratch = self.tmp / "cfg.json"
        scratch.write_text(json.dumps(cfg))
        self.run_cli("--config", str(scratch), "init")

    def test_state_lives_in_git_dir_and_is_invisible(self):
        p = al.find_project()
        self.assertEqual(p.dir, self.clone / ".git" / "agent-loop")
        self.assertEqual(p.root, self.clone)
        self.assertEqual(al.git(self.clone, "status", "--porcelain"), "")
        self.assertEqual(p.worktree("12"), self.tmp / "app-worktrees" / "12")

    def test_tickets_from_project_board(self):
        got = {t["id"]: t for t in json.loads(self.run_cli("tickets", "--all"))}
        self.assertEqual(set(got), {"11", "12", "13"})   # other repos and PRs are not tickets
        self.assertEqual((got["11"]["status"], got["11"]["branch"]), ("ready", "fix/11-fix-login"))
        self.assertEqual((got["12"]["status"], got["12"]["reason"]), ("blocked", "waiting on #11"))
        self.assertEqual((got["13"]["status"], got["13"]["blockers"]), ("blocked", ["50"]))  # native dependency, off the board

    def test_prepare_in_sibling_folder_and_move(self):
        out = json.loads(self.run_cli("prepare", "11"))
        wt = Path(out["tickets"][0]["worktree"])
        self.assertEqual(wt, self.tmp / "app-worktrees" / "11")
        self.assertTrue((wt / "AGENTS.md").is_file())
        self.assertEqual(al.find_project(wt).dir, self.clone / ".git" / "agent-loop")
        self.run_cli("move", "11", "review")
        self.assertEqual(self.gh_calls[-1][-4:], ("--field", "Status", "--value", "In review"))
        self.assertIn("https://github.com/o/app/issues/11", self.gh_calls[-1])

    def test_doctor_passes(self):
        out = self.run_cli("doctor")
        self.assertNotIn("FAIL", out)
        self.assertIn("github board readable (4 columns", out)


def adf(*paras):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": t}]} for t in paras]}


JIRA_ISSUES = {"issues": [
    {"key": "APP-1", "fields": {"summary": "Login page", "status": {"name": "To Do"}, "labels": [], "issuetype": {"name": "Story"},
                                "description": adf("Users sign in."), "issuelinks": []}},
    {"key": "APP-2", "fields": {"summary": "Session timeout", "status": {"name": "To Do"}, "labels": [], "issuetype": {"name": "Bug"},
                                "description": None, "issuelinks": [
                                    {"type": {"inward": "is blocked by"}, "inwardIssue": {"key": "APP-1", "fields": {"status": {"statusCategory": {"key": "new"}}}}}]}},
    {"key": "APP-3", "fields": {"summary": "Audit log", "status": {"name": "To Do"}, "labels": [], "issuetype": {"name": "Story"},
                                "description": None, "issuelinks": [
                                    {"type": {"inward": "is blocked by"}, "inwardIssue": {"key": "OPS-9", "fields": {"status": {"statusCategory": {"key": "done"}}}}}]}},
], "isLast": True}


class JiraTest(Base):
    def setUp(self):
        super().setUp()
        self.calls = []

        def jira_http(method, url, headers=None, data=None):
            self.calls.append((method, url, data))
            if "/search/jql" in url:
                return JIRA_ISSUES
            if url.endswith("/transitions") and method == "GET":
                return {"transitions": [{"id": "31", "name": "Review", "to": {"name": "In Review"}}]}
            if url.endswith("/transitions"):
                return None
            raise AssertionError(url)

        al.http_json = jira_http
        self.clone = self.tmp / "app"
        sh("git", "clone", "-q", str(self.origin), str(self.clone))
        os.chdir(self.clone)
        cfg = al.detect(self.clone, "https://acme.atlassian.net/jira/software/projects/APP/boards/3")["config"]
        cfg["repo"] = "o/app"
        cfg["tracker"].update(columns={"ready": "To Do", "doing": "In Progress", "review": "In Review"},
                              show=["To Do", "In Progress", "In Review"], done=["Done"])
        (self.clone / ".git" / "agent-loop").mkdir()
        (self.clone / ".git" / "agent-loop" / "config.json").write_text(json.dumps(cfg))
        os.environ.update(JIRA_EMAIL="me@acme.com", JIRA_API_TOKEN="tok")

    def test_jira_tickets(self):
        got = {t["id"]: t for t in json.loads(self.run_cli("tickets", "--all"))}
        self.assertEqual(got["APP-1"]["ref"], "APP-1")
        self.assertEqual(got["APP-1"]["branch"], "feat/app-1-login-page")
        self.assertEqual(got["APP-1"]["url"], "https://acme.atlassian.net/browse/APP-1")
        self.assertIn("Users sign in.", got["APP-1"]["desc"])
        self.assertEqual((got["APP-2"]["status"], got["APP-2"]["branch"]), ("blocked", "fix/app-2-session-timeout"))
        self.assertEqual(got["APP-3"]["status"], "ready")  # its blocker is done in another project
        self.assertIn("jql=project = APP AND sprint in openSprints() ORDER BY Rank", urllib.parse.unquote_plus(self.calls[0][1]))

    def test_jira_branch_ids_and_move(self):
        s = al.snapshot(al.find_project())
        self.assertEqual(s.branch_id("feat/app-12-thing"), "APP-12")
        self.assertIsNone(s.branch_id("feat/client-feedback"))
        self.run_cli("move", "app-1", "review")
        method, url, data = self.calls[-1]
        self.assertEqual((method, json.loads(data)), ("POST", {"transition": {"id": "31"}}))
        self.assertTrue(url.endswith("/issue/APP-1/transitions"))

    def test_missing_credentials(self):
        del os.environ["JIRA_API_TOKEN"]
        self.assertIn("export JIRA_EMAIL and JIRA_API_TOKEN", self.run_cli("brief"))


class ParseUrlTest(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(al.parse_tracker_url("https://github.com/users/me/projects/3/views/1"),
                         {"type": "github", "owner": "me", "project": 3})
        self.assertEqual(al.parse_tracker_url("https://acme.atlassian.net/browse/APP-12"),
                         {"type": "jira", "site": "https://acme.atlassian.net", "project": "APP"})
        self.assertEqual(al.text_blockers("Depends on #4 and #5\n\n## Blockers\n#6", "Blockers"), ["6", "4", "5"])


if __name__ == "__main__":
    unittest.main()
