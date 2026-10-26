"""The sprint loop: plan every ticket, batch the ones that do not overlap, then implement ->
independent review -> fix (bounded rounds) -> handoff per ticket, in parallel.

Each agent call goes through an adapter (`adapters.run_agent` by default), which runs Claude
Code, Codex or Cursor headless in the ticket's worktree and returns the stage's JSON. Every
finished call is saved under `<agent-loop dir>/runs/<run id>/`, so a resumed run skips it.
Agents never write outside their worktree: the runner writes the plan and handoff files.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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


def blocking(review: dict) -> bool:
    return (any(f["severity"] != "minor" for f in review["findings"])
            or bool(review["unmetCriteria"]) or any(not c["ok"] for c in review["checks"]))


def fingerprint(worktree: str) -> str:
    """The worktree's tracked and staged content. Untracked files are left out: running tests
    creates caches (`__pycache__`, coverage files) that are not an edit."""
    out = []
    for args in (["status", "--porcelain", "--untracked-files=no"], ["diff"], ["diff", "--cached"]):
        r = subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True)
        out.append(r.stdout)
    return "\n".join(out)


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
    lock: threading.Lock = field(default_factory=threading.Lock)

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
            (self.dir / "run.json").write_text(json.dumps(state, indent=2))

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
    def __init__(self, run: Run, adapter: Adapter):
        self.run, self.adapter = run, adapter
        self.p = run.project
        self.checks = checks_line(self.p.get("checks") or [])
        self.parallel = max(1, min(int(self.p.get("maxParallel") or 10), 16))
        self.max_fix = max(0, min(int(self.p.get("maxFixRounds", 2)), 4))

    # --- one agent call, cached -------------------------------------------------

    def call(self, t: dict, label: str, stage: str, prompt: str, schema: str) -> dict | None:
        cached = self.run.dir / f"{t['id']}.{label}.json"
        if cached.is_file():
            return json.loads(cached.read_text())
        tool = self.run.tool(stage)
        self.run.update(ticket=(t["id"], {"stage": label}))
        self.run.log(f"{t['ref']} {label} ({tool})")
        mode = MODES[stage]
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

    def plan(self, t: dict) -> dict | None:
        plan = self.call(t, "plan", "plan", render("plan", **self.values(t)), "plan")
        if plan and plan.get("planMarkdown"):
            plans = Path(self.p["dir"]) / "plans"
            plans.mkdir(parents=True, exist_ok=True)
            (plans / f"{t['id']}.md").write_text(plan["planMarkdown"].strip() + "\n")
        return plan

    def review(self, t: dict, plan: dict, round_: int) -> dict | None:
        note = f"This is re-review round {round_}: judge the current state, not the history." if round_ else ""
        label = f"review{round_}" if round_ else "review"
        return self.call(t, label, "review", render("review", **self.values(t, plan, roundNote=note)), "review")

    def build(self, t: dict, plan: dict) -> dict:
        plan_md = plan.get("planMarkdown") or plan.get("summary", "")
        built = self.call(t, "implement", "implement", render("implement", **self.values(t, plan, plan=plan_md)), "build")
        if not built:
            return {"status": "needs-human", "why": "implementer failed", "rounds": 0}
        if built.get("stuck"):
            return {"status": "needs-human", "why": built["stuck"], "rounds": 0}
        review = self.review(t, plan, 0)
        rounds = 0
        while review and blocking(review) and rounds < self.max_fix:
            rounds += 1
            todo = [f for f in review["findings"] if f["severity"] != "minor"]
            lines = [f"- [{f['severity']}] " + (f"{f['file']}{':' + str(f['line']) if f.get('line') else ''}: " if f.get("file") else "")
                     + f["issue"] + (f" (suggested: {f['fix']})" if f.get("fix") else "") for f in todo]
            lines += [f"- [unmet criterion] {c}" for c in review["unmetCriteria"]]
            lines += [f"- [failing check] `{c['cmd']}`" for c in review["checks"] if not c["ok"]]
            self.call(t, f"fix{rounds}", "fix", render("fix", **self.values(t, plan, round=rounds, findings="\n".join(lines))), "build")
            review = self.review(t, plan, rounds)
        self.run.update(ticket=(t["id"], {"rounds": rounds}))
        if not review:
            return {"status": "needs-human", "why": "reviewer failed", "rounds": rounds}
        if blocking(review):
            return {"status": "needs-human", "why": f"still failing review after {rounds} fix round(s)", "rounds": rounds, "review": review}
        return {"status": "ready", "why": "", "rounds": rounds, "review": review}

    def handoff(self, t: dict, plan: dict, res: dict) -> dict:
        review = res.get("review")
        last = ("Last review findings:\n" + "\n".join(f"- [{f['severity']}] {f.get('file') or ''} {f['issue']}" for f in review["findings"])
                if review and review["findings"] else "The last review raised nothing.")
        status = res["status"] + (f" ({res['why']})" if res["why"] else "")
        h = self.call(t, "handoff", "handoff", render(
            "handoff", **self.values(t, status=status, plan=plan.get("planMarkdown") or plan.get("summary", ""),
                                     lastReview=last, whyNote=f"Why it needs a human: {res['why']}." if res["why"] else "",
                                     summaryNote="; when it needs a human, say why first" if res["why"] else "")), "handoff")
        self.write_handoff(t, res, h)
        return h or {}

    def write_handoff(self, t: dict, res: dict, h: dict | None) -> None:
        h = h or {"summary": f"Handoff agent failed. {res['why']}".strip(), "decisions": [], "followUps": [], "openFindings": [],
                  "commitMessage": "", "prTitle": t["name"], "prChanges": []}
        bullets = lambda xs: "\n".join(f"- {x}" for x in xs) or "- None"  # noqa: E731
        text = "\n".join([
            "---", f"id: {t['id']}", f"title: {t['name']}", f"status: {res['status']}", f"branch: {t['branch']}",
            f"base: {re.sub(r'^origin/', '', t['base'])}", f"worktree: {t['worktree']}", f"card: {t.get('url', '')}",
            f"updated: {now()}", f"run: {self.run.id}", "---", "",
            "## Summary", h["summary"].strip(), "",
            "## Decisions", bullets(h["decisions"]), "",
            "## Follow-ups", bullets(h["followUps"]), "",
            "## Open review findings", bullets(h["openFindings"]), "",
            "## Commit message", h["commitMessage"].strip(), "",
            "## PR title", h["prTitle"].strip(), "",
            "## PR changes", bullets(h["prChanges"]), "",
        ])
        d = Path(self.p["dir"]) / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{t['id']}.md").write_text(text)

    def chain(self, t: dict, plan: dict) -> dict:
        try:
            res = self.build(t, plan)
        except Exception as e:  # noqa: BLE001
            self.run.log(f"{t['ref']} build crashed: {e}")
            res = {"status": "needs-human", "why": f"build crashed: {e}", "rounds": 0}
        h = self.handoff(t, plan, res)
        self.run.update(ticket=(t["id"], {"stage": "done", "status": res["status"], "why": res["why"]}))
        self.run.log(f"{t['ref']} {res['status']}{': ' + res['why'] if res['why'] else ''}")
        return {"t": t, **res, "handoff": h}

    # --- the run --------------------------------------------------------------

    def execute(self) -> dict:
        tickets = self.run.tickets
        self.run.update(started=now(), finished=None)
        self.run.log(f"run {self.run.id}: {len(tickets)} ticket(s), tools " +
                     ", ".join(f"{s}={self.run.tool(s)}" for s in STAGES))

        with ThreadPoolExecutor(self.parallel) as pool:
            plans = list(pool.map(self.plan, tickets))

        batch, deferred, needs_human, taken = [], [], [], {}
        exclusive = self.p.get("exclusive") or []
        for t, plan in zip(tickets, plans):
            if not plan:
                deferred.append({"id": t["id"], "ref": t["ref"], "reason": "planner failed; run it again"})
            elif not plan["feasible"]:
                needs_human.append({"id": t["id"], "ref": t["ref"], "name": t["name"], "question": plan.get("question") or plan["summary"]})
            elif len(batch) >= self.parallel:
                deferred.append({"id": t["id"], "ref": t["ref"], "reason": f"over maxParallel ({self.parallel})"})
            else:
                mine = claims(plan, exclusive)
                clash = next((c for c in sorted(mine) if c in taken), None)
                if clash:
                    deferred.append({"id": t["id"], "ref": t["ref"], "reason": f"overlaps {taken[clash]} on {clash.split(':', 1)[1]}"})
                    continue
                for c in mine:
                    taken[c] = t["ref"]
                batch.append((t, plan))
        for d in deferred:
            self.run.update(ticket=(d["id"], {"stage": "done", "status": "deferred", "why": d["reason"]}))
        for n in needs_human:
            self.run.update(ticket=(n["id"], {"stage": "done", "status": "needs-human", "why": n["question"]}))
        self.run.log("batch: " + (", ".join(t["ref"] for t, _ in batch) or "none"))
        if deferred:
            self.run.log("deferred: " + "; ".join(f"{d['ref']} ({d['reason']})" for d in deferred))
        if needs_human:
            self.run.log("needs a human: " + ", ".join(n["ref"] for n in needs_human))

        if self.run.plan_only:
            for t, _ in batch:
                self.run.update(ticket=(t["id"], {"stage": "planned", "status": "planned"}))
            summary = {"stoppedAfter": "plan", "plans": [{"id": t["id"], "ref": t["ref"], **plan} for t, plan in batch],
                       "deferred": deferred, "needsHuman": needs_human}
        else:
            with ThreadPoolExecutor(self.parallel) as pool:
                done = list(pool.map(lambda tp: self.chain(*tp), batch))
            summary = {
                "ready": [{"id": r["t"]["id"], "ref": r["t"]["ref"], "name": r["t"]["name"], "branch": r["t"]["branch"],
                           "rounds": r["rounds"], **{k: r["handoff"].get(k) for k in ("commitMessage", "prTitle", "prChanges")}}
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
        extra = f", {t['rounds']} fix round(s)" if t.get("rounds") else ""
        out.append(f"- {t.get('ref', tid)} {t.get('name', '')}: {t.get('status') or t.get('stage')}{extra}"
                   + (f" ({t['why']})" if t.get("why") else ""))
    s = state.get("summary")
    if s and "ready" in s:
        out += ["", "Ready to ship: " + (", ".join(r["ref"] for r in s["ready"]) or "none")]
    return "\n".join(out)
