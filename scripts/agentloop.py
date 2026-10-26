#!/usr/bin/env python3
"""agent-loop CLI: project brief, ticket selection, worktrees, handoffs and board moves.

Everything the skills and the sprint workflow need from the outside world goes through here,
so the workflow script itself never touches the filesystem or the network.

A project's state lives in an agent-loop directory holding `config.json`, found from the
current directory in this order:

1. `.agent-loop/` in the current directory or any parent (a bare repo whose worktrees are
   sibling folders, like `<root>/.git` + `<root>/main` + `<root>/812`);
2. `agent-loop/` inside the repository's shared git directory (`<clone>/.git/agent-loop`),
   which every worktree of the clone resolves to and which git never shows as untracked.

Trackers: Trello, GitHub Projects and Jira. Credentials come from the environment or from
`gh auth`, never from a file:

    Trello  TRELLO_KEY, TRELLO_TOKEN
    Jira    JIRA_EMAIL, JIRA_API_TOKEN
    GitHub  `gh auth login` with the `project` scope (`gh auth refresh -s project`)

Commands:

    detect [--tracker-url URL]      what the repo looks like, and a suggested config (JSON)
    init [--config FILE] [--store root|git] [--root DIR]
                                    install a config (default: the template) and scaffold the rest
    doctor                          check config, credentials, tracker columns, repo and checks
    statuses [--config FILE]        the tracker's column names
    show-config                     the resolved config plus root and dir (JSON)
    brief [--hook] [--json]         the project brief (--hook: SessionStart JSON, silent outside a project)
    tickets [ID ...] [--all]        ticket objects as JSON (default: the ready ones)
    prepare ID [ID ...] [--force]   create or reuse each ticket's worktree and run setup
                                    (--force: also for blocked tickets, on the configured base)
    run [ID ...] [--plan] [--force] [--detach] [--resume RUN [--build]]
                                    the loop over the ids (default: every ready ticket); --plan stops
                                    after planning (--resume RUN --build carries on with those plans);
                                    --detach runs it in the background
    status [RUN] [--json]           where a run is (default: the latest)
    handoffs [--status S]           handoff notes as JSON
    mark ID STATUS [--pr URL]       set a handoff's status (ready, needs-human, shipped)
    move ID COLUMN_KEY              move a ticket to a configured column (ready, doing, review) or a named one
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
NET_TIMEOUT = 15

DEFAULT_CONFIG = {
    "name": "",
    "repo": "",
    "base": "origin/main",
    "mainWorktree": ".",
    "worktreePath": "{root}/../{repo}-worktrees/{id}",
    "branch": "{type}/{slug}",
    "typeByLabel": {"bug": "fix", "chore": "chore"},
    "defaultType": "feat",
    "setup": [],
    "checks": [],
    "exclusive": [],
    "contextFiles": [],
    "maxParallel": 10,
    "maxFixRounds": 2,
    "agents": {"default": "claude", "plan": None, "implement": None, "review": None, "fix": None, "handoff": None},
    "agentModels": {},
    "agentArgs": {},
    "agentTimeoutMinutes": 30,
    "tracker": {
        "type": "trello",
        "columns": {"ready": "Sprint Backlog", "doing": "In Progress", "review": "Code Review"},
        "show": ["Sprint Backlog", "In Progress", "Code Review"],
        "done": ["Done"],
        "blockerSection": "Blockers",
    },
    "pr": {
        "template": ".github/PULL_REQUEST_TEMPLATE.md",
        "ticketLine": "#### Ticket [{ref}]({url})",
        "draft": False,
    },
}


# --- config and project -----------------------------------------------------------

def merged(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = merged(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def normalize(cfg: dict) -> dict:
    """Accept the 0.1 config shape (top-level `trello`, `prDraft`)."""
    cfg = dict(cfg)
    if "trello" in cfg and "tracker" not in cfg:
        t = cfg.pop("trello")
        cfg["tracker"] = {"type": "trello", "board": t.get("board", ""), "columns": t.get("lists", {}),
                          **{k: t[k] for k in ("show", "done") if k in t}}
        cfg.setdefault("worktreePath", "{root}/{id}")
        cfg.setdefault("mainWorktree", "main")
    if "prDraft" in cfg:
        cfg.setdefault("pr", {})["draft"] = cfg.pop("prDraft")
    return merged(DEFAULT_CONFIG, cfg)


@dataclass
class Project:
    root: Path
    dir: Path
    config: dict

    @property
    def repo_name(self) -> str:
        return (self.config["repo"].rsplit("/", 1)[-1] if self.config["repo"] else "") or self.root.name

    @property
    def main(self) -> Path:
        return (self.root / self.config["mainWorktree"]).resolve()

    def worktree(self, ticket_id: str) -> Path:
        path = self.config["worktreePath"].format(root=self.root, id=ticket_id, repo=self.repo_name)
        return Path(os.path.normpath(path))


def git_common_dir(start: Path) -> Path | None:
    out = subprocess.run(["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                         capture_output=True, text=True)
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def find_project(start: Path | None = None, config_file: Path | None = None) -> Project | None:
    here = (start or Path.cwd()).resolve()
    if config_file:
        common = git_common_dir(here)
        root = common.parent if common else here
        return Project(root, config_file.resolve().parent, normalize(json.loads(config_file.read_text())))
    for d in (here, *here.parents):
        cfg = d / ".agent-loop" / "config.json"
        if cfg.is_file():
            return Project(d, cfg.parent, normalize(json.loads(cfg.read_text())))
    common = git_common_dir(here) if here.exists() else None
    if common and (common / "agent-loop" / "config.json").is_file():
        cfg = common / "agent-loop" / "config.json"
        return Project(common.parent, cfg.parent, normalize(json.loads(cfg.read_text())))
    return None


def require_project(config_file: str | None = None) -> Project:
    p = find_project(config_file=Path(config_file) if config_file else None)
    if not p:
        raise SystemExit("No agent-loop config for this directory; run /agent-loop:setup (or `agentloop.py init`).")
    return p


# --- outside world (patched in tests) -------------------------------------------

def http_json(method: str, url: str, headers: dict | None = None, data: bytes | None = None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as r:
            body = r.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} on {method} {url.split('?')[0]}: {e.read().decode()[:200]}") from e


def gh(*args: str) -> str:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {out.stderr.strip()[:300]}")
    return out.stdout


def gh_json(*args: str):
    return json.loads(gh(*args) or "null")


def git(root: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check and out.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


# --- trackers ---------------------------------------------------------------

@dataclass
class Board:
    """Tickets normalized across trackers: {id, name, url, status, labels, desc, blockers}."""

    cards: list[dict] = field(default_factory=list)
    resolved: set[str] = field(default_factory=set)      # known done/closed/archived, on the board or not
    open_elsewhere: set[str] = field(default_factory=set)  # known open but not on the board


def text_blockers(desc: str, section: str | None) -> list[str]:
    found: list[str] = []
    if section:
        m = re.search(rf"#+\s*{re.escape(section)}\s*\n(.*?)(?=\n#+\s|\Z)", desc or "", re.S | re.I)
        if m:
            found += re.findall(r"#(\d+)", m.group(1))
    for m in re.finditer(r"(?:blocked by|depends on)\s*:?\s*((?:#\d+(?:\s*,\s*|\s+and\s+|\s*))+)", desc or "", re.I):
        found += re.findall(r"#(\d+)", m.group(1))
    return list(dict.fromkeys(found))


class Tracker:
    kind = ""
    id_pattern = r"\d+"

    def __init__(self, cfg: dict, repo: str, state_dir: Path | None = None):
        self.cfg, self.repo, self.state_dir = cfg, repo, state_dir

    def ref(self, ticket_id: str) -> str:
        return f"#{ticket_id}" if ticket_id.isdigit() else ticket_id

    def norm_id(self, raw: str) -> str:
        return raw.lstrip("#").upper()

    def missing_creds(self) -> str | None:
        return None

    def fetch(self) -> Board:
        raise NotImplementedError

    def columns(self) -> list[str]:
        raise NotImplementedError

    def move(self, ticket_id: str, column: str) -> str:
        raise NotImplementedError


class TrelloTracker(Tracker):
    kind = "trello"
    BASE = "https://api.trello.com/1"

    def missing_creds(self):
        if not (os.environ.get("TRELLO_KEY") and os.environ.get("TRELLO_TOKEN")):
            return "export TRELLO_KEY and TRELLO_TOKEN before starting the session"
        return None

    def api(self, method: str, path: str, **params):
        """`path` must NOT carry a query string: params are encoded here, once."""
        params.update(key=os.environ["TRELLO_KEY"], token=os.environ["TRELLO_TOKEN"])
        qs = urllib.parse.urlencode(params)
        if method == "GET":
            return http_json("GET", f"{self.BASE}/{path}?{qs}")
        return http_json(method, f"{self.BASE}/{path}", data=qs.encode())

    def columns(self):
        return [x["name"] for x in self.api("GET", f"boards/{self.cfg['board']}/lists", fields="name")]

    def fetch(self):
        b = self.cfg["board"]
        names = {x["id"]: x["name"] for x in self.api("GET", f"boards/{b}/lists", fields="name")}
        raw = self.api("GET", f"boards/{b}/cards", fields="name,idShort,idList,desc,labels,url", limit=1000)
        closed = self.api("GET", f"boards/{b}/cards/closed", fields="idShort", limit=1000)
        cards = [{
            "id": str(c["idShort"]), "name": c["name"], "url": c.get("url", ""),
            "status": names.get(c["idList"], "?"), "desc": c.get("desc", ""),
            "labels": [lab["name"] for lab in c.get("labels", []) if lab.get("name")],
            "blockers": text_blockers(c.get("desc", ""), self.cfg.get("blockerSection")),
        } for c in raw]
        return Board(cards, {str(c["idShort"]) for c in closed})

    def move(self, ticket_id, column):
        b = self.cfg["board"]
        lists = {x["name"]: x["id"] for x in self.api("GET", f"boards/{b}/lists", fields="name")}
        if column not in lists:
            raise RuntimeError(f"No list named {column!r} on the board")
        card = self.api("GET", f"boards/{b}/cards/{ticket_id}", fields="id,name")
        self.api("PUT", f"cards/{card['id']}", idList=lists[column])
        return card["name"]


class GitHubTracker(Tracker):
    """A GitHub Projects (v2) board whose single-select Status field is the column."""

    kind = "github"

    @property
    def status_field(self) -> str:
        return self.cfg.get("statusField") or "Status"

    def project_args(self) -> list[str]:
        return [str(self.cfg["project"]), "--owner", self.cfg["owner"]]

    def columns(self):
        fields = gh_json("project", "field-list", *self.project_args(), "--format", "json", "--limit", "100")
        for f in fields.get("fields", []):
            if f.get("name") == self.status_field:
                return [o["name"] for o in f.get("options", [])]
        raise RuntimeError(f"The project has no field named {self.status_field!r}")

    def fetch(self):
        data = gh_json("project", "item-list", *self.project_args(), "--format", "json", "--limit", "1000")
        key = self.status_field.lower()
        cards = []
        for item in data.get("items", []):
            c = item.get("content") or {}
            repo = c.get("repository") or ""
            if c.get("type") != "Issue" or (self.repo and not repo.endswith(self.repo)):
                continue
            body = c.get("body") or ""
            labels = item.get("labels") or c.get("labels") or []
            cards.append({
                "id": str(c["number"]), "name": c.get("title") or item.get("title", ""), "url": c.get("url", ""),
                "status": item.get(key) or "No status", "desc": body,
                "labels": [lab["name"] if isinstance(lab, dict) else lab for lab in labels],
                "blockers": text_blockers(body, self.cfg.get("blockerSection")),
            })
        board = Board(cards)
        if self.repo:
            closed = gh_json("issue", "list", "-R", self.repo, "--state", "closed", "--limit", "1000", "--json", "number")
            board.resolved = {str(i["number"]) for i in closed}
            on_board = {c["id"] for c in cards}
            opened = gh_json("issue", "list", "-R", self.repo, "--state", "open", "--limit", "1000", "--json", "number")
            board.open_elsewhere = {str(i["number"]) for i in opened} - on_board
            ready = self.cfg["columns"].get("ready")
            for c in cards:
                if c["status"] != ready:
                    continue
                try:  # native "blocked by" dependencies, where the repo has them
                    deps = gh_json("api", f"repos/{self.repo}/issues/{c['id']}/dependencies/blocked_by")
                    for dep in deps:
                        n = str(dep["number"])
                        if n not in c["blockers"]:
                            c["blockers"].append(n)
                        if n not in on_board:
                            (board.resolved if dep.get("state") == "closed" else board.open_elsewhere).add(n)
                except (RuntimeError, TypeError, KeyError):
                    pass
        return board

    def move(self, ticket_id, column):
        url = f"https://github.com/{self.repo}/issues/{ticket_id}"
        gh("project", "item-edit", *self.project_args(), "--url", url, "--field", self.status_field, "--value", column)
        return url


def adf_text(node) -> str:
    """Flatten Atlassian Document Format to markdown-ish text: paragraphs, headings, list items."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_text(n) for n in node)
    t, kids = node.get("type"), adf_text(node.get("content", []))
    if t == "text":
        return node.get("text", "")
    if t == "heading":
        return "#" * node.get("attrs", {}).get("level", 3) + " " + kids + "\n\n"
    if t == "paragraph":
        return kids + "\n\n"
    if t == "listItem":
        return "- " + kids.strip() + "\n"
    if t in ("bulletList", "orderedList"):
        return kids + "\n"
    if t == "hardBreak":
        return "\n"
    if t == "inlineCard":
        return node.get("attrs", {}).get("url", "")
    return kids


