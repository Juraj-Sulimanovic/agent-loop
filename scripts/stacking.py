"""Turn the ready tickets into pull request stacks for the ship skill.

`pr.stack` picks the shape:

    sprint  one stack per sprint: chains keep their order, independent tickets go on top, in the
            order they finished; the whole stack goes on top of the user's own open stack when a
            ticket builds on one of its branches
    chains  only tickets that build on another ticket are stacked; the rest target the trunk

`build` commits each staged ticket locally and rebases it in its own worktree (never by checking
a branch out elsewhere: each branch is checked out in its ticket's worktree). A ticket is one
commit: its handoff `commit:` and anything after it. More than one is squashed into one, only
after the user confirmed it (`--squash`). So a rebase stops at most once: a conflict goes to one
resolve agent, the checks run, and when an agent changed code, one read-only combine review
follows. A combine finding is shown, not a stop. A ticket that fails stops its stack there:

- the resolve agent could not resolve the conflicts: the rebase is aborted, the user rebases;
- its checks fail: the branch is left as it is, and the handoff says so (`checksFailed:`,
  `resolved:`), so that the next `build` checks it again;
- the combine review itself failed: the next `build` retries it.

Nothing is resumed: `build` runs from the start each time, and tickets that already sit on the
one below do not move.

Nothing here pushes. The commands for the push, the pull requests and `gh stack link` are
printed for the ship skill to run after the user approves them.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable

import runner

Adapter = Callable[..., dict]
Checker = Callable[[str, list[str]], list[dict]]


# --- git ---------------------------------------------------------------------

def git(wt: str | Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    import os
    return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env={**os.environ, **(env or {})})


def out(wt: str | Path, *args: str) -> str:
    r = git(wt, *args)
    return r.stdout.strip() if r.returncode == 0 else ""


def rev(wt: str | Path, ref: str) -> str:
    return out(wt, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")


def is_ancestor(wt: str | Path, a: str, b: str) -> bool:
    return git(wt, "merge-base", "--is-ancestor", a, b).returncode == 0


def rebasing(wt: str | Path) -> bool:
    d = Path(out(wt, "rev-parse", "--absolute-git-dir"))
    return (d / "rebase-merge").exists() or (d / "rebase-apply").exists()


def unmerged(wt: str | Path) -> list[str]:
    return out(wt, "diff", "--name-only", "--diff-filter=U").splitlines()


NO_EDITOR = {"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}


def own_commits(wt: str | Path, t: dict, onto: str = "") -> list[str] | None:
    """A ticket's own commits, oldest first: its handoff `commit:` and every commit after it.
    When the developer rebased it by hand, that commit is gone: then they are the commits on top
    of `onto`, if it sits on it. [] when it has none yet (staged only); None when unknown."""
    c = t.get("commit") or ""
    if not c:
        return []
    if rev(wt, c) and is_ancestor(wt, c, "HEAD"):
        return out(wt, "rev-list", "--reverse", "--first-parent", f"{c}^..HEAD").splitlines() or None
    onto_commit = rev(wt, onto) if onto else ""
    if onto_commit and is_ancestor(wt, onto_commit, "HEAD"):
        return out(wt, "rev-list", "--reverse", "--first-parent", f"{onto_commit}..HEAD").splitlines() or None
    return None


# --- handoff notes -------------------------------------------------------------

def set_meta(path: Path, key: str, value: str) -> None:
    """Set one front-matter line of a handoff note."""
    text = path.read_text()
    line = f"{key}: {value}"
    if re.search(rf"^{re.escape(key)}:.*$", text, re.M):
        text = re.sub(rf"^{re.escape(key)}:.*$", lambda _: line, text, count=1, flags=re.M)
    else:
        text = text.replace("\n---", f"\n{line}\n---", 1)
    path.write_text(text)


def drop_meta(path: Path, key: str) -> None:
    path.write_text(re.sub(rf"^{key}:.*\n", "", path.read_text(), count=1, flags=re.M))


def section(body: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\s*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    return m.group(1).strip() if m else ""


# --- plan ----------------------------------------------------------------------

def trunk_of(base: str) -> str:
    return re.sub(r"^origin/", "", base)


def linearize(tickets: list[dict]) -> list[list[dict]]:
    """Trees of tickets (a ticket whose base is another's branch hangs under it), each flattened
    parent first, siblings in finish order; the trees in the order their roots finished."""
    tickets = sorted(tickets, key=lambda t: (t.get("updated", ""), t["id"]))
    by_branch = {t["branch"]: t for t in tickets}
    children: dict[str, list[dict]] = {}
    roots = []
    for t in tickets:
        parent = by_branch.get(t.get("base", ""))
        if parent and parent is not t:
            children.setdefault(parent["id"], []).append(t)
        else:
            roots.append(t)

    def walk(t: dict) -> list[dict]:
        return [t] + [x for c in children.get(t["id"], []) for x in walk(c)]

    return [walk(r) for r in roots]


def chain_of(prs: list[dict], pr: dict, trunk: str) -> list[dict]:
    """The open stack a PR is in, bottom to top: down its bases to the trunk, up to the newest
    PR that targets each branch."""
    by_head = {p["headRefName"]: p for p in prs}
    down, cur, seen = [], pr, set()
    while cur and cur["number"] not in seen:
        seen.add(cur["number"])
        down.append(cur)
        cur = by_head.get(cur["baseRefName"]) if cur["baseRefName"] != trunk else None
    chain = list(reversed(down))
    while True:
        above = sorted((p for p in prs if p["baseRefName"] == chain[-1]["headRefName"] and p["number"] not in seen),
                       key=lambda p: p["number"])
        if not above:
            return chain
        seen.add(above[-1]["number"])
        chain.append(above[-1])


def plan(tickets: list[dict], trunk: str, mode: str, prs: list[dict], me: str, link_ok: bool) -> list[dict]:
    """The stacks to build. Each: {anchor, link, tickets: [{..., parent}]}.

    `anchor` is what the bottom ticket sits on: the trunk, the top of the user's own open stack
    (`own`), or someone else's branch (`foreign`: kept as it is, never joined to their stack).
    `parent` is each ticket's PR base; `rebase` says whether its branch must move onto it."""
    by_head = {p["headRefName"]: p for p in prs}
    groups: dict[str, dict] = {}
    for tree in linearize(tickets):
        root = tree[0]
        base = root.get("base") or trunk
        pr = by_head.get(base) if base != trunk else None
        if pr and (pr.get("author") or {}).get("login") == me and me:
            existing = chain_of(prs, pr, trunk)
            key = f"own:{existing[0]['number']}"
            anchor = {"kind": "own", "branch": existing[-1]["headRefName"], "prs": [p["number"] for p in existing]}
        elif pr:
            key, anchor = f"foreign:{base}", {"kind": "foreign", "branch": base, "prs": []}
        else:
            # On the trunk, or on a branch whose PR is gone (merged): it goes onto the trunk.
            key, anchor = "trunk", {"kind": "trunk", "branch": trunk, "prs": []}
        g = groups.setdefault(key, {"anchor": anchor, "trees": []})
        g["trees"].append(tree)

    if mode == "sprint":
        # One stack: everything on the trunk joins the first own stack a ticket builds on, if any.
        own = next((k for k in groups if k.startswith("own:")), None)
        if own and "trunk" in groups:
            groups[own]["trees"] = groups.pop("trunk")["trees"] + groups[own]["trees"]
            groups[own]["trees"].sort(key=lambda tr: (tr[0].get("updated", ""), tr[0]["id"]))

    stacks = []
    for key, g in groups.items():
        anchor = g["anchor"]
        line = mode == "sprint" and anchor["kind"] != "foreign"
        out_tickets = []
        if line:
            parent = anchor["branch"]
            for tree in g["trees"]:
                for t in tree:
                    out_tickets.append({**t, "parent": parent, "rebase": True})
                    parent = t["branch"]
        else:
            for tree in g["trees"]:
                for t in tree:
                    out_tickets.append({**t, "parent": t.get("base") or trunk, "rebase": False})
        stacks.append({"anchor": anchor, "link": line and link_ok and len(anchor["prs"]) + len(out_tickets) > 1,
                       "tickets": out_tickets})
    return stacks


# --- build ---------------------------------------------------------------------

def onto_ref(wt: str, parent: str, trunk: str, anchor: dict) -> str:
    """The commit a ticket goes onto: another ticket's local branch, else the remote branch."""
    if (parent == anchor["branch"] and anchor["kind"] != "foreign") or parent == trunk:
        return f"origin/{parent}"
    return parent if rev(wt, f"refs/heads/{parent}") else f"origin/{parent}"


RESOLVE_SCHEMA = runner.SCHEMAS["build"]
COMBINE_SCHEMA = runner.SCHEMAS["findings"]


def resolve_prompt(state_dir: Path, cfg: dict, brief: str, ref: Callable[[str], str], t: dict, onto: str, below: str,
                   problem: str) -> str:
    """The resolve agent's prompt: ticket `t` (a handoff note) is moving onto `onto`, the branch of
    ticket `below`, and `problem` stops it."""
    note = Path(t["path"]).read_text() if t.get("path") and Path(t["path"]).is_file() else ""
    below_note = ""
    if below and (state_dir / "handoffs" / f"{below}.md").is_file():
        below_note = section((state_dir / "handoffs" / f"{below}.md").read_text(), "Summary")
    return runner.render("resolve", ref=ref(t["id"]), name=t.get("title") or t.get("name", ""), worktree=t["worktree"],
                         branch=t["branch"], onto=onto, problem=problem, summary=section(note, "Summary") or "(no summary)",
                         below=f"The ticket below it, {ref(below)}, did this:\n\n{below_note}" if below_note else "",
                         checksLine=runner.checks_line(cfg.get("checks") or []), briefPath=brief)


def conflict_problem(files: list[str]) -> str:
    return ("The rebase stopped with conflicts in: " + ", ".join(f"`{f}`" for f in files)
            + ". Resolve every conflict so that both changes work, then `git add` the files.")


def checks_problem(failing: list[dict]) -> str:
    return ("After the rebase, these checks fail:\n\n" + "\n".join(
        f"- `{c['cmd']}`" + (f"\n\n```\n{c['tail'][-1200:]}\n```" if c.get("tail") else "") for c in failing)
        + "\n\nFix the code so that this change works on top of the code below it, then `git add -A`.")


def has_markers(wt: str | Path, files: list[str]) -> list[str]:
    return [f for f in files if Path(wt, f).is_file()
            and re.search(r"^(<{7}|>{7})( |$)", Path(wt, f).read_text(errors="ignore"), re.M)]


class Builder:
    def __init__(self, project: dict, cfg: dict, state_dir: Path, run_dir: Path, adapter: Adapter | None,
                 checker: Checker = runner.run_checks, ref: Callable[[str], str] = lambda i: f"#{i}", squash: bool = False):
        self.project, self.cfg, self.dir, self.run_dir = project, cfg, state_dir, run_dir
        self.adapter, self.checker, self.ref, self.squash = adapter, checker, ref, squash
        self.trunk = trunk_of(cfg["base"])

    # one agent call in the ticket's worktree: the resolve agent writes, the combine review only reads
    def agent(self, t: dict, label: str, prompt: str, review: bool = False) -> dict | None:
        if self.adapter is None:
            return None
        self.run_dir.mkdir(parents=True, exist_ok=True)
        agents = self.cfg.get("agents") or {}
        tool = agents.get("review" if review else "fix") or agents.get("default") or "claude"
        if tool == "workflow":
            tool = "claude"
        (self.run_dir / f"{t['id']}.{label}.prompt.md").write_text(prompt)
        try:
            result = self.adapter(tool=tool, prompt=prompt, schema=COMBINE_SCHEMA if review else RESOLVE_SCHEMA,
                                  cwd=Path(t["worktree"]), mode="check" if review else "write",
                                  model=(self.cfg.get("agentModels") or {}).get(tool),
                                  extra_args=(self.cfg.get("agentArgs") or {}).get(tool),
                                  timeout=int(self.cfg.get("agentTimeoutMinutes") or 30) * 60,
                                  log_path=self.run_dir / f"{t['id']}.{label}.log")
        except Exception as e:  # noqa: BLE001 - a failed agent stops this ticket, not the ship
            (self.run_dir / f"{t['id']}.{label}.error.txt").write_text(str(e))
            return None
        (self.run_dir / f"{t['id']}.{label}.json").write_text(json.dumps(result, indent=2))
        return result

    def resolve_prompt(self, t: dict, onto: str, below: str, problem: str) -> str:
        return resolve_prompt(self.dir, self.cfg, self.project.get("briefPath", ""), self.ref, t, onto, below, problem)

    def message(self, t: dict) -> str:
        body = Path(t["path"]).read_text() if t.get("path") and Path(t["path"]).is_file() else ""
        return section(body, "Commit message") or t.get("title") or f"ticket {t['id']}"

    def note(self, t: dict, key: str, value: str | None) -> None:
        """Set (or, with None, drop) one front-matter line of the ticket's handoff note."""
        if t.get("path") and Path(t["path"]).is_file():
            if value is None:
                drop_meta(Path(t["path"]), key)
            else:
                set_meta(Path(t["path"]), key, value)

    def remembered_findings(self, t: dict, head: str) -> list[dict]:
        """Combine findings saved for this commit. A different commit clears them: the user amended."""
        raw = t.get("combineFindings") or ""
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data = None
        if not isinstance(data, dict) or data.get("commit") != head:
            self.note(t, "combineFindings", None)
            return []
        return [f for f in data.get("findings") or [] if isinstance(f, dict)]

    def remember_findings(self, t: dict, findings: list[dict]) -> None:
        """Keep the combine findings with the commit they describe, so a later build can show them."""
        if not findings:
            self.note(t, "combineFindings", None)
            return
        head = out(t["worktree"], "rev-parse", "HEAD")
        self.note(t, "combineFindings", json.dumps({"commit": head, "findings": findings}, separators=(",", ":")))

    def commit_staged(self, t: dict) -> str:
        """Commit what is staged: a new commit when the ticket has none yet (the loop left it
        staged), else into its one commit (a fix the developer staged). '' when done."""
        wt = t["worktree"]
        if git(wt, "diff", "--cached", "--quiet").returncode == 0:
            return ""
        args = ("commit", "-q", "--amend", "--no-edit") if t.get("commit") else ("commit", "-q", "-m", self.message(t))
        r = git(wt, *args)
        if r.returncode:
            return (r.stderr or r.stdout).strip()[-500:] or f"git commit exited {r.returncode}"
        t["commit"] = out(wt, "rev-parse", "HEAD")
        self.note(t, "commit", t["commit"])
        return ""

    def squash_into_one(self, t: dict, own: list[str]) -> str:
        """Squash the ticket's commits into one, named after the handoff commit message; the other
        commits' subjects go in its body. '' when done."""
        wt = t["worktree"]
        title = self.message(t)
        others = [s for s in (out(wt, "log", "-1", "--format=%s", c) for c in own) if s and s != title.splitlines()[0]]
        message = title + ("\n\nAlso:\n" + "\n".join(f"- {s}" for s in others) if others else "")
        git(wt, "reset", "-q", "--soft", f"{own[0]}^")
        r = git(wt, "commit", "-q", "-m", message)
        if r.returncode:
            git(wt, "reset", "-q", "--soft", own[-1])  # back to where it was
            return (r.stderr or r.stdout).strip()[-500:] or f"git commit exited {r.returncode}"
        t["commit"] = out(wt, "rev-parse", "HEAD")
        self.note(t, "commit", t["commit"])
        return ""

    def combine_review(self, t: dict, onto: str, below: str, what: str, diff: str) -> list[dict] | None:
        """The one read-only review of a combine: did it drop or twist either ticket's behaviour?
        The problems found, or None when the reviewer failed."""
        note = Path(t["path"]).read_text() if t.get("path") and Path(t["path"]).is_file() else ""
        below_note = ""
        if below and (self.dir / "handoffs" / f"{below}.md").is_file():
            below_note = section((self.dir / "handoffs" / f"{below}.md").read_text(), "Summary")
        prompt = runner.render("combine", ref=self.ref(t["id"]), name=t.get("title") or t.get("name", ""),
                               worktree=t["worktree"], onto=onto, what=what, summary=section(note, "Summary") or "(no summary)",
                               below=(f"The ticket below it, {self.ref(below)}, did this:\n\n{below_note}" if below_note
                                      else f"The ticket below it is `{onto}`."),
                               diff=diff[-20000:] or "(no diff)")
        result = self.agent(t, "ship-combine", prompt, review=True)
        if result is None:
            return None
        return [f for f in result.get("findings") or [] if f.get("severity") != "minor"]

    def prepare(self, t: dict, onto: str) -> tuple[list[str], str]:
        """Commit what is staged and make the ticket one commit. (its commits, '') or ([], why it stops)."""
        wt = t["worktree"]
        if not Path(wt).is_dir():
            return [], f"the worktree {wt} is gone"
        if rebasing(wt):
            return [], (f"a rebase is in progress in its worktree: finish it (`git -C {wt} rebase --continue`) "
                        f"or abort it (`git -C {wt} rebase --abort`), then run ship again")
        unstaged = [line for line in out(wt, "status", "--porcelain", "--untracked-files=no").splitlines()
                    if line[1:2] not in (" ", "")]
        if unstaged:
            return [], "it has unstaged changes made after the loop: " + ", ".join(line[3:] for line in unstaged)
        own = own_commits(wt, t, onto)
        if own is None:
            return [], (f"its handoff commit {t.get('commit', '')[:12]} is no longer on its branch, and the branch does "
                        f"not sit on {onto}, so its own commits are unknown. Rebase it onto {onto} by hand and run ship again.")
        if own and own[0] != t.get("commit"):  # rebased by hand: its first own commit is the new handoff commit
            t["commit"] = own[0]
            t["handRebased"] = True  # its checks run again
            self.note(t, "commit", own[0])
        error = self.commit_staged(t)
        if error:
            return [], f"the commit failed: {error}"
        own = own_commits(wt, t, onto) or []
        if len(own) > 1:
            if not self.squash:
                return [], (f"it has {len(own)} commits; ship squashes them into one, named after its handoff commit "
                            "message, only after you confirm it")
            error = self.squash_into_one(t, own)
            if error:
                return [], f"the squash failed: {error}"
            own = [t["commit"]]
        return own, ""

    def ticket(self, t: dict, onto: str, below: str) -> dict:
        """Build one ticket: one commit, rebase, at most one resolve agent, checks, combine review.

        A combine finding is shown with the ticket, not a stop."""
        wt = t["worktree"]
        res = {"rebased": False, "resolved": [], "checks": [], "findings": []}
        stop = lambda reason, **more: {**res, **more, "status": "stopped", "reason": reason}  # noqa: E731
        before = out(wt, "rev-parse", "HEAD") if Path(wt).is_dir() else ""
        undo = f" To go back to before this ship: `git -C {wt} reset --hard {before[:12]}` (it is in `git -C {wt} reflog`)."
        own, why = self.prepare(t, onto)
        if why:
            return stop(why)
        onto_commit = rev(wt, onto)
        if not onto_commit:
            return stop(f"{onto} does not exist; fetch it or build the ticket below first")
        fork = out(wt, "rev-parse", f"{own[0]}^")

        diff = ""
        if not is_ancestor(wt, onto_commit, "HEAD"):
            git(wt, "rebase", "--onto", onto_commit, fork, env=NO_EDITOR)
            res["rebased"] = True
            if rebasing(wt):  # one commit, so it stops at most once
                files = unmerged(wt)
                answer = self.agent(t, "ship-resolve", self.resolve_prompt(t, onto, below, conflict_problem(files)))
                left = unmerged(wt) or has_markers(wt, files)
                if not answer or answer.get("stuck") or left:
                    why = ((answer or {}).get("stuck") or ("still conflicted: " + ", ".join(left) if left else "the resolve agent failed"))
                    git(wt, "rebase", "--abort")
                    return stop(f"could not resolve the conflicts with {onto} ({why}). The rebase was aborted. Rebase it by "
                                f"hand: `git -C {wt} rebase --onto {onto} HEAD^`, resolve the conflicts, `git add` them, "
                                "`git rebase --continue`, then run ship again.", conflicts=files)
                diff = out(wt, "diff", "--cached", onto_commit, "--", *files)
                git(wt, "rebase", "--continue", env=NO_EDITOR)
                if rebasing(wt):
                    git(wt, "rebase", "--abort")
                    return stop("the rebase still stops after the conflicts were resolved; it was aborted." + undo, conflicts=files)
                res["resolved"].append(f"resolved the conflicts in {', '.join(files)} with {onto}")
            t["commit"] = out(wt, "rev-parse", "HEAD")
            self.note(t, "commit", t["commit"])
            self.note(t, "base", trunk_of(onto))

        # A ticket that an agent changed earlier (in the sprint, or in a ship that stopped) is checked again.
        if t.get("resolved") and not res["resolved"]:
            res["resolved"].append(t["resolved"])
        if not (res["rebased"] or res["resolved"] or t.get("checksFailed") or t.get("handRebased")):
            res["findings"] = self.remembered_findings(t, out(wt, "rev-parse", "HEAD"))
            return {**res, "status": "built"}

        checks = self.checker(wt, self.cfg.get("checks") or [])
        res["checks"] = checks
        failing = [c for c in checks if not c["ok"]]
        if res["resolved"]:
            self.note(t, "resolved", " and ".join(res["resolved"]))
        if failing:
            self.note(t, "checksFailed", f"on top of {onto}")
            return stop(f"its checks fail on top of {onto}: " + ", ".join(c["cmd"] for c in failing)
                        + ". The branch is left as it is. Fix it in its worktree, `git add` the fix, and run ship again." + undo)
        self.note(t, "checksFailed", None)

        if res["resolved"]:  # an agent changed code: one read-only review of the combine
            findings = self.combine_review(t, onto, below, " and ".join(res["resolved"]),
                                           diff or out(wt, "diff", onto_commit, "HEAD"))
            if findings is None:
                return stop("the combine review failed; run ship again to retry it." + undo)
            res["findings"] = findings
            self.remember_findings(t, findings)
            self.note(t, "resolved", None)  # reviewed: a later build shows the saved findings, it does not review again
        return {**res, "status": "built"}

    def build(self, stacks: list[dict]) -> list[dict]:
        for s in stacks:
            stopped = None
            prev = ""
            for t in s["tickets"]:
                if stopped:
                    t.update(status="waiting", reason=f"waits for {self.ref(stopped)}")
                    continue
                wt = t["worktree"]
                if s["anchor"]["kind"] != "foreign":
                    onto = onto_ref(wt, t["parent"], self.trunk, s["anchor"])
                else:
                    onto = t["parent"] if rev(wt, f"refs/heads/{t['parent']}") else f"origin/{t['parent']}"
                if t["rebase"]:
                    r = self.ticket(t, onto, prev)
                else:
                    error = self.commit_staged(t) if Path(wt).is_dir() else f"the worktree {wt} is gone"
                    r = {"status": "stopped", "reason": f"the commit failed: {error}"} if error else {"status": "built"}
                t.update(r)
                t["commit"] = out(wt, "rev-parse", "HEAD") if Path(wt).is_dir() else ""
                t["stat"] = out(wt, "diff", "--stat", onto, "HEAD") if Path(wt).is_dir() else ""
                if r["status"] != "built":
                    stopped = t["id"]
                prev = t["id"]
            s["stoppedAt"] = stopped
        return stacks


# --- the commands for the ship skill ----------------------------------------------

def commands(stacks: list[dict], repo: str, root: str, state_dir: Path, prs: list[dict], draft: bool) -> None:
    """Add the publish commands to each stack that has no stopped ticket.

    A stopped ticket publishes nothing in its stack, including the tickets built below it."""
    by_head = {p["headRefName"]: p for p in prs}
    for s in stacks:
        if s.get("stoppedAt"):
            s["commands"] = []
            continue
        cmds, built = [], [t for t in s["tickets"] if t.get("status") == "built"]
        for t in built:
            q = shlex.quote
            cmds.append(f"git -C {q(t['worktree'])} push --force-with-lease -u origin {q(t['branch'])}")
            pr = by_head.get(t["branch"])
            if pr:
                cmds.append(f"gh pr edit {pr['number']} -R {q(repo)} --base {q(t['parent'])}")
            else:
                body = Path(t.get("path") or "").read_text() if t.get("path") else ""
                title = section(body, "PR title") or t.get("title") or t["branch"]
                cmds.append(f"gh pr create -R {q(repo)} --head {q(t['branch'])} --base {q(t['parent'])} --title {q(title)} "
                            f"--body-file {q(str(state_dir / 'plans' / (t['id'] + '.pr.md')))}" + (" --draft" if draft else ""))
        if s["link"] and len(s["anchor"]["prs"]) + len(built) > 1 and built:
            cmds.append(f"cd {shlex.quote(root)} && gh stack link " + " ".join(
                [str(n) for n in s["anchor"]["prs"]] + [shlex.quote(t["branch"]) for t in built]))
        s["commands"] = cmds


def gh_stack_installed() -> bool:
    try:
        return subprocess.run(["gh", "stack", "--version"], capture_output=True, text=True, timeout=30).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def run_dir_for(state_dir: Path) -> Path:
    return state_dir / "runs" / ("ship-" + time.strftime("%Y%m%d-%H%M%S"))
