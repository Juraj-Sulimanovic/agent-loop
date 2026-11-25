"""The sprint loop: plan every ticket, batch the ones that do not overlap, then implement ->
independent review -> fix (bounded rounds) -> handoff per ticket, in parallel. When the project
commits at handoff, a ticket that overlaps a running one, or that waits for one, starts when that
one is ready, on top of its commit (the same as workflows/sprint.js).

Each agent call goes through an adapter (`adapters.run_agent` by default), which runs Claude
Code, Codex or Cursor headless in the ticket's worktree and returns the stage's JSON. Every
finished call is saved under `<agent-loop dir>/runs/<run id>/`, so a resumed run skips it.
Agents never write outside their worktree: the runner writes the plan and handoff files.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import tempfile
import threading
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import knowledge  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PROMPTS = PLUGIN_ROOT / "prompts"
SCHEMAS = json.loads((PROMPTS / "schemas.json").read_text())
STAGES = ("plan", "implement", "review", "fix", "handoff")
MODES = {"plan": "read", "implement": "write", "review": "check", "fix": "write", "handoff": "read"}

Adapter = Callable[..., dict]
Stacker = Callable[[str, str, list], dict]  # agentloop.stack_ticket for one run: (ticket, onto, needs)
RESOLVE_ROUNDS = 6


def render(template: str, /, **values) -> str:
    text = (PROMPTS / f"{template}.md").read_text()
    text = re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), "")), text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def checks_line(checks: list[str]) -> str:
    if checks:
        return "The checks are: " + ", ".join(f"`{c}`" for c in checks) + ". Run them from the worktree."
    return "No checks are configured; run whatever the repository instructions name for linting and tests."


def claims(plan: dict, exclusive: list[str]) -> set[str]:
    out = set()
    for f in plan.get("files") or []:
        area = next((prefix for prefix in exclusive if f.startswith(prefix)), None)
        out.add(f"area:{area}" if area else f"file:{f}")
    return out


LENSES = ("spec", "correctness", "standards", "security")
DEFAULT_LENSES = ("spec", "correctness", "standards")
SEVERITY = {"blocker": 0, "major": 1, "minor": 2}


SANDBOX_CACHE = "cursor-sandbox-cache"


def writable(directory: Path) -> bool:
    """Whether a file can really be created in `directory` (made if missing). A sandbox can deny the
    write even when access(2) says yes, so this tries it."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory):
            return True
    except OSError:
        return False


def check_env(env: dict | None = None) -> dict:
    """The environment a check runs with. Cursor's sandbox points UV_CACHE_DIR at a private copy under
    cursor-sandbox-cache, and the variable outlives the sandbox: a command allowed out of it still
    inherits it, and that copy can hold metadata uv cannot read (uvx then fails with no output). Drop
    it then, so uv uses its default cache, but only when that cache is writable from here: inside the
    sandbox it is not, and unsetting would only trade the problem for a permission error. A cache
    path set anywhere else (by a person or CI) is kept."""
    env = dict(os.environ if env is None else env)
    cache = env.get("UV_CACHE_DIR", "")
    if SANDBOX_CACHE in Path(cache).parts:
        base = Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else Path.home() / ".cache"
        if writable(base / "uv"):
            del env["UV_CACHE_DIR"]
    return env


def run_checks(worktree: str, checks: list[str], timeout: int = 900) -> list[dict]:
    """Run the configured checks in the worktree: objective, so the runner runs them, not an agent."""
    results = []
    env = check_env()
    for cmd in checks:
        try:
            r = subprocess.run(cmd, shell=True, cwd=worktree, capture_output=True, text=True, timeout=timeout,
                               env=env)
            results.append({"cmd": cmd, "ok": r.returncode == 0, "tail": (r.stdout + r.stderr).strip()[-1500:]})
        except subprocess.TimeoutExpired:
            results.append({"cmd": cmd, "ok": False, "tail": f"timed out after {timeout // 60} min"})
    return results


def dedupe(findings: list[dict]) -> list[dict]:
    """One finding per place: the same file and line from two reviewers keeps the more severe one."""
    out: dict = {}
    for f in sorted(findings, key=lambda f: SEVERITY.get(f["severity"], 3)):
        key = (f.get("file"), f.get("line")) if f.get("file") and f.get("line") else ("text", f["issue"].strip().lower())
        if key in out:
            out[key]["lens"] = ", ".join(dict.fromkeys(out[key]["lens"].split(", ") + [f["lens"]]))
        else:
            out[key] = dict(f)
    return list(out.values())


def finding_text(f: dict) -> str:
    where = f"{f['file']}{':' + str(f['line']) if f.get('line') else ''}: " if f.get("file") else ""
    return f"{where}{f['issue']}" + (f"\nEvidence: {f['evidence']}" if f.get("evidence") else "") + (f"\nSuggested fix: {f['fix']}" if f.get("fix") else "")


def blocking(review: dict) -> bool:
    """Confirmed blocker or major findings the change introduced, or a failing check."""
    return (any(f["severity"] != "minor" for f in review["findings"])
            or bool(review["unmetCriteria"]) or any(not c["ok"] for c in review["checks"]))


def class_key(f: dict) -> tuple[str, str]:
    return ((f.get("class") or f["issue"][:60]).strip().lower(), f.get("file") or "")


def sensitive_matches(paths: list[str], globs: list[str]) -> list[str]:
    return sorted({p for p in paths for g in globs if fnmatch.fnmatch(p, g) or fnmatch.fnmatch(p, g.rstrip("/") + "/*")})


DEFAULT_SCOPE_IGNORE = ["*.lock", "package-lock.json", "pnpm-lock.yaml", "poetry.lock", "uv.lock"]