class JiraTracker(Tracker):
    kind = "jira"
    id_pattern = r"[A-Za-z][A-Za-z0-9_]*-\d+"

    def missing_creds(self):
        if not (os.environ.get("JIRA_EMAIL") and os.environ.get("JIRA_API_TOKEN")):
            return "export JIRA_EMAIL and JIRA_API_TOKEN before starting the session"
        return None

    @property
    def site(self) -> str:
        return self.cfg["site"].rstrip("/")

    def api(self, method: str, path: str, body: dict | None = None, **params):
        auth = base64.b64encode(f"{os.environ['JIRA_EMAIL']}:{os.environ['JIRA_API_TOKEN']}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}", "Accept": "application/json", "Content-Type": "application/json"}
        url = f"{self.site}/rest/api/3/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        return http_json(method, url, headers, json.dumps(body).encode() if body is not None else None)

    def jql(self) -> str:
        return self.cfg.get("jql") or f"project = {self.cfg['project']} AND sprint in openSprints() ORDER BY Rank"

    def columns(self):
        names: list[str] = []
        for issue_type in self.api("GET", f"project/{self.cfg['project']}/statuses"):
            names += [s["name"] for s in issue_type.get("statuses", []) if s["name"] not in names]
        return names

    def fetch(self):
        board, token = Board(), None
        fields = "summary,status,labels,description,issuelinks,issuetype"
        for _ in range(20):
            params = {"jql": self.jql(), "fields": fields, "maxResults": 100}
            if token:
                params["nextPageToken"] = token
            page = self.api("GET", "search/jql", **params)
            for issue in page.get("issues", []):
                f = issue["fields"]
                blockers = []
                for link in f.get("issuelinks", []):
                    other = link.get("inwardIssue")
                    if not other or link.get("type", {}).get("inward", "").lower() != "is blocked by":
                        continue
                    blockers.append(other["key"])
                    category = other.get("fields", {}).get("status", {}).get("statusCategory", {}).get("key")
                    (board.resolved if category == "done" else board.open_elsewhere).add(other["key"])
                labels = list(f.get("labels") or [])
                if f.get("issuetype", {}).get("name"):
                    labels.append(f["issuetype"]["name"])
                board.cards.append({
                    "id": issue["key"], "name": f.get("summary", ""), "url": f"{self.site}/browse/{issue['key']}",
                    "status": f.get("status", {}).get("name", "?"), "desc": adf_text(f.get("description")).strip(),
                    "labels": labels, "blockers": blockers,
                })
            token = page.get("nextPageToken")
            if not token or page.get("isLast"):
                break
        on_board = {c["id"] for c in board.cards}
        board.open_elsewhere -= on_board
        return board

    def move(self, ticket_id, column):
        options = self.api("GET", f"issue/{ticket_id}/transitions").get("transitions", [])
        match = next((t for t in options if t.get("to", {}).get("name") == column or t.get("name") == column), None)
        if not match:
            raise RuntimeError(f"{ticket_id} has no transition to {column!r}; available: "
                               + ", ".join(t.get("to", {}).get("name", t.get("name", "?")) for t in options))
        self.api("POST", f"issue/{ticket_id}/transitions", {"transition": {"id": match["id"]}})
        return ticket_id


