#!/usr/bin/env python3
"""agent-loop CLI: project brief, ticket selection, worktrees, handoffs and board moves.

Everything the skills and the sprint workflow need from the outside world goes through here,
so the workflow script itself never touches the filesystem or the network.

A project is any directory holding `.agent-loop/config.json`; commands find it by walking up
from the current directory, so every worktree under the project resolves to the same one.

Credentials come from the environment, never from a file:

    TRELLO_KEY=...  TRELLO_TOKEN=...

Commands:

    init [--root DIR]            scaffold .agent-loop/ in DIR (default: cwd)
    brief [--hook] [--json]      the project brief (--hook: SessionStart JSON, silent outside a project)
    tickets [ID ...] [--all]     ticket objects as JSON (default: the ready ones)
    prepare ID [ID ...] [--force]  create or reuse each ticket's worktree and branch, run setup
                                 (--force: also for blocked tickets, on the configured base)
    handoffs [--status S]        handoff notes as JSON
    mark ID STATUS [--pr URL]    set a handoff's status (ready, needs-human, shipped)
    move ID LIST_KEY             move a card to a configured list (ready, doing, review)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TRELLO = "https://api.trello.com/1"
NET_TIMEOUT = 10

DEFAULT_CONFIG = {
    "name": "",
    "repo": "",
    "base": "origin/main",
    "mainWorktree": "main",
    "branch": "{type}/{slug}",
    "typeByLabel": {"Bug": "fix", "Chore": "chore"},
    "defaultType": "feat",
    "setup": [],
    "checks": [],
    "exclusive": [],
    "contextFiles": [],
    "maxParallel": 10,
    "maxFixRounds": 2,
    "prDraft": False,
    "trello": {
        "board": "",
        "lists": {"ready": "Sprint Backlog", "doing": "In Progress", "review": "Code Review"},
        "show": ["Sprint Backlog", "In Progress", "Code Review"],
        "done": ["Done"],
    },
}


# --- project ----------------------------------------------------------------

@dataclass
class Project:
    root: Path
    config: dict

    @property
    def dir(self) -> Path:
        return self.root / ".agent-loop"

    def worktree(self, card_id: int) -> Path:
        return self.root / str(card_id)


def merged(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = merged(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def find_project(start: Path | None = None) -> Project | None:
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        cfg = d / ".agent-loop" / "config.json"
        if cfg.is_file():
            return Project(d, merged(DEFAULT_CONFIG, json.loads(cfg.read_text())))
    return None


def require_project() -> Project:
    p = find_project()
    if not p:
        raise SystemExit("No .agent-loop/config.json above the current directory; run `agentloop.py init`.")
    return p


# --- outside world (patched in tests) -------------------------------------------

def trello(method: str, path: str, **params):
    """Trello REST call. `path` must NOT carry a query string: params are encoded here, once."""
    params.update(key=os.environ["TRELLO_KEY"], token=os.environ["TRELLO_TOKEN"])
    qs = urllib.parse.urlencode(params)
    try:
        if method == "GET":
            req = urllib.request.Request(f"{TRELLO}/{path}?{qs}")
        else:
            req = urllib.request.Request(f"{TRELLO}/{path}", data=qs.encode(), method=method)
        with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Trello HTTP {e.code} on {method} {path}: {e.read().decode()[:200]}") from e


def gh_json(*args: str):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout or "null")


def git(root: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check and out.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def have_trello() -> bool:
    return bool(os.environ.get("TRELLO_KEY") and os.environ.get("TRELLO_TOKEN"))


# --- board ------------------------------------------------------------------

BLOCKERS = re.compile(r"###\s*Blockers\s*\n(.*?)(?=\n###|\Z)", re.S)
CARD_REF = re.compile(r"#(\d+)")
BRANCH_ID = re.compile(r"^(?:[\w.-]+/)?(\d+)(?:[-_]|$)")


def blockers_of(desc: str) -> list[int]:
    m = BLOCKERS.search(desc or "")
    return [int(n) for n in CARD_REF.findall(m.group(1))] if m else []


def branch_id(branch: str) -> int | None:
    m = BRANCH_ID.match(branch or "")
    return int(m.group(1)) if m else None


def slug_of(card: dict) -> str:
    """The card URL's own slug (`816-every-step-belongs-to-an-agent`), else one built from the name."""
    tail = (card.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
    if tail.startswith(f"{card['idShort']}-"):
        return tail[:70].rstrip("-")
    words = re.sub(r"[^a-z0-9]+", "-", card["name"].lower()).strip("-")
    return f"{card['idShort']}-{words}"[:70].rstrip("-")


@dataclass
class Snapshot:
    """Everything the brief and the ticket selection read, fetched once."""

    lists: dict[str, str]          # list id -> name
    cards: list[dict]
    archived: set[int]
    open_prs: list[dict]
    merged_prs: list[dict]
    worktrees: dict[str, str]      # branch -> path
    errors: list[str]

    def by_id(self) -> dict[int, dict]:
        return {c["idShort"]: c for c in self.cards}

    def list_name(self, card: dict) -> str:
        return self.lists.get(card["idList"], "?")

    def pr_for(self, card_id: int) -> dict | None:
        return next((p for p in self.open_prs if branch_id(p["headRefName"]) == card_id), None)

    def merged_ids(self) -> set[int]:
        return {i for p in self.merged_prs if (i := branch_id(p["headRefName"])) is not None}

    def worktree_for(self, card_id: int) -> tuple[str, str] | None:
        return next(((b, p) for b, p in self.worktrees.items() if branch_id(b) == card_id), None)


def snapshot(p: Project) -> Snapshot:
    s = Snapshot({}, [], set(), [], [], {}, [])
    board = p.config["trello"]["board"]
    if not have_trello():
        s.errors.append("Board unavailable: export TRELLO_KEY and TRELLO_TOKEN before starting the session.")
    elif board:
        try:
            s.lists = {x["id"]: x["name"] for x in trello("GET", f"boards/{board}/lists", fields="name")}
            s.cards = trello("GET", f"boards/{board}/cards", fields="name,idShort,idList,desc,labels,url,dateLastActivity", limit=1000)
            s.archived = {c["idShort"] for c in trello("GET", f"boards/{board}/cards/closed", fields="idShort", limit=1000)}
        except (RuntimeError, OSError) as e:
            s.errors.append(f"Board unavailable: {e}")
    if p.config["repo"]:
        try:
            fields = "number,title,headRefName,baseRefName,isDraft,reviewDecision,url"
            s.open_prs = gh_json("pr", "list", "-R", p.config["repo"], "--state", "open", "--limit", "50", "--json", fields)
            s.merged_prs = gh_json("pr", "list", "-R", p.config["repo"], "--state", "merged", "--limit", "30", "--json", "number,title,headRefName,mergedAt")
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            s.errors.append(f"GitHub unavailable: {e}")
    try:
        path = None
        for line in git(p.root, "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                path = line[9:]
            elif line.startswith("branch refs/heads/") and path:
                s.worktrees[line[18:]] = path
    except RuntimeError as e:
        s.errors.append(str(e))
    return s


def classify(p: Project, s: Snapshot, card: dict) -> dict:
    """One ticket object: what it is, whether an agent may start it, and on which base."""
    lists_cfg = p.config["trello"]
    cards = s.by_id()
    done_lists = set(lists_cfg["done"])
    merged_ids = s.merged_ids()
    cid = card["idShort"]
    labels = [lab["name"] for lab in card.get("labels", []) if lab.get("name")]
    kind = next((t for lab, t in p.config["typeByLabel"].items() if lab in labels), p.config["defaultType"])

    open_blockers = []
    for b in blockers_of(card.get("desc", "")):
        other = cards.get(b)
        resolved = b in s.archived or b in merged_ids or (other and s.list_name(other) in done_lists)
        if not resolved and other is None:
            resolved = True  # a dead reference blocks nothing; the brief flags it
        if not resolved:
            open_blockers.append(b)

    base, status, reason = p.config["base"], "ready", ""
    wt, pr = s.worktree_for(cid), s.pr_for(cid)
    if pr:
        status, reason = "in-flight", f"PR #{pr['number']} open"
    elif wt:
        status, reason = "in-flight", f"worktree {wt[1]}"
    elif open_blockers:
        stack = [s.pr_for(b) for b in open_blockers]
        if len(open_blockers) == 1 and stack[0]:
            base, reason = f"origin/{stack[0]['headRefName']}", f"stacked on #{open_blockers[0]} (PR #{stack[0]['number']})"
        else:
            status, reason = "blocked", "waiting on " + ", ".join(f"#{b}" for b in open_blockers)

    branch = wt[0] if wt else p.config["branch"].format(type=kind, slug=slug_of(card), id=cid)
    return {
        "id": cid,
        "name": card["name"],
        "url": card.get("url", ""),
        "list": s.list_name(card),
        "labels": labels,
        "desc": card.get("desc", ""),
        "status": status,
        "reason": reason,
        "blockers": open_blockers,
        "branch": branch,
        "base": base,
        "worktree": wt[1] if wt else str(p.worktree(cid)),
    }


def ready_list_cards(p: Project, s: Snapshot) -> list[dict]:
    ready = p.config["trello"]["lists"]["ready"]
    return [c for c in s.cards if s.list_name(c) == ready]


# --- handoffs ----------------------------------------------------------------

def read_handoff(path: Path) -> dict:
    text = path.read_text()
    meta, body = {}, text
    if text.startswith("---\n") and "\n---" in text[4:]:
        head, body = text[4:].split("\n---", 1)
        for line in head.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    return {**meta, "id": int(meta.get("id") or path.stem), "path": str(path), "body": body.strip()}


def handoffs(p: Project) -> list[dict]:
    d = p.dir / "handoffs"
    if not d.is_dir():
        return []
    notes = [read_handoff(f) for f in d.glob("*.md")]
    return sorted(notes, key=lambda h: h.get("updated", ""), reverse=True)


def section(body: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\s*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    return m.group(1).strip() if m else ""


# --- brief -------------------------------------------------------------------

def read_capped(path: Path, cap: int) -> str:
    """The file without HTML comments, capped; a pointer instead when it is still the template."""
    if not path.is_file():
        return ""
    strip = lambda s: re.sub(r"<!--.*?-->", "", s, flags=re.S).strip()  # noqa: E731
    text = strip(path.read_text())
    template = PLUGIN_ROOT / "templates" / path.name
    if not text or (template.is_file() and text == strip(template.read_text())):
        return f"(not filled in yet: edit `{path}`)"
    return text if len(text) <= cap else text[:cap].rstrip() + f"\n\n… (truncated; read {path} for the rest)"


def brief(p: Project, s: Snapshot) -> str:
    cfg = p.config
    out = [f"# Project brief: {cfg['name'] or p.root.name}", ""]
    out += [f"Root `{p.root}`. Worktrees live at `{p.root}/<card id>`; the main checkout is `{p.root / cfg['mainWorktree']}`."]
    if cfg["contextFiles"]:
        out += ["Domain and conventions: " + ", ".join(f"`{f}`" for f in cfg["contextFiles"]) + " (in any worktree)."]
    for e in s.errors:
        out += [f"> {e}"]

    for title, name, cap in (("Sprint goal", "sprint.md", 4000), ("Client requirements", "requirements.md", 6000)):
        text = read_capped(p.dir / name, cap)
        if text:
            out += ["", f"## {title}", "", text]

    if s.cards:
        out += ["", "## Board"]
        shown = cfg["trello"]["show"]
        for name in shown:
            cards = [c for c in s.cards if s.list_name(c) == name]
            if not cards:
                continue
            out += ["", f"### {name} ({len(cards)})", ""]
            for c in cards:
                t = classify(p, s, c)
                tag = "" if name != cfg["trello"]["lists"]["ready"] else f" [{t['status']}{': ' + t['reason'] if t['reason'] else ''}]"
                labels = f" ({', '.join(t['labels'])})" if t["labels"] else ""
                out.append(f"- #{c['idShort']} {c['name']}{labels}{tag}")
        known = set(s.by_id()) | s.archived
        dead = sorted({b for c in s.cards for b in blockers_of(c.get("desc", "")) if b not in known})
        if dead:
            out += ["", "Dead blocker references: " + ", ".join(f"#{b}" for b in dead)]

    if s.open_prs or s.merged_prs:
        out += ["", "## Pull requests", ""]
        for pr in s.open_prs:
            state = "draft" if pr.get("isDraft") else (pr.get("reviewDecision") or "awaiting review").lower().replace("_", " ")
            base = "" if pr["baseRefName"] == "main" else f", onto `{pr['baseRefName']}`"
            out.append(f"- #{pr['number']} {pr['title']} (`{pr['headRefName']}`{base}, {state})")
        if s.merged_prs:
            out += ["", "Recently merged: " + "; ".join(f"#{pr['number']} {pr['title']}" for pr in s.merged_prs[:8])]

    if s.worktrees:
        out += ["", "## Local worktrees", ""]
        out += [f"- `{b}` at `{path}`" for b, path in s.worktrees.items()]

    notes = handoffs(p)
    if notes:
        out += ["", "## Agent handoffs (newest first)", ""]
        for h in notes[:12]:
            out.append(f"### #{h['id']} {h.get('title', '')} [{h.get('status', '?')}]")
            for part in ("Summary", "Decisions", "Follow-ups"):
                text = section(h["body"], part)
                if text:
                    out += [f"{part}: {text}" if "\n" not in text else f"{part}:\n{text}"]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


# --- commands ----------------------------------------------------------------

def cmd_init(a) -> None:
    root = Path(a.root or Path.cwd()).resolve()
    d = root / ".agent-loop"
    (d / "handoffs").mkdir(parents=True, exist_ok=True)
    (d / "plans").mkdir(exist_ok=True)
    for name in ("config.json", "sprint.md", "requirements.md"):
        target = d / name
        if not target.exists():
            target.write_text((PLUGIN_ROOT / "templates" / name).read_text())
            print(f"created {target}")
    print(f"Edit {d / 'config.json'}, then start a session anywhere under {root}.")


def cmd_brief(a) -> None:
    p = find_project() if a.hook else require_project()
    if not p:
        return
    text = brief(p, snapshot(p))
    try:
        (p.dir / "brief.md").write_text(text)
    except OSError:
        pass
    if a.hook:
        intro = ("The agent-loop plugin generated this brief for the project this session runs in. "
                 "It is current as of session start; refresh it with `/agent-loop:brief`.\n\n")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": intro + text}}))
    elif a.json:
        print(json.dumps({"brief": text, "path": str(p.dir / "brief.md")}))
    else:
        print(text)


def cmd_tickets(a) -> None:
    p = require_project()
    s = snapshot(p)
    if not s.cards:
        raise SystemExit("\n".join(s.errors) or "The board has no cards.")
    if a.ids:
        by = s.by_id()
        missing = [i for i in a.ids if i not in by]
        if missing:
            raise SystemExit("Not on the board: " + ", ".join(f"#{i}" for i in missing))
        picked = [classify(p, s, by[i]) for i in a.ids]
    else:
        picked = [classify(p, s, c) for c in ready_list_cards(p, s)]
        if not a.all:
            picked = [t for t in picked if t["status"] == "ready"]
    print(json.dumps(picked, indent=2))


def run_setup(p: Project, wt: Path) -> list[dict]:
    results = []
    for cmd in p.config["setup"]:
        cmd = cmd.format(root=p.root, main=p.root / p.config["mainWorktree"], worktree=wt)
        r = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True)
        results.append({"cmd": cmd, "ok": r.returncode == 0, "tail": (r.stdout + r.stderr).strip()[-300:]})
    return results


def cmd_prepare(a) -> None:
    p = require_project()
    s = snapshot(p)
    by = s.by_id()
    git(p.root, "fetch", "-q", "origin")
    prepared = []
    for cid in a.ids:
        if cid not in by:
            prepared.append({"id": cid, "error": "not on the board"})
            continue
        t = classify(p, s, by[cid])
        wt = Path(t["worktree"])
        if t["status"] == "blocked" and not a.force:
            prepared.append({**t, "error": t["reason"]})
            continue
        if not wt.exists():
            exists = git(p.root, "rev-parse", "--verify", "-q", f"refs/heads/{t['branch']}", check=False).strip()
            if exists:
                git(p.root, "worktree", "add", str(wt), t["branch"])
            else:
                git(p.root, "worktree", "add", "--no-track", "-b", t["branch"], str(wt), t["base"])
            t["created"] = True
        t["setup"] = run_setup(p, wt)
        prepared.append(t)
    print(json.dumps(prepared, indent=2))


def cmd_handoffs(a) -> None:
    p = require_project()
    notes = [h for h in handoffs(p) if not a.status or h.get("status") == a.status]
    print(json.dumps(notes, indent=2))


def cmd_mark(a) -> None:
    p = require_project()
    path = p.dir / "handoffs" / f"{a.id}.md"
    if not path.is_file():
        raise SystemExit(f"No handoff for #{a.id}")
    text = path.read_text()
    text = re.sub(r"^status:.*$", f"status: {a.status}", text, count=1, flags=re.M)
    if a.pr:
        text = re.sub(r"^pr:.*$\n?", "", text, flags=re.M)
        text = text.replace("\n---", f"\npr: {a.pr}\n---", 1)
    path.write_text(text)
    print(f"#{a.id} -> {a.status}")


def cmd_move(a) -> None:
    p = require_project()
    target = p.config["trello"]["lists"].get(a.list_key, a.list_key)
    board = p.config["trello"]["board"]
    lists = {x["name"]: x["id"] for x in trello("GET", f"boards/{board}/lists", fields="name")}
    if target not in lists:
        raise SystemExit(f"No list named {target!r} on the board")
    card = trello("GET", f"boards/{board}/cards/{a.id}", fields="id,name")
    trello("PUT", f"cards/{card['id']}", idList=lists[target])
    print(f"#{a.id} {card['name']} -> {target}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="agentloop")
    sub = ap.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("init"); x.add_argument("--root"); x.set_defaults(fn=cmd_init)
    x = sub.add_parser("brief"); x.add_argument("--hook", action="store_true"); x.add_argument("--json", action="store_true"); x.set_defaults(fn=cmd_brief)
    x = sub.add_parser("tickets"); x.add_argument("ids", nargs="*", type=int); x.add_argument("--all", action="store_true"); x.set_defaults(fn=cmd_tickets)
    x = sub.add_parser("prepare"); x.add_argument("ids", nargs="+", type=int); x.add_argument("--force", action="store_true"); x.set_defaults(fn=cmd_prepare)
    x = sub.add_parser("handoffs"); x.add_argument("--status"); x.set_defaults(fn=cmd_handoffs)
    x = sub.add_parser("mark"); x.add_argument("id", type=int); x.add_argument("status", choices=["ready", "needs-human", "shipped"]); x.add_argument("--pr"); x.set_defaults(fn=cmd_mark)
    x = sub.add_parser("move"); x.add_argument("id", type=int); x.add_argument("list_key"); x.set_defaults(fn=cmd_move)
    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except RuntimeError as e:
        if getattr(a, "hook", False):
            return  # never break session start
        raise SystemExit(str(e))


if __name__ == "__main__":
    main(sys.argv[1:])