def out_of_plan(changed: list[str], planned: list[str], ignore: list[str]) -> list[str]:
    """The changed files the plan did not list, leaving out the ones `ignore` matches (lockfiles)."""
    listed = set(planned)
    ignored = lambda f: any(fnmatch.fnmatch(f, g) or fnmatch.fnmatch(Path(f).name, g) for g in ignore)  # noqa: E731
    return sorted(f for f in set(changed) if f and f not in listed and not ignored(f))


def scope_files_note(files: list[str]) -> str:
    """For the spec reviewer: the staged files outside the plan, to judge one by one."""
    if not files:
        return ""
    return "\n".join([
        "", "## Files outside the plan", "",
        "The plan did not list these staged files:", "", *(f"- `{f}`" for f in files), "",
        "For each one, report a `major` finding with class `scope` and that file, unless the ticket clearly needs the "
        "change there (say why in the finding). Do not report them otherwise.",
    ])


def git_out(worktree: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def fingerprint(worktree: str) -> str:
    """The worktree's tracked and staged content. Untracked files are left out: running tests
    creates caches (`__pycache__`, coverage files) that are not an edit."""
    out = []
    for args in (["status", "--porcelain", "--untracked-files=no"], ["diff"], ["diff", "--cached"]):
        r = subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True)
        out.append(r.stdout)
    return "\n".join(out)


def last_review_text(review: dict | None, last_ran: dict | None) -> str:
    """What the handoff agent hears about the review. A ticket that ended on failing checks had no
    review of its final version: then it hears the last review that ran, or that none did."""
    lines = lambda r: "\n".join(f"- [{f['severity']}] {f.get('file') or ''} {f['issue']}" for f in r["findings"])  # noqa: E731
    if review and review.get("skipped"):
        if last_ran and last_ran["findings"]:
            return ("The checks still fail, so the last fix was not reviewed. Findings of the last review that ran, "
                    "not checked again:\n" + lines(last_ran))
        return "No review of the final version ran: the checks still fail." if not last_ran else \
            "The checks still fail, so the last fix was not reviewed; the last review that ran raised nothing."
    return "Last review findings:\n" + lines(review) if review and review["findings"] else "The last review raised nothing."


def commit_ticket(worktree: str, message: str, base: str = "") -> tuple[str, str]:
    """Commit a ready ticket's staged change on its branch, locally: (commit, error).

    A Python step makes this commit, not an agent, so a ticket waiting on this one can build on
    it. Signing and commit hooks run as configured; push and the pull request wait for the ship
    skill and the user's approval."""
    wt = str(worktree)
    if subprocess.run(["git", "-C", wt, "diff", "--cached", "--quiet"]).returncode == 0:
        # Already committed by an earlier handoff of this ticket (a resumed run): keep that commit.
        if base and git_out(wt, "rev-list", "--count", f"{base}..HEAD") not in ("", "0"):
            return git_out(wt, "rev-parse", "HEAD"), ""
        return "", "nothing is staged"
    try:
        out = subprocess.run(["git", "-C", wt, "commit", "-q", "-m", message], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=300)
    except subprocess.TimeoutExpired:
        return "", "git commit timed out after 5 min"
    if out.returncode:
        return "", (out.stderr or out.stdout).strip()[-500:] or f"exit {out.returncode}"
    return git_out(wt, "rev-parse", "HEAD"), ""