class FileTracker(Tracker):
    """Tickets in a JSON file in the agent-loop folder: for trying the loop out, and for teams without a board.

    `tickets.json` is a list of {id, name, status, desc?, labels?, blockers?, url?}.
    """

    kind = "file"
    id_pattern = r"[A-Za-z][A-Za-z0-9_]*-\d+|\d+"

    @property
    def path(self) -> Path:
        return (self.state_dir or Path.cwd()) / self.cfg.get("file", "tickets.json")

    def load(self) -> list[dict]:
        if not self.path.is_file():
            raise RuntimeError(f"No tickets file at {self.path}")
        return json.loads(self.path.read_text())

    def columns(self):
        c = self.cfg
        return list(dict.fromkeys([*c["columns"].values(), *c["show"], *c["done"]]))

    def fetch(self):
        cards = [{
            "id": self.norm_id(str(t["id"])), "name": t["name"], "url": t.get("url", ""),
            "status": t.get("status", ""), "desc": t.get("desc", ""), "labels": t.get("labels", []),
            "blockers": [self.norm_id(str(b)) for b in t.get("blockers", [])]
                        or text_blockers(t.get("desc", ""), self.cfg.get("blockerSection")),
        } for t in self.load()]
        return Board(cards)

    def move(self, ticket_id, column):
        tickets = self.load()
        for t in tickets:
            if self.norm_id(str(t["id"])) == ticket_id:
                t["status"] = column
                self.path.write_text(json.dumps(tickets, indent=2) + "\n")
                return t["name"]
        raise RuntimeError(f"No ticket {ticket_id} in {self.path}")


TRACKERS = {t.kind: t for t in (TrelloTracker, GitHubTracker, JiraTracker, FileTracker)}


def tracker_for(p: Project) -> Tracker:
    kind = p.config["tracker"]["type"]
    if kind not in TRACKERS:
        raise SystemExit(f"Unknown tracker type {kind!r}; use one of {', '.join(TRACKERS)}")
    return TRACKERS[kind](p.config["tracker"], p.config["repo"], p.dir)


def parse_tracker_url(url: str) -> dict:
    u = urllib.parse.urlparse(url.strip())
    parts = [x for x in u.path.split("/") if x]
    if u.netloc.endswith("trello.com") and len(parts) >= 2 and parts[0] == "b":
        return {"type": "trello", "board": parts[1]}
    if u.netloc == "github.com" and len(parts) >= 4 and parts[0] in ("orgs", "users") and parts[2] == "projects":
        return {"type": "github", "owner": parts[1], "project": int(parts[3])}
    if u.netloc.endswith(".atlassian.net"):
        site = f"{u.scheme or 'https'}://{u.netloc}"
        if "projects" in parts and parts.index("projects") + 1 < len(parts):
            return {"type": "jira", "site": site, "project": parts[parts.index("projects") + 1]}
        if "browse" in parts and parts.index("browse") + 1 < len(parts):
            return {"type": "jira", "site": site, "project": parts[parts.index("browse") + 1].split("-")[0]}
        return {"type": "jira", "site": site}
    raise SystemExit(f"Not a Trello board, GitHub project or Jira URL: {url}")


# --- snapshot and classification ---------------------------------------------

@dataclass
class Snapshot:
    """Everything the brief and the ticket selection read, fetched once."""

    tracker: Tracker
    board: Board
    open_prs: list[dict]
    merged_prs: list[dict]
    worktrees: dict[str, str]      # branch -> path
    errors: list[str]

    def by_id(self) -> dict[str, dict]:
        return {c["id"]: c for c in self.board.cards}

    def branch_id(self, branch: str) -> str | None:
        m = re.match(rf"^(?:[\w.-]+/)?({self.tracker.id_pattern})(?:[-_]|$)", branch or "")
        return self.tracker.norm_id(m.group(1)) if m else None

    def pr_for(self, ticket_id: str) -> dict | None:
        return next((p for p in self.open_prs if self.branch_id(p["headRefName"]) == ticket_id), None)

    def merged_ids(self) -> set[str]:
        return {i for p in self.merged_prs if (i := self.branch_id(p["headRefName"])) is not None}

    def worktree_for(self, ticket_id: str) -> tuple[str, str] | None:
        return next(((b, path) for b, path in self.worktrees.items() if self.branch_id(b) == ticket_id), None)


def snapshot(p: Project) -> Snapshot:
    t = tracker_for(p)
    s = Snapshot(t, Board(), [], [], {}, [])
    missing = t.missing_creds()
    if missing:
        s.errors.append(f"Board unavailable: {missing}.")
    else:
        try:
            s.board = t.fetch()
        except (RuntimeError, OSError, KeyError, subprocess.TimeoutExpired) as e:
            hint = " (run `gh auth refresh -s project`)" if "scope" in str(e) else ""
            s.errors.append(f"Board unavailable: {e}{hint}")
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