def write_handoff_file(state_dir: Path, run_id: str, t: dict, res: dict, h: dict | None,
                       sensitive: list[str] | None = None, out_of_plan: list[str] | None = None) -> Path:
    """The handoff note the brief, the ship skill and the decisions log read. `res` is the
    ticket's result (status, why, preExisting findings); `h` the handoff agent's answer."""
    h = h or {"summary": f"Handoff agent failed. {res.get('why', '')}".strip(), "decisions": [], "followUps": [],
              "openFindings": [], "commitMessage": "", "prTitle": t["name"], "prChanges": [], "testing": "", "risk": "", "revert": ""}
    bullets = lambda xs: "\n".join(f"- {x}" for x in xs or []) or "- None"  # noqa: E731
    pre = [finding_text(f).splitlines()[0] + (f" [{f['class']}]" if f.get("class") else "") for f in res.get("preExisting") or []]
    front = ["---", f"id: {t['id']}", f"title: {t['name']}", f"status: {res['status']}", f"branch: {t['branch']}",
             f"base: {re.sub(r'^origin/', '', t['base'])}", f"worktree: {t['worktree']}", f"card: {t.get('url', '')}",
             f"updated: {now()}", f"run: {run_id}", f"risk: {'high' if sensitive else 'normal'}"]
    if sensitive:
        front.append(f"sensitive: {', '.join(sensitive)}")
    if out_of_plan:
        front.append(f"outOfPlan: {', '.join(out_of_plan)}")
    if h.get("commit") or res.get("commit"):
        front.append(f"commit: {h.get('commit') or res['commit']}")
    d = state_dir / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{t['id']}.md"
    if path.is_file():  # a replayed handoff keeps the stacking step's note that an agent combined this ticket with another
        front += re.findall(r"^resolved:.*$", path.read_text().split("\n---", 1)[0], re.M)[:1]
    text = "\n".join(front + ["---", "",
        "## Summary", (f"**Needs you:** {res['why']}\n\n" if res.get("why") and res.get("status") != "ready" else "")
        + (h.get("summary") or "").strip(), "",
        "## Decisions", bullets(h.get("decisions")), "",
        "## Follow-ups", bullets(h.get("followUps")), "",
        "## Open review findings", bullets(h.get("openFindings")), "",
        "## Pre-existing issues", bullets(pre), "",
        "## Testing", (h.get("testing") or "Not stated.").strip(), "",
        "## Risk", (h.get("risk") or "Not stated.").strip(), "",
        "## How to revert", (h.get("revert") or "Not stated.").strip(), "",
        "## Commit message", (h.get("commitMessage") or "").strip(), "",
        "## PR title", (h.get("prTitle") or "").strip(), "",
        "## PR changes", bullets(h.get("prChanges")), "",
    ])
    path.write_text(text)
    return path


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Run:
    """One run: its folder, the prepared tickets and project settings, and the tool per stage."""

    dir: Path
    project: dict
    tickets: list[dict]
    agents: dict
    models: dict = field(default_factory=dict)
    timeout_minutes: int = 30
    agent_args: dict = field(default_factory=dict)
    plan_only: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def id(self) -> str:
        return self.dir.name

    def tool(self, stage: str) -> str:
        return self.agents.get(stage) or self.agents.get("default") or "claude"

    def log(self, message: str) -> None:
        line = f"{now()} {message}"
        print(line, flush=True)
        with self.lock, open(self.dir / "log.txt", "a") as f:
            f.write(line + "\n")

    def state(self) -> dict:
        path = self.dir / "run.json"
        with self.lock:
            return json.loads(path.read_text()) if path.is_file() else {}

    def update(self, **changes) -> None:
        with self.lock:
            state = self.state()
            for k, v in changes.items():
                if k == "ticket":
                    tid, fields = v
                    state.setdefault("tickets", {}).setdefault(tid, {}).update(fields)
                else:
                    state[k] = v
            tmp = self.dir / "run.json.tmp"
            tmp.write_text(json.dumps(state, indent=2))
            tmp.replace(self.dir / "run.json")  # atomic: a reader never sees half a file

    def save(self) -> None:
        """Write everything needed to resume this run."""
        self.update(id=self.id, created=now(), project=self.project, prepared=self.tickets, agents=self.agents,
                    models=self.models, timeoutMinutes=self.timeout_minutes, agentArgs=self.agent_args, planOnly=self.plan_only,
                    tickets={t["id"]: {"ref": t["ref"], "name": t["name"], "stage": "queued"} for t in self.tickets})

    @classmethod
    def load(cls, run_dir: Path) -> "Run":
        s = json.loads((run_dir / "run.json").read_text())
        return cls(run_dir, s["project"], s["prepared"], s["agents"], s.get("models", {}),
                   s.get("timeoutMinutes", 30), s.get("agentArgs", {}), s.get("planOnly", False))