def slug_of(card: dict) -> str:
    """The Trello card URL's own slug (`816-every-step-belongs-to-an-agent`), else id plus the name's words."""
    tail = (card.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
    if "trello.com" in (card.get("url") or "") and tail.startswith(f"{card['id']}-"):
        return tail[:70].rstrip("-")
    words = re.sub(r"[^a-z0-9]+", "-", card["name"].lower()).strip("-")
    return f"{card['id'].lower()}-{words}"[:70].rstrip("-")


def classify(p: Project, s: Snapshot, card: dict) -> dict:
    """One ticket object: what it is, whether an agent may start it, and on which base."""
    tcfg = p.config["tracker"]
    cards = s.by_id()
    done = set(tcfg["done"])
    merged_ids = s.merged_ids()
    cid = card["id"]
    by_label = {k.lower(): v for k, v in p.config["typeByLabel"].items()}
    kind = next((by_label[lab.lower()] for lab in card["labels"] if lab.lower() in by_label), p.config["defaultType"])

    open_blockers = []
    for b in card["blockers"]:
        other = cards.get(b)
        if b in s.board.resolved or b in merged_ids or (other and other["status"] in done):
            continue
        if other is None and b not in s.board.open_elsewhere:
            continue  # a dead reference blocks nothing; the brief flags it
        open_blockers.append(b)

    base, status, reason = p.config["base"], "ready", ""
    wt, pr = s.worktree_for(cid), s.pr_for(cid)
    ref = s.tracker.ref
    if pr:
        status, reason = "in-flight", f"PR #{pr['number']} open"
    elif wt:
        status, reason = "in-flight", f"worktree {wt[1]}"
    elif open_blockers:
        stack = [s.pr_for(b) for b in open_blockers]
        if len(open_blockers) == 1 and stack[0]:
            base, reason = f"origin/{stack[0]['headRefName']}", f"stacked on {ref(open_blockers[0])} (PR #{stack[0]['number']})"
        else:
            status, reason = "blocked", "waiting on " + ", ".join(ref(b) for b in open_blockers)

    branch = wt[0] if wt else p.config["branch"].format(type=kind, slug=slug_of(card), id=cid)
    return {
        "id": cid,
        "ref": ref(cid),
        "name": card["name"],
        "url": card["url"],
        "list": card["status"],
        "labels": card["labels"],
        "desc": card["desc"],
        "status": status,
        "reason": reason,
        "blockers": open_blockers,
        "branch": branch,
        "base": base,
        "worktree": wt[1] if wt else str(p.worktree(cid)),
    }


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
    return {**meta, "id": meta.get("id") or path.stem, "path": str(path), "body": body.strip()}


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
    cfg, ref = p.config, s.tracker.ref
    out = [f"# Project brief: {cfg['name'] or p.repo_name}", ""]
    out += [f"Main checkout `{p.main}`; ticket worktrees at `{p.worktree('<id>')}`; agent-loop state in `{p.dir}`."]
    if cfg["contextFiles"]:
        out += ["Domain and conventions: " + ", ".join(f"`{f}`" for f in cfg["contextFiles"]) + " (in any worktree)."]
    for e in s.errors:
        out += [f"> {e}"]

    for title, name, cap in (("Sprint goal", "sprint.md", 4000), ("Client requirements", "requirements.md", 6000)):
        text = read_capped(p.dir / name, cap)
        if text:
            out += ["", f"## {title}", "", text]

    if s.board.cards:
        out += ["", f"## Board ({s.tracker.kind})"]
        ready = cfg["tracker"]["columns"].get("ready")
        for name in cfg["tracker"]["show"]:
            cards = [c for c in s.board.cards if c["status"] == name]
            if not cards:
                continue
            out += ["", f"### {name} ({len(cards)})", ""]
            for c in cards:
                t = classify(p, s, c)
                tag = "" if name != ready else f" [{t['status']}{': ' + t['reason'] if t['reason'] else ''}]"
                labels = f" ({', '.join(t['labels'])})" if t["labels"] else ""
                out.append(f"- {ref(c['id'])} {c['name']}{labels}{tag}")
        known = set(s.by_id()) | s.board.resolved | s.board.open_elsewhere
        dead = sorted({b for c in s.board.cards for b in c["blockers"] if b not in known})
        if dead:
            out += ["", "Dead blocker references: " + ", ".join(ref(b) for b in dead)]

    if s.open_prs or s.merged_prs:
        out += ["", "## Pull requests", ""]
        for pr in s.open_prs:
            state = "draft" if pr.get("isDraft") else (pr.get("reviewDecision") or "awaiting review").lower().replace("_", " ")
            base = "" if pr["baseRefName"] == p.config["base"].split("/", 1)[-1] else f", onto `{pr['baseRefName']}`"
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
            out.append(f"### {ref(h['id'])} {h.get('title', '')} [{h.get('status', '?')}]")
            for part in ("Summary", "Decisions", "Follow-ups"):
                text = section(h["body"], part)
                if text:
                    out += [f"{part}: {text}" if "\n" not in text else f"{part}:\n{text}"]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


# --- detect --------------------------------------------------------------------

def make_targets(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return re.findall(r"^([A-Za-z][\w-]*):", path.read_text(), re.M)


def detect(start: Path, tracker_url: str | None) -> dict:
    """Facts about the repo and a config suggested from them; the setup wizard confirms the rest."""
    common = git_common_dir(start)
    if not common:
        raise SystemExit("Not inside a git repository.")
    bare = git(common, "rev-parse", "--is-bare-repository", check=False).strip() == "true"
    root = common.parent
    worktrees: dict[str, str] = {}
    path = None
    for line in git(common, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[9:]
        elif line.startswith("branch refs/heads/") and path:
            worktrees[line[18:]] = path
    head = git(common, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False).strip()
    default = head.rsplit("/", 1)[-1] if head else "main"
    main = root
    if bare:
        # The folder named after the default branch, else the worktree on it, else any not under .claude/
        paths = [Path(x) for x in worktrees.values()]
        candidates = [x for x in paths if x.name == default]
        if default in worktrees:
            candidates.append(Path(worktrees[default]))
        candidates += [x for x in paths if ".claude" not in x.parts] + paths
        main = candidates[0] if candidates else root
    remote = git(common, "remote", "get-url", "origin", check=False).strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", remote)
    repo = m.group(1) if m else ""

    checks, setup, exclusive, context = [], [], [], []
    targets = make_targets(main / "Makefile")
    for t in ("lint", "typecheck", "test-unit"):
        if t in targets:
            checks.append(f"make {t}")
    if "test-unit" not in targets and "test" in targets:
        checks.append("make test")
    pkg = main / "package.json"
    if pkg.is_file() and not checks:
        scripts = json.loads(pkg.read_text()).get("scripts", {})
        runner = "pnpm" if (main / "pnpm-lock.yaml").exists() else "yarn" if (main / "yarn.lock").exists() else "npm run"
        checks += [f"{runner} {s}" for s in ("lint", "typecheck", "test") if s in scripts]
    if (main / ".env").exists():
        setup.append("test -f .env || cp {main}/.env .env")
    for lock, cmd in (("uv.lock", "uv sync -q"), ("poetry.lock", "poetry install -q"), ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
                      ("yarn.lock", "yarn install --frozen-lockfile"), ("package-lock.json", "npm ci"), ("Gemfile.lock", "bundle install")):
        if (main / lock).exists():
            setup.append(cmd)
            exclusive.append(lock)
    for manifest in ("pyproject.toml", "package.json", "Gemfile"):
        if (main / manifest).exists():
            exclusive.append(manifest)
    for d in ("alembic/versions", "migrations", "prisma/migrations", "db/migrate", "src/migrations"):
        if (main / d).is_dir():
            exclusive.append(d + "/")
    for f in ("AGENTS.md", "CLAUDE.md", "CONTEXT.md", "CONTRIBUTING.md", "docs/adr", "docs/notes"):
        if (main / f).exists():
            context.append(f + ("/" if (main / f).is_dir() else ""))
    template = next((t for t in (".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md",
                                 "docs/pull_request_template.md", "PULL_REQUEST_TEMPLATE.md") if (main / t).is_file()), "")

    config = merged(DEFAULT_CONFIG, {
        "name": repo.rsplit("/", 1)[-1] if repo else root.name,
        "repo": repo,
        "base": f"origin/{default}",
        "mainWorktree": os.path.relpath(main, root),
        "worktreePath": "{root}/{id}" if bare else "{root}/../{repo}-worktrees/{id}",
        "setup": setup, "checks": checks, "exclusive": exclusive, "contextFiles": context,
        "pr": {"template": template},
    })
    if tracker_url:
        config["tracker"] = merged(config["tracker"], parse_tracker_url(tracker_url))
    agents_md = (main / "AGENTS.md").read_text() if (main / "AGENTS.md").is_file() else ""
    return {
        "facts": {
            "layout": "bare repo with sibling worktrees" if bare else "clone",
            "root": str(root), "main": str(main), "gitDir": str(common), "remote": remote,
            "defaultBranch": default, "store": str(root / ".agent-loop") if bare else str(common / "agent-loop"),
            "makeTargets": targets, "prTemplate": template,
            "agentsForbidCommits": bool(re.search(r"(?i)git commit.{0,120}(confirm|only after)", agents_md)),
            "credentials": {
                "trello": bool(os.environ.get("TRELLO_KEY") and os.environ.get("TRELLO_TOKEN")),
                "jira": bool(os.environ.get("JIRA_EMAIL") and os.environ.get("JIRA_API_TOKEN")),
            },
            "existingConfig": str(p.dir / "config.json") if (p := find_project(start)) else "",
        },
        "config": config,
    }


# --- commands ----------------------------------------------------------------

def scaffold(d: Path) -> None:
    (d / "handoffs").mkdir(parents=True, exist_ok=True)
    (d / "plans").mkdir(exist_ok=True)
    for name in ("sprint.md", "requirements.md"):
        target = d / name
        if not target.exists():
            target.write_text((PLUGIN_ROOT / "templates" / name).read_text())
            print(f"created {target}")


def cmd_init(a) -> None:
    start = Path(a.root or Path.cwd()).resolve()
    if a.config:
        cfg = json.loads(Path(a.config).read_text())
    else:
        cfg = json.loads((PLUGIN_ROOT / "templates" / "config.json").read_text())
    common = git_common_dir(start)
    store = a.store or ("git" if common and not a.root and
                        git(common, "rev-parse", "--is-bare-repository", check=False).strip() == "false" else "root")
    if store == "git":
        if not common:
            raise SystemExit("--store git needs a git repository")
        d = common / "agent-loop"
    else:
        d = (Path(a.root).resolve() if a.root else common.parent if common else start) / ".agent-loop"
    d.mkdir(parents=True, exist_ok=True)
    target = d / "config.json"
    if target.exists() and not a.config:
        print(f"kept {target}")
    else:
        target.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"wrote {target}")
    scaffold(d)


def cmd_detect(a) -> None:
    print(json.dumps(detect(Path.cwd(), a.tracker_url), indent=2))


def cmd_statuses(a) -> None:
    p = require_project(a.config)
    t = tracker_for(p)
    if (missing := t.missing_creds()):
        raise SystemExit(f"Cannot read the board: {missing}")
    print(json.dumps(t.columns()))


def cmd_show_config(a) -> None:
    p = require_project(a.config)
    print(json.dumps({**p.config, "root": str(p.root), "dir": str(p.dir), "main": str(p.main)}, indent=2))


AGENT_LOGIN = {"claude": "install Claude Code and run `claude` once to log in",
               "codex": "codex login", "cursor": "cursor-agent login"}


def agent_ready(tool: str) -> tuple[bool, str]:
    """Whether a headless agent tool is installed and logged in."""
    cmd = {"claude": ["claude", "--version"], "codex": ["codex", "login", "status"], "cursor": ["cursor-agent", "status"]}.get(tool)
    if not cmd:
        return False, f"unknown tool (use {', '.join(AGENT_LOGIN)})"
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, "not installed"
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out.stdout + out.stderr).strip()
    if out.returncode or "not logged in" in text.lower():
        return False, text.splitlines()[-1] if text else f"exit {out.returncode}"
    return True, ""


def cmd_doctor(a) -> None:
    p = require_project(a.config)
    cfg, results = p.config, []

    def check(ok: bool, what: str, fix: str = "") -> None:
        results.append(ok)
        print(f"{'ok  ' if ok else 'FAIL'} {what}" + (f"\n     fix: {fix}" if fix and not ok else ""))

    print(f"config {p.dir / 'config.json'}")
    for tool in sorted({v for v in cfg["agents"].values() if v}):
        ok, detail = agent_ready(tool)
        check(ok, f"agent tool {tool}{': ' + detail if detail else ''}", AGENT_LOGIN.get(tool, ""))
    t = tracker_for(p)
    missing = t.missing_creds()
    check(not missing, f"{t.kind} credentials", missing or "")
    if not missing:
        try:
            cols = t.columns()
            check(True, f"{t.kind} board readable ({len(cols)} columns: {', '.join(cols)})")
            wanted = list(cfg["tracker"]["columns"].values()) + cfg["tracker"]["show"] + cfg["tracker"]["done"]
            unknown = sorted({c for c in wanted if c not in cols})
            check(not unknown, "configured columns exist on the board", "rename in config: " + ", ".join(unknown))
        except (RuntimeError, OSError, KeyError, subprocess.TimeoutExpired) as e:
            fix = "gh auth refresh -s project" if "scope" in str(e) else "check the board id and credentials"
            check(False, f"{t.kind} board readable: {e}", fix)
    if cfg["repo"]:
        try:
            gh_json("repo", "view", cfg["repo"], "--json", "name")
            check(True, f"GitHub repo {cfg['repo']} readable")
        except RuntimeError as e:
            check(False, f"GitHub repo {cfg['repo']}: {e}", "gh auth login")
    else:
        check(False, "repo is set", "set \"repo\": \"owner/name\"")
    check(bool(git(p.root, "rev-parse", "--verify", "-q", cfg["base"], check=False).strip()),
          f"base {cfg['base']} exists", f"git -C {p.root} fetch origin")
    check(p.main.is_dir(), f"main checkout {p.main}", "fix mainWorktree")
    parent = p.worktree("X").parent
    check(parent.is_dir() or os.access(parent.parent, os.W_OK), f"worktree folder {parent} usable", "fix worktreePath")
    for cmd in cfg["checks"] + cfg["setup"]:
        word = shlex.split(cmd)[0]
        ok = word in ("test", "cp", "[") or bool(shutil.which(word))
        if ok and word == "make":
            target = shlex.split(cmd)[1] if len(shlex.split(cmd)) > 1 else ""
            ok = not target or target in make_targets(p.main / "Makefile")
        check(ok, f"command `{cmd}`", "install it or change checks/setup")
    tpl = cfg["pr"].get("template")
    check(not tpl or (p.main / tpl).is_file(), f"PR template {tpl or '(none)'}", "fix pr.template or set it to \"\"")
    if not all(results):
        raise SystemExit(1)


def trim_brief(text: str, limit: int) -> str:
    """Fit the brief under `limit` characters, dropping the least useful parts first."""
    if not limit or len(text) <= limit:
        return text
    head, _, notes = text.partition("\n## Agent handoffs (newest first)\n")
    blocks = [b for b in re.split(r"\n(?=### )", notes) if b.strip()]
    while blocks and len(head) + sum(len(b) for b in blocks) + 60 > limit:
        blocks.pop()  # oldest handoff first
    if blocks:
        head += "\n## Agent handoffs (newest first)\n" + "\n".join(blocks)
    for pattern in (r"\n## Local worktrees\n.*?(?=\n## |\Z)", r"\nRecently merged: [^\n]*"):
        if len(head) > limit:
            head = re.sub(pattern, "", head, flags=re.S)
    if len(head) > limit:
        head = head[:limit].rsplit("\n", 1)[0]
    return head.rstrip() + "\n\n(Brief shortened to fit; `brief.md` in the agent-loop folder has all of it.)\n"


def hook_input() -> dict:
    """The JSON a session hook gets on stdin (Claude Code, Codex and Cursor all send one), if any."""
    stdin = sys.stdin
    if stdin is None or stdin.isatty():
        return {}
    try:
        if not select.select([stdin], [], [], 2.0)[0]:
            return {}  # an open pipe nobody writes to: never hang session start
    except (ValueError, OSError, io.UnsupportedOperation):
        pass  # not a real file (tests); just read it
    try:
        return json.loads(stdin.read() or "{}")
    except ValueError:
        return {}


def cmd_brief(a) -> None:
    event = hook_input() if a.hook else {}
    start = event.get("cwd") or next(iter(event.get("workspace_roots") or []), None)
    p = find_project(Path(start) if start else None) if a.hook else require_project(a.config)
    if not p:
        return
    text = brief(p, snapshot(p))
    try:
        (p.dir / "brief.md").write_text(text)
    except OSError:
        pass
    if a.hook:
        intro = ("The agent-loop plugin generated this brief for the project this session runs in. "
                 "It is current as of session start; the agent-loop brief skill refreshes it.\n\n")
        context = intro + trim_brief(text, a.max_chars)
        if "cursor_version" in event:
            print(json.dumps({"additional_context": context}))
        else:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))
    elif a.json:
        print(json.dumps({"brief": text, "path": str(p.dir / "brief.md")}))
    else:
        print(trim_brief(text, a.max_chars) if a.max_chars else text)


def picked_ids(s: Snapshot, raw: list[str]) -> list[str]:
    return [s.tracker.norm_id(x) for x in raw]


def cmd_tickets(a) -> None:
    p = require_project(a.config)
    s = snapshot(p)
    if not s.board.cards:
        raise SystemExit("\n".join(s.errors) or "The board has no tickets.")
    if a.ids:
        by = s.by_id()
        ids = picked_ids(s, a.ids)
        missing = [i for i in ids if i not in by]
        if missing:
            raise SystemExit("Not on the board: " + ", ".join(s.tracker.ref(i) for i in missing))
        picked = [classify(p, s, by[i]) for i in ids]
    else:
        ready = p.config["tracker"]["columns"]["ready"]
        picked = [classify(p, s, c) for c in s.board.cards if c["status"] == ready]
        if not a.all:
            picked = [t for t in picked if t["status"] == "ready"]
    print(json.dumps(picked, indent=2))


def run_setup(p: Project, wt: Path) -> list[dict]:
    results = []
    for cmd in p.config["setup"]:
        cmd = cmd.format(root=p.root, main=p.main, worktree=wt)
        r = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True)
        results.append({"cmd": cmd, "ok": r.returncode == 0, "tail": (r.stdout + r.stderr).strip()[-300:]})
    return results