class Loop:
    def __init__(self, run: Run, adapter: Adapter, checker: Callable[[str, list[str]], list[dict]] = run_checks,
                 stack: Stacker | None = None):
        self.run, self.adapter, self.checker, self.stack = run, adapter, checker, stack
        self.p = run.project
        review = self.p.get("review") or {}
        self.lenses = [x for x in review.get("lenses", DEFAULT_LENSES) if x in LENSES] or list(DEFAULT_LENSES)
        self.verify = review.get("verify", True)
        self.votes = max(1, int(review.get("votes", 3)))
        self.review_file = review.get("file", "REVIEW.md")
        self.sensitive = self.p.get("sensitivePaths") or []
        self.scope_ignore = review.get("scopeIgnore", DEFAULT_SCOPE_IGNORE)
        self.checks = checks_line(self.p.get("checks") or [])
        self.parallel = max(1, min(int(self.p.get("maxParallel") or 10), 16))
        self.max_fix = max(0, min(int(self.p.get("maxFixRounds", 2)), 4))
        self.commits = bool(self.p.get("commitAtHandoff"))
        self.stacking = self.commits and stack is not None  # without a commit nothing can build on a finished ticket

    # --- one agent call, cached -------------------------------------------------

    def call(self, t: dict, label: str, stage: str, prompt: str, schema: str) -> dict | None:
        cached = self.run.dir / f"{t['id']}.{label}.json"
        if cached.is_file():
            return json.loads(cached.read_text())
        tool = self.run.tool(stage)
        self.run.update(ticket=(t["id"], {"stage": label}))
        self.run.log(f"{t['ref']} {label} ({tool})")
        mode = MODES[stage]
        (self.run.dir / f"{t['id']}.{label}.prompt.md").write_text(prompt)  # what the agent was asked, for anyone debugging a run
        before = fingerprint(t["worktree"]) if mode == "check" else None
        try:
            result = self.adapter(
                tool=tool, prompt=prompt, schema=SCHEMAS[schema], cwd=Path(t["worktree"]), mode=mode,
                model=self.run.models.get(tool), extra_args=self.run.agent_args.get(tool),
                timeout=self.run.timeout_minutes * 60, log_path=self.run.dir / f"{t['id']}.{label}.log",
            )
        except Exception as e:  # noqa: BLE001 - one failed agent must not stop the other tickets
            self.run.log(f"{t['ref']} {label} failed: {e}")
            return None
        if before is not None and fingerprint(t["worktree"]) != before:
            self.run.log(f"{t['ref']} {label}: the reviewer changed files in the worktree")
            result.setdefault("findings", []).append({"severity": "major", "file": None, "line": None, "fix": None,
                                                      "issue": "The reviewer edited the worktree; re-check its changes."})
        cached.write_text(json.dumps(result, indent=2))
        return result

    def knowledge_note(self, picked: dict | None) -> str:
        """For the planner: where the index is. For later stages: the items the planner picked."""
        if picked is None:
            index = self.p.get("knowledgeIndex")
            return (f"## Project knowledge\n\nClient transcripts, documents and test files are indexed in `{index}`, "
                    "with paths to readable text versions. Read the index, then only the items this ticket needs.") if index else ""
        return knowledge.ticket_note(knowledge.resolve(Path(self.p["dir"]), picked.get("knowledge") or []))

    def values(self, t: dict, picked: dict | None = None, **extra) -> dict:
        base = {k: t.get(k, "") for k in ("ref", "name", "url", "worktree", "branch", "base")}
        base.update(desc=t.get("desc") or "(no description)", briefPath=self.p.get("briefPath", ""),
                    checksLine=self.checks, knowledgeNote=self.knowledge_note(picked))
        base["header"] = render("header", **base)
        base.update(extra)
        return base

    # --- stages ---------------------------------------------------------------

    def plan(self, t: dict, label: str = "plan") -> dict | None:
        plan = self.call(t, label, "plan", render("plan", **self.values(t)), "plan")
        if plan and plan.get("planMarkdown"):
            plans = Path(self.p["dir"]) / "plans"
            plans.mkdir(parents=True, exist_ok=True)
            (plans / f"{t['id']}.md").write_text(plan["planMarkdown"].strip() + "\n")
        return plan

    def check_results(self, t: dict, round_: int) -> list[dict]:
        cached = self.run.dir / f"{t['id']}.checks{round_ or ''}.json"
        if cached.is_file():
            return json.loads(cached.read_text())
        results = self.checker(t["worktree"], self.p.get("checks") or [])
        cached.write_text(json.dumps(results, indent=2))
        failed = [c["cmd"] for c in results if not c["ok"]]
        self.run.log(f"{t['ref']} checks: " + (f"failing {', '.join(failed)}" if failed else f"{len(results)} passing"))
        return results

    # --- review ---------------------------------------------------------------

    def ticket_state(self, t: dict) -> dict:
        return (self.run.state().get("tickets") or {}).get(t["id"], {})

    def tree(self, t: dict, step: str) -> str:
        """The staged tree after a build step, recorded once so a resumed run sees the same one."""
        trees = self.ticket_state(t).get("trees", {})
        if step not in trees:
            trees[step] = git_out(t["worktree"], "write-tree")
            self.run.update(ticket=(t["id"], {"trees": trees}))
        return trees[step]

    def risk(self, t: dict, plan: dict, round_: int = 0) -> list[str]:
        """The sensitive paths the ticket's plan or staged change touches (empty: normal risk).

        Looked at again in every review round, since a fix can touch one; each round's answer is
        recorded, so a resumed run asks the same reviewers."""
        if not self.sensitive:
            return []
        state = self.ticket_state(t)
        rounds = state.get("sensitiveRounds")
        if rounds is None and "sensitive" in state:
            return state["sensitive"]  # a run saved before rounds were recorded
        rounds = rounds or {}
        if str(round_) not in rounds:
            changed = git_out(t["worktree"], "diff", "--cached", "--name-only", t["base"]).splitlines()
            hits = sensitive_matches(list(plan.get("files") or []) + changed, self.sensitive)
            if hits and hits != state.get("sensitive"):
                self.run.log(f"{t['ref']} high risk: touches {', '.join(hits)}")
            rounds[str(round_)] = hits
            self.run.update(ticket=(t["id"], {"risk": "high" if hits else "normal", "sensitive": hits, "sensitiveRounds": rounds}))
        return rounds[str(round_)]

    def review_file_note(self, t: dict) -> str:
        if self.review_file and (Path(t["worktree"]) / self.review_file).is_file():
            return (f"`{self.review_file}` at the worktree root is the code owners' guidance for review: what matters, "
                    "what not to flag, which paths to skip. Where it differs from the defaults below, it wins.")
        return ""

    def scope_note(self, round_: int, prev_tree: str, cur_tree: str, prior: list[dict], responses: list[dict]) -> str:
        if not round_ or not prev_tree:  # the first review that runs reads the whole change
            return ""
        answers = {r["n"]: r for r in responses or []}
        lines = []
        for n, f in enumerate(prior, 1):
            a = answers.get(n)
            reply = f" -- fixer: {a['action']}: {a['note']}" if a else " -- fixer: no answer"
            lines.append(f"{n}. [{f['severity']}, {f.get('class') or 'unclassified'}] {finding_text(f).splitlines()[0]}{reply}")
        return "\n".join([
            "", f"## Scope: re-review round {round_}", "",
            f"The previous round's findings went to a fixer. Review only what the fix changed: `git diff {prev_tree} {cur_tree}`. "
            "Report (a) earlier findings that are still unresolved, keeping their class, and (b) problems the fix itself introduced. "
            "Report nothing else: no new findings in code the fix did not touch, nothing already settled. When the fixer disputed "
            "a finding with a reason, report it again only if the reason is wrong.", "",
            "Earlier findings and the fixer's answers:", "", *lines,
        ])

    def verdict(self, t: dict, plan: dict, label: str, f: dict, votes: int) -> dict | None:
        """Ask `votes` skeptics to refute the finding; a majority of those who answered decides."""
        prompt = render("verify", **self.values(t, plan, lens=f["lens"], severity=f["severity"], finding=finding_text(f),
                                                reviewFileNote=self.review_file_note(t)))
        labels = [label] if votes == 1 else [f"{label}-v{k + 1}" for k in range(votes)]
        with ThreadPoolExecutor(len(labels)) as pool:
            answers = [v for v in pool.map(lambda lab: self.call(t, lab, "review", prompt, "verify"), labels) if v]
        if not answers:
            return None
        confirmed = [v for v in answers if v["verdict"] == "confirmed"]
        if len(confirmed) * 2 <= len(answers):
            return {"verdict": "refuted", "reason": next(v["reason"] for v in answers if v["verdict"] == "refuted"),
                    "votes": f"{len(confirmed)}/{len(answers)}"}
        severity = sorted((v["severity"] for v in confirmed), key=SEVERITY.get)[len(confirmed) // 2]  # the median grade
        return {"verdict": "confirmed", "severity": severity, "reason": confirmed[0]["reason"],
                "votes": f"{len(confirmed)}/{len(answers)}"}

    def review(self, t: dict, plan: dict, round_: int, prior: list[dict] | None = None,
               responses: list[dict] | None = None, trees: tuple[str, str] = ("", "")) -> dict | None:
        """Independent reviewers, one per angle, in parallel; then skeptics per blocking finding.

        Round 0 reviews the whole change. Later rounds review only what the last fix changed,
        against the earlier findings and the fixer's answers, so the loop converges.
        """
        suffix = str(round_) if round_ else ""
        checks = self.check_results(t, round_)
        sensitive = self.risk(t, plan, round_)
        planned = list(plan.get("files") or [])
        changed = git_out(t["worktree"], "diff", "--cached", "--name-only", t["base"]).splitlines()
        outside = out_of_plan(changed, planned, self.scope_ignore)
        failing = [c["cmd"] for c in checks if not c["ok"]]
        if failing:  # red checks: the fixer gets their output first; reviewers read code that passes
            self.run.log(f"{t['ref']} review{suffix}: checks failing ({', '.join(failing)}): fixing before review")
            result = {"round": round_, "findings": [], "preExisting": [], "unmetCriteria": [], "checks": checks,
                      "refuted": [], "sensitive": sensitive, "outOfPlan": outside, "skipped": "checks"}
            (self.run.dir / f"{t['id']}.round{round_}.json").write_text(json.dumps(result, indent=2))
            return result
        if round_ and trees[0]:  # a re-review judges only the files this fix touched
            since = set(git_out(t["worktree"], "diff", "--cached", "--name-only", trees[0]).splitlines())
            scope_files = [f for f in outside if f in since]
        else:
            scope_files = outside
        scope_block = scope_files_note(scope_files)
        lenses = self.lenses + (["security"] if sensitive and "security" not in self.lenses else [])
        scope_lens = "spec" if "spec" in lenses else lenses[0]  # who judges the files outside the plan
        votes = self.votes if sensitive else 1
        scope = self.scope_note(round_, trees[0], trees[1], prior or [], responses or [])
        checks_note = ("" if self.p.get("checks") else
                       "\nNo checks are configured: run the ones the repository instructions name, and report a failing one as a blocker.")
        review_note = self.review_file_note(t)

        def lens_call(lens: str):
            brief = render(f"lenses/{lens}", checksNote=checks_note, sensitive=", ".join(sensitive))
            if lens == scope_lens:
                brief += scope_block
            prompt = render("review", **self.values(t, plan, lens=lens, lensBrief=brief, scopeNote=scope, reviewFileNote=review_note))
            return self.call(t, f"review{suffix}-{lens}", "review", prompt, "findings")

        with ThreadPoolExecutor(len(lenses)) as pool:
            results = list(pool.map(lens_call, lenses))
        if all(r is None for r in results):
            return None
        found = dedupe([dict(f, lens=lens) for lens, r in zip(lenses, results) if r for f in r["findings"]])
        pre_existing = [f for f in found if f.get("introduced") is False]
        found = [f for f in found if f.get("introduced") is not False]
        to_check = [f for f in found if f["severity"] != "minor"]
        refuted = []
        if self.verify and to_check:
            with ThreadPoolExecutor(min(len(to_check), 8)) as pool:
                verdicts = list(pool.map(lambda i_f: self.verdict(t, plan, f"verify{suffix}-{i_f[0] + 1}", i_f[1], votes),
                                         enumerate(to_check)))
            for f, v in zip(to_check, verdicts):
                if v is None:
                    f["verified"] = False  # every skeptic failed: keep the finding, unconfirmed
                elif v["verdict"] == "refuted":
                    f.update(refuted=v["reason"], votes=v["votes"])
                    refuted.append(f)
                else:
                    f.update(severity=v["severity"], verified=True, reason=v["reason"], votes=v["votes"])
            found = [f for f in found if "refuted" not in f]
            if refuted:
                self.run.log(f"{t['ref']} review{suffix}: {len(refuted)} finding(s) refuted by the skeptics")
        result = {"round": round_, "findings": found, "preExisting": pre_existing, "unmetCriteria": [], "checks": checks,
                  "refuted": refuted, "sensitive": sensitive, "outOfPlan": outside}
        (self.run.dir / f"{t['id']}.round{round_}.json").write_text(json.dumps(result, indent=2))
        return result

    # --- build: implement, then review and fix until clean -------------------------

    def fix_list(self, review: dict) -> tuple[list[dict], str]:
        todo = [f for f in review["findings"] if f["severity"] != "minor"]
        lines = (["The checks fail, so no reviewer has read this version of the change. Make them pass; the review follows."]
                 if review.get("skipped") else [])
        lines += [f"{n}. [{f['severity']}, {f.get('class') or 'unclassified'}; {f['lens']} review] " + finding_text(f).replace("\n", "\n   ")
                 for n, f in enumerate(todo, 1)]
        lines += [f"- [unmet criterion] {c}" for c in review["unmetCriteria"]]
        lines += [f"- [failing check] `{c['cmd']}`" + (f"\n  Output (tail):\n  " + c.get("tail", "").replace("\n", "\n  ") if c.get("tail") else "")
                  for c in review["checks"] if not c["ok"]]
        return todo, "\n".join(lines)

    def build(self, t: dict, plan: dict) -> dict:
        plan_md = plan.get("planMarkdown") or plan.get("summary", "")
        built = self.call(t, "implement", "implement", render("implement", **self.values(t, plan, plan=plan_md)), "build")
        if not built:
            return {"status": "needs-human", "why": "implementer failed", "rounds": 0}
        if built.get("stuck"):
            return {"status": "needs-human", "why": built["stuck"], "rounds": 0}
        prev_tree = self.tree(t, "implement")
        review = self.review(t, plan, 0)
        ran = review if review and not review.get("skipped") else None  # the last review whose reviewers ran
        pre_existing = list(review["preExisting"]) if review else []
        rounds = 0
        last = None  # the last review that ran: (the tree it read, its findings for the fixer, the fixer's answers)
        while review and blocking(review) and rounds < self.max_fix:
            rounds += 1
            todo, lines = self.fix_list(review)
            fixed = self.call(t, f"fix{rounds}", "fix", render("fix", **self.values(t, plan, round=rounds, findings=lines)), "build")
            cur_tree = self.tree(t, f"fix{rounds}")
            if not review.get("skipped"):
                last = (prev_tree, todo, (fixed or {}).get("responses"))
            # Scoped to what changed since the last review that ran; with none yet, the whole change.
            review = self.review(t, plan, rounds, prior=last[1] if last else None, responses=last[2] if last else None,
                                 trees=(last[0], cur_tree) if last else ("", cur_tree))
            prev_tree = cur_tree
            if not review:
                break
            if review.get("skipped"):
                continue
            ran = review
            pre_existing += review["preExisting"]
            last_keys = {class_key(f) for f in last[1]} if last else set()
            repeated = [f for f in review["findings"] if f["severity"] != "minor" and class_key(f) in last_keys]
            if repeated and blocking(review):
                f = repeated[0]
                self.run.update(ticket=(t["id"], {"rounds": rounds}))
                return {"status": "needs-human", "rounds": rounds, "review": review, "preExisting": pre_existing,
                        "why": f"the same issue survived a fix ({f.get('class') or f['issue'][:60]}"
                               f"{' in ' + f['file'] if f.get('file') else ''}); rethink the approach"}
        self.run.update(ticket=(t["id"], {"rounds": rounds}))
        if not review:
            return {"status": "needs-human", "why": "reviewer failed", "rounds": rounds, "preExisting": pre_existing}
        if review.get("skipped"):
            failing = ", ".join(c["cmd"] for c in review["checks"] if not c["ok"])
            return {"status": "needs-human", "why": f"checks still failing after {rounds} fix round(s): {failing}",
                    "rounds": rounds, "review": review, "preExisting": pre_existing, "lastRan": ran}
        if blocking(review):
            return {"status": "needs-human", "why": f"still failing review after {rounds} fix round(s)", "rounds": rounds,
                    "review": review, "preExisting": pre_existing}
        return {"status": "ready", "why": "", "rounds": rounds, "review": review, "preExisting": pre_existing}

    # --- handoff --------------------------------------------------------------

    def handoff(self, t: dict, plan: dict, res: dict) -> dict:
        review = res.get("review")
        last = last_review_text(review, res.get("lastRan"))
        status = res["status"] + (f" ({res['why']})" if res["why"] else "")
        pre = dedupe(res.get("preExisting") or [])
        res["preExisting"] = pre
        sensitive = self.ticket_state(t).get("sensitive") or []
        diff = git_out(t["worktree"], "diff", "--cached", t["base"])
        if len(diff) > 40000:
            diff = diff[:40000] + "\n… (diff truncated; read the rest with git if you can)"
        h = self.call(t, "handoff", "handoff", render(
            "handoff", **self.values(t, status=status, plan=plan.get("planMarkdown") or plan.get("summary", ""),
                                     stat=git_out(t["worktree"], "diff", "--cached", "--stat", t["base"]), diff=diff,
                                     lastReview=last, whyNote=f"Why it needs a human: {res['why']}." if res["why"] else "",
                                     summaryNote="; when it needs a human, say why first" if res["why"] else "",
                                     preExistingNote=("Problems the reviewers found in code this change did not touch (listed "
                                                      "separately as pre-existing; do not repeat them in openFindings):\n"
                                                      + "\n".join(f"- {finding_text(f).splitlines()[0]}" for f in pre)) if pre else "",
                                     riskNote=(f"This change touches sensitive paths ({', '.join(sensitive)}); say in `risk` what a "
                                               "human reviewer must check there.") if sensitive else "")), "handoff")
        if res["status"] == "ready" and self.commits:
            res["commit"], error = commit_ticket(t["worktree"], ((h or {}).get("commitMessage") or "").strip() or t["name"], t["base"])
            if error:
                res.update(status="needs-human", why=f"the handoff commit failed: {error}")
        self.write_handoff(t, res, h, sensitive, (review or {}).get("outOfPlan"))
        return h or {}

    def write_handoff(self, t: dict, res: dict, h: dict | None, sensitive: list[str] | None = None,
                      out_of_plan: list[str] | None = None) -> None:
        write_handoff_file(Path(self.p["dir"]), self.run.id, t, res, h, sensitive, out_of_plan)

    def chain(self, t: dict, plan: dict) -> dict:
        try:
            res = self.build(t, plan)
        except Exception as e:  # noqa: BLE001
            self.run.log(f"{t['ref']} build crashed: {e}")
            res = {"status": "needs-human", "why": f"build crashed: {e}", "rounds": 0}
        h = self.handoff(t, plan, res)
        self.run.update(ticket=(t["id"], {"stage": "done", "status": res["status"], "why": res["why"],
                                          **({"commit": res.get("commit") or ""} if self.commits else {})}))
        self.run.log(f"{t['ref']} {res['status']}{': ' + res['why'] if res['why'] else ''}")
        return {"t": t, **res, "handoff": h}

    # --- stacking: a waiting ticket moves onto the finished ones it waits on -------

    def restack(self, t: dict, on: set, order: list[str], n: int) -> tuple[dict, dict | None, str]:
        """Move waiting ticket `t` onto the finished tickets `on` and plan it there:
        (ticket, plan, why it needs a human). `n` counts the ticket's moves in this run. The stack
        step leaves alone a ticket that a stopped run already moved, so a resume is safe."""
        by = {x["id"]: x for x in self.run.tickets}
        onto = max(on, key=order.index)  # the one that finished last
        needs = sorted(on - {onto})
        moved: dict = {}
        for _ in range(RESOLVE_ROUNDS):
            try:
                moved = self.stack(t["id"], onto, needs) or {}
            except Exception as e:  # noqa: BLE001
                moved = {"ok": False, "reason": str(e)}
            ask = moved.get("resolve")
            if not ask:
                break
            # Blockers on separate branches go in a line first; a conflict or a failing check
            # there gets a resolve agent, then the stack step carries on from where it stopped.
            x = by[ask["id"]]
            k = self.ticket_state(x).get("resolves", 0) + 1  # a new label each time: a resume never reuses an old answer
            self.run.update(ticket=(x["id"], {"resolves": k}))
            self.run.log(f"{x['ref']}: {ask['why']}; a resolve agent combines them for {t['ref']}")
            self.call(x, f"resolve{k}", "fix", ask["prompt"], "build")
            self.run.update(ticket=(x["id"], {"stage": "done"}))
        if not moved.get("ok"):
            why = moved.get("reason") or (f"{moved['resolve']['ref']} still {moved['resolve']['why']}" if moved.get("resolve")
                                          else "the stack step failed")
            return t, None, f"cannot build on {by[onto]['ref']}: {why}"
        t = next((x for x in self.run.tickets if x["id"] == t["id"]), t)
        t["base"] = moved["base"]
        self.run.log(f"{t['ref']}: moved onto {by[onto]['ref']} ({t['base']}); planning it on the new code")
        plan = self.plan(t, f"plan{n + 1}")  # a new label per move: any earlier plan was made on another base
        if not plan or not plan["feasible"]:
            return t, None, (plan.get("question") or plan["summary"]) if plan else "the planner failed"
        return t, plan, ""

    # --- the run --------------------------------------------------------------

    def execute(self) -> dict:
        tickets = self.run.tickets
        self.run.update(started=now(), finished=None)
        self.run.log(f"run {self.run.id}: {len(tickets)} ticket(s), tools " +
                     ", ".join(f"{s}={self.run.tool(s)}" for s in STAGES))

        refs = {t["id"]: t["ref"] for t in tickets}
        # A ticket that waits for others of this run is planned once, after it is moved onto their
        # code: a plan made before that code exists is thrown away, and a false "infeasible" would stop it.
        blockers_of = lambda t: {i for i in t.get("waitFor") or [] if i in refs} if self.stacking else set()  # noqa: E731
        with ThreadPoolExecutor(self.parallel) as pool:
            plans = list(pool.map(lambda t: None if blockers_of(t) else self.plan(t), tickets))

        batch, deferred, needs_human, taken = [], [], [], {}
        later = []  # {t, plan, on}: tickets that wait for others of this run (when it stacks)
        exclusive = self.p.get("exclusive") or []
        names = lambda ids: ", ".join(refs.get(i, f"#{i}") for i in sorted(ids))  # noqa: E731
        place = lambda claim: claim.split(":", 1)[1]  # noqa: E731

        def defer(t: dict, reason: str) -> None:
            deferred.append({"id": t["id"], "ref": t["ref"], "reason": reason})
            self.run.update(ticket=(t["id"], {"stage": "done", "status": "deferred", "why": reason}))

        def human(t: dict, question: str) -> None:
            needs_human.append({"id": t["id"], "ref": t["ref"], "name": t["name"], "question": question})
            self.run.update(ticket=(t["id"], {"stage": "done", "status": "needs-human", "why": question}))

        def hold(t: dict, plan: dict, on: set) -> None:
            later.append({"t": t, "plan": plan, "on": on})
            why = f"waits for {names(on) if on else 'a free slot'}"
            self.run.update(ticket=(t["id"], {"stage": "waiting", "why": why}))
            self.run.log(f"{t['ref']}: {why}")

        for t, plan in zip(tickets, plans):
            if blockers_of(t):
                hold(t, None, blockers_of(t))  # restack plans it
                continue
            if not plan:
                defer(t, "planner failed; run it again")
                continue
            if not plan["feasible"]:
                human(t, plan.get("question") or plan["summary"])
                continue
            mine = claims(plan, exclusive)
            clashes = [c for c in sorted(mine) if c in taken]
            if clashes and self.stacking:
                hold(t, plan, {taken[c]["id"] for c in clashes})
            elif clashes:
                defer(t, f"overlaps {taken[clashes[0]]['ref']} on {place(clashes[0])}")
            elif len(batch) >= self.parallel and self.stacking:
                hold(t, plan, set())
            elif len(batch) >= self.parallel:
                defer(t, f"over maxParallel ({self.parallel})")
            else:
                for c in mine:
                    taken[c] = t
                batch.append((t, plan))
        self.run.log("batch: " + (", ".join(t["ref"] for t, _ in batch) or "none"))
        if deferred:
            self.run.log("deferred: " + "; ".join(f"{d['ref']} ({d['reason']})" for d in deferred))
        if needs_human:
            self.run.log("needs a human: " + ", ".join(n["ref"] for n in needs_human))

        if self.run.plan_only:
            for t in [t for t, _ in batch] + [w["t"] for w in later]:
                self.run.update(ticket=(t["id"], {"stage": "planned", "status": "planned"}))
            summary = {"stoppedAfter": "plan", "plans": [{"id": t["id"], "ref": t["ref"], **plan} for t, plan in batch],
                       "waiting": [{"id": w["t"]["id"], "ref": w["t"]["ref"], "waitsFor": sorted(w["on"])} for w in later],
                       "deferred": deferred, "needsHuman": needs_human}
            self.run.update(summary=summary, finished=now())
            self.run.log(f"run {self.run.id} finished")
            return summary

        # Tickets run with no barrier between them. Each time one finishes, the tickets that waited
        # for it wake: moved onto its commit, planned on the new code, and started unless they
        # now overlap another running ticket.
        results = {n["id"]: {"status": "needs-human"} for n in needs_human}  # id -> the finished ticket's result
        results.update({d["id"]: {"status": "deferred"} for d in deferred})
        order: list[str] = []        # ids in the order they finished
        holding: dict = {}           # claim -> the running ticket that holds it
        running: dict = {}           # future -> (ticket, its claims)
        stacks: dict = {}            # id -> how many times it was moved onto other tickets

        with ThreadPoolExecutor(self.parallel) as pool:
            def start(t: dict, plan: dict) -> None:
                mine = claims(plan, exclusive)
                for c in mine:
                    holding[c] = t
                running[pool.submit(self.chain, t, plan)] = (t, mine)

            def wake() -> None:
                for w in list(later):
                    if len(running) >= self.parallel:
                        break
                    if not all(i in results for i in w["on"]):
                        continue
                    later.remove(w)
                    t, plan = w["t"], w["plan"]
                    failed = {i for i in w["on"] if results[i]["status"] != "ready"}
                    if failed:
                        results[t["id"]] = {"status": "deferred"}
                        defer(t, f"waits on {names(failed)}, which did not finish ready")
                        continue
                    if w["on"]:
                        n = stacks[t["id"]] = stacks.get(t["id"], 0) + 1
                        t, plan, why = self.restack(t, w["on"], order, n)
                        if why:
                            results[t["id"]] = {"status": "needs-human"}
                            human(t, why)
                            continue
                    clashes = [c for c in sorted(claims(plan, exclusive)) if c in holding]
                    if clashes:
                        self.run.log(f"{t['ref']}: overlaps a running ticket on {place(clashes[0])}")
                        hold(t, plan, {holding[c]["id"] for c in clashes})
                        continue
                    start(t, plan)

            for t, plan in batch:
                start(t, plan)
            wake()
            while running:
                for f in wait(running, return_when=FIRST_COMPLETED)[0]:
                    t, mine = running.pop(f)
                    try:
                        r = f.result()
                    except Exception as e:  # noqa: BLE001 - one crashed ticket must not stop the others
                        r = {"t": t, "status": "needs-human", "why": f"crashed: {e}", "rounds": 0, "handoff": {}}
                        self.run.update(ticket=(t["id"], {"stage": "done", "status": r["status"], "why": r["why"]}))
                        self.run.log(f"{t['ref']} crashed: {e}")
                    results[t["id"]] = r
                    order.append(t["id"])
                    for c in mine:
                        if holding.get(c) is t:
                            del holding[c]
                wake()
        for w in later:
            defer(w["t"], f"waits on {names(w['on'])}, which did not finish")

        position = {t["id"]: i for i, t in enumerate(tickets)}
        base = {t["id"]: t["base"] for t in self.run.tickets}  # a finished ticket may have been put on another since
        done = sorted((r for r in results.values() if "t" in r), key=lambda r: position[r["t"]["id"]])
        summary = {
            "ready": [{"id": r["t"]["id"], "ref": r["t"]["ref"], "name": r["t"]["name"], "branch": r["t"]["branch"],
                       "base": base.get(r["t"]["id"], r["t"]["base"]),
                       "rounds": r["rounds"], "risk": "high" if (r.get("review") or {}).get("sensitive") else "normal",
                       "preExisting": len(r.get("preExisting") or []),
                       **{k: r["handoff"].get(k) for k in ("commitMessage", "prTitle", "prChanges")}}
                      for r in done if r["status"] == "ready"],
            "needsHuman": needs_human + [{"id": r["t"]["id"], "ref": r["t"]["ref"], "name": r["t"]["name"], "question": r["why"]}
                                         for r in done if r["status"] != "ready"],
            "deferred": deferred,
        }
        self.run.update(summary=summary, finished=now())
        self.run.log(f"run {self.run.id} finished")
        return summary


def status_text(state: dict) -> str:
    """A short human report of a run, finished or not."""
    out = [f"Run {state.get('id')} ({'finished ' + state['finished'] if state.get('finished') else 'running'}; started {state.get('started') or state.get('created')})"]
    for tid, t in (state.get("tickets") or {}).items():
        extra = (f", {t['rounds']} fix round(s)" if t.get("rounds") else "") + (", HIGH RISK" if t.get("risk") == "high" else "")
        out.append(f"- {t.get('ref', tid)} {t.get('name', '')}: {t.get('status') or t.get('stage')}{extra}"
                   + (f" ({t['why']})" if t.get("why") else ""))
    s = state.get("summary")
    if s and "ready" in s:
        out += ["", "Ready to ship: " + (", ".join(r["ref"] for r in s["ready"]) or "none")]
    return "\n".join(out)