def prepare(p: Project, s: Snapshot, ids: list[str], force: bool = False) -> tuple[list[dict], list[dict]]:
    """Create or reuse each ticket's worktree and run setup; returns (prepared tickets, skipped ones)."""
    by = s.by_id()
    git(p.root, "fetch", "-q", "origin")
    tickets, skipped = [], []
    for cid in ids:
        if cid not in by:
            skipped.append({"id": cid, "error": "not on the board"})
            continue
        t = classify(p, s, by[cid])
        wt = Path(t["worktree"])
        if t["status"] == "blocked" and not force:
            skipped.append({**t, "error": t["reason"]})
            continue
        if not wt.exists():
            wt.parent.mkdir(parents=True, exist_ok=True)
            exists = git(p.root, "rev-parse", "--verify", "-q", f"refs/heads/{t['branch']}", check=False).strip()
            if exists:
                git(p.root, "worktree", "add", str(wt), t["branch"])
            else:
                git(p.root, "worktree", "add", "--no-track", "-b", t["branch"], str(wt), t["base"])
            t["created"] = True
        t["setup"] = run_setup(p, wt)
        failed = [x for x in t["setup"] if not x["ok"]]
        if failed:
            skipped.append({**t, "error": f"setup failed: {failed[0]['cmd']}: {failed[0]['tail']}"})
        else:
            tickets.append(t)
    return tickets, skipped


def run_project(p: Project) -> dict:
    """The project settings a run needs."""
    cfg = p.config
    return {"root": str(p.root), "dir": str(p.dir), "briefPath": str(p.dir / "brief.md"),
            **{k: cfg[k] for k in ("checks", "exclusive", "maxParallel", "maxFixRounds")}}


def cmd_prepare(a) -> None:
    p = require_project(a.config)
    s = snapshot(p)
    tickets, skipped = prepare(p, s, picked_ids(s, a.ids), a.force)
    print(json.dumps({"project": run_project(p), "tickets": tickets, "skipped": skipped}, indent=2))


def runs_dir(p: Project) -> Path:
    return p.dir / "runs"


def cmd_run(a) -> None:
    """Start a run (prepare the tickets, then the loop), or resume one."""
    import runner
    p = require_project(a.config)
    if a.resume:
        run_dir = runs_dir(p) / a.resume
        if not (run_dir / "run.json").is_file():
            raise SystemExit(f"No run {a.resume} in {runs_dir(p)}")
        run = runner.Run.load(run_dir)
        if a.build and run.plan_only:
            run.plan_only = False
            run.update(planOnly=False, finished=None)
    else:
        s = snapshot(p)
        if not s.board.cards:
            raise SystemExit("\n".join(s.errors) or "The board has no tickets.")
        if a.ids:
            ids = picked_ids(s, a.ids)
        else:
            ready = p.config["tracker"]["columns"]["ready"]
            ids = [c["id"] for c in s.board.cards if c["status"] == ready and classify(p, s, c)["status"] == "ready"]
        if not ids:
            raise SystemExit("No ready tickets.")
        tickets, skipped = prepare(p, s, ids, a.force)
        for x in skipped:
            print(f"skipped {s.tracker.ref(x['id'])}: {x['error']}", file=sys.stderr)
        if not tickets:
            raise SystemExit("Nothing to run.")
        (p.dir / "brief.md").write_text(brief(p, s))
        run_dir = runs_dir(p) / time.strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True)
        run = runner.Run(run_dir, run_project(p), tickets, p.config["agents"], p.config["agentModels"],
                         p.config["agentTimeoutMinutes"], p.config["agentArgs"], a.plan)
        run.save()
        run.update(skipped=[{"id": x["id"], "error": x["error"]} for x in skipped])
    if a.detach:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--config", str(p.dir / "config.json"), "run", "--resume", run.id]
        with open(run.dir / "detached.log", "a") as out:
            proc = subprocess.Popen(cmd, cwd=p.root, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        run.update(pid=proc.pid)
        print(json.dumps({"run": run.id, "pid": proc.pid, "log": str(run.dir / "log.txt"),
                          "tickets": [t["ref"] for t in run.tickets]}))
        return
    import adapters
    summary = runner.Loop(run, adapters.run_agent).execute()
    print(json.dumps({"run": run.id, **summary}, indent=2))


def cmd_status(a) -> None:
    import runner
    p = require_project(a.config)
    d = runs_dir(p)
    runs = sorted(x.name for x in d.iterdir() if (x / "run.json").is_file()) if d.is_dir() else []
    if not runs:
        raise SystemExit("No runs yet.")
    run_id = a.run or runs[-1]
    path = d / run_id / "run.json"
    if not path.is_file():
        raise SystemExit(f"No run {run_id}; runs: {', '.join(runs[-5:])}")
    state = json.loads(path.read_text())
    if a.json:
        state.pop("prepared", None)
        print(json.dumps(state, indent=2))
    else:
        print(runner.status_text(state))


def cmd_handoffs(a) -> None:
    p = require_project(a.config)
    notes = [h for h in handoffs(p) if not a.status or h.get("status") == a.status]
    print(json.dumps(notes, indent=2))


def cmd_mark(a) -> None:
    p = require_project(a.config)
    path = p.dir / "handoffs" / f"{tracker_for(p).norm_id(a.id)}.md"
    if not path.is_file():
        raise SystemExit(f"No handoff for {a.id}")
    text = path.read_text()
    text = re.sub(r"^status:.*$", f"status: {a.status}", text, count=1, flags=re.M)
    if a.pr:
        text = re.sub(r"^pr:.*$\n?", "", text, flags=re.M)
        text = text.replace("\n---", f"\npr: {a.pr}\n---", 1)
    path.write_text(text)
    print(f"{a.id} -> {a.status}")


def cmd_move(a) -> None:
    p = require_project(a.config)
    t = tracker_for(p)
    if (missing := t.missing_creds()):
        raise SystemExit(f"Cannot move: {missing}")
    target = p.config["tracker"]["columns"].get(a.column, a.column)
    ticket_id = t.norm_id(a.id)
    print(f"{t.ref(ticket_id)} {t.move(ticket_id, target)} -> {target}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="agentloop")
    ap.add_argument("--config", help="use this config file instead of looking one up")
    sub = ap.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("detect"); x.add_argument("--tracker-url"); x.set_defaults(fn=cmd_detect)
    x = sub.add_parser("init"); x.add_argument("--root"); x.add_argument("--store", choices=["root", "git"]); x.set_defaults(fn=cmd_init)
    x = sub.add_parser("doctor"); x.set_defaults(fn=cmd_doctor)
    x = sub.add_parser("statuses"); x.set_defaults(fn=cmd_statuses)
    x = sub.add_parser("show-config"); x.set_defaults(fn=cmd_show_config)
    x = sub.add_parser("brief"); x.add_argument("--hook", action="store_true"); x.add_argument("--json", action="store_true")
    x.add_argument("--max-chars", type=int, default=None, help="cap the brief (default with --hook: 9000, about 2,300 tokens)")
    x.set_defaults(fn=cmd_brief)
    x = sub.add_parser("tickets"); x.add_argument("ids", nargs="*"); x.add_argument("--all", action="store_true"); x.set_defaults(fn=cmd_tickets)
    x = sub.add_parser("prepare"); x.add_argument("ids", nargs="+"); x.add_argument("--force", action="store_true"); x.set_defaults(fn=cmd_prepare)
    x = sub.add_parser("run"); x.add_argument("ids", nargs="*"); x.add_argument("--plan", action="store_true")
    x.add_argument("--force", action="store_true"); x.add_argument("--resume"); x.add_argument("--detach", action="store_true")
    x.add_argument("--build", action="store_true", help="with --resume: carry a --plan run on into implementation"); x.set_defaults(fn=cmd_run)
    x = sub.add_parser("status"); x.add_argument("run", nargs="?"); x.add_argument("--json", action="store_true"); x.set_defaults(fn=cmd_status)
    x = sub.add_parser("handoffs"); x.add_argument("--status"); x.set_defaults(fn=cmd_handoffs)
    x = sub.add_parser("mark"); x.add_argument("id"); x.add_argument("status", choices=["ready", "needs-human", "shipped"]); x.add_argument("--pr"); x.set_defaults(fn=cmd_mark)
    x = sub.add_parser("move"); x.add_argument("id"); x.add_argument("column"); x.set_defaults(fn=cmd_move)
    a = ap.parse_args(argv)
    if getattr(a, "hook", False) and a.max_chars is None:
        a.max_chars = 9000
    try:
        a.fn(a)
    except RuntimeError as e:
        if getattr(a, "hook", False):
            return  # never break session start
        raise SystemExit(str(e))


if __name__ == "__main__":
    main(sys.argv[1:])
