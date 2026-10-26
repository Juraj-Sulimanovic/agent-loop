"""Project knowledge: client transcripts, documents and test files, kept next to the project's
agent-loop state and never committed.

    <agent-loop dir>/knowledge/
      index.json                 one entry per item (the source of truth)
      INDEX.md                   generated from index.json; the brief lists it
      items/<id>/original/...    the files as given
      items/<id>/text/...        a plain-text version agents can read (docx, xlsx, csv, pdf)

Agents never get these files pasted in: the brief lists the index, the planner picks the items a
ticket needs, and later stages get their paths. Text extraction uses the standard library only.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".vtt", ".srt"}
PREVIEW_ROWS = 15
PREVIEW_COLS = 40


def row_text(cells: list[str]) -> str:
    extra = len(cells) - PREVIEW_COLS
    return "\t".join(cells[:PREVIEW_COLS]) + (f"\t… {extra} more columns" if extra > 0 else "")
KINDS = ("transcript", "doc", "data", "other")


def kdir(state_dir: Path) -> Path:
    return state_dir / "knowledge"


def load(state_dir: Path) -> list[dict]:
    path = kdir(state_dir) / "index.json"
    return json.loads(path.read_text()) if path.is_file() else []


def save(state_dir: Path, items: list[dict]) -> None:
    d = kdir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.json").write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")
    (d / "INDEX.md").write_text(index_markdown(state_dir, items))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "item"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- text extraction ---------------------------------------------------------------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def docx_text(path: Path) -> str:
    """Paragraphs and table rows of a .docx, headings marked with #."""
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    in_tables = {id(p) for tbl in root.iter(f"{W}tbl") for p in tbl.iter(f"{W}p")}
    out = []
    for block in root.iter():
        if block.tag == f"{W}p" and id(block) not in in_tables:
            text = "".join(t.text or "" for t in block.iter(f"{W}t")).strip()
            style = block.find(f"{W}pPr/{W}pStyle")
            level = re.search(r"(\d)$", style.get(f"{W}val", "")) if style is not None else None
            if text:
                heading = style is not None and "heading" in style.get(f"{W}val", "").lower()
                out.append(("#" * int(level.group(1)) + " " if heading and level else "") + text)
        elif block.tag == f"{W}tr":
            cells = ["".join(t.text or "" for t in c.iter(f"{W}t")).strip() for c in block.findall(f"{W}tc")]
            if any(cells):
                out.append(" | ".join(cells))
    return "\n\n".join(out) + "\n"


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def xlsx_text(path: Path, rows: int = PREVIEW_ROWS) -> str:
    """Each sheet's name, size and first rows as tab-separated text."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{S}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{S}t")))
        wb = ElementTree.fromstring(z.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels}
        out = [f"Workbook {path.name}"]
        for sheet in wb.iter(f"{S}sheet"):
            target = targets.get(sheet.get(f"{R}id"), "")
            member = "xl/" + target.lstrip("/").removeprefix("xl/")
            if member not in names:
                continue
            root = ElementTree.fromstring(z.read(member))
            dim = root.find(f"{S}dimension")
            all_rows = root.findall(f"{S}sheetData/{S}row")
            out += ["", f"## Sheet: {sheet.get('name')} ({len(all_rows)} rows{', ' + dim.get('ref') if dim is not None else ''})"]
            for row in all_rows[:rows]:
                cells: dict[int, str] = {}
                for c in row.findall(f"{S}c"):
                    v, kind = c.find(f"{S}v"), c.get("t")
                    if kind == "inlineStr":
                        text = "".join(t.text or "" for t in c.iter(f"{S}t"))
                    elif v is None:
                        continue
                    elif kind == "s":
                        text = shared[int(v.text)] if v.text and v.text.isdigit() and int(v.text) < len(shared) else ""
                    else:
                        text = v.text or ""
                    cells[_col(c.get("r", "A1"))] = text.replace("\t", " ").replace("\n", " ")
                if cells:
                    out.append(row_text([cells.get(i, "") for i in range(max(cells) + 1)]))
            if len(all_rows) > rows:
                out.append(f"… {len(all_rows) - rows} more rows")
    return "\n".join(out) + "\n"


def csv_text(path: Path, rows: int = PREVIEW_ROWS) -> str:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    records = list(csv.reader(io.StringIO(raw)))
    out = [f"CSV {path.name}: {max(len(records) - 1, 0)} data rows, {len(records[0]) if records else 0} columns", ""]
    out += [row_text(r) for r in records[: rows + 1]]
    if len(records) > rows + 1:
        out.append(f"… {len(records) - rows - 1} more rows")
    return "\n".join(out) + "\n"


def pdf_text(path: Path) -> str | None:
    if not shutil.which("pdftotext"):
        return None
    out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=120)
    return out.stdout if out.returncode == 0 else None


def extract(path: Path) -> str | None:
    """A readable text version of the file, or None when there is no extractor for it."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            return docx_text(path)
        if suffix in (".xlsx", ".xlsm"):
            return xlsx_text(path)
        if suffix == ".csv":
            return csv_text(path)
        if suffix == ".pdf":
            return pdf_text(path)
        if suffix in TEXT_SUFFIXES:
            return path.read_text(errors="replace")
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, UnicodeError, OSError) as e:
        return f"(could not extract text: {e})\n"
    return None


def guess_kind(paths: list[Path]) -> str:
    names = " ".join(p.name.lower() for p in paths)
    if re.search(r"transcript|sync|meeting|call|standup|\.vtt|\.srt", names):
        return "transcript"
    if all(p.suffix.lower() in (".csv", ".xlsx", ".xlsm", ".json") for p in paths):
        return "data"
    return "doc"


# --- ingest ------------------------------------------------------------------------

def ingest(state_dir: Path, source: Path, kind: str | None = None, title: str | None = None,
           date: str | None = None, item_id: str | None = None) -> dict:
    """Copy a file or a folder into the knowledge folder as one item, with text versions.

    Files identical to one already in the knowledge folder (or to another in the same folder)
    are recorded as duplicates and not copied again.
    """
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    items = load(state_dir)
    files = sorted(p for p in source.rglob("*") if p.is_file() and not p.name.startswith(".")) if source.is_dir() else [source]
    if not files:
        raise ValueError(f"No files in {source}")
    known = {f["sha256"]: (it["id"], f["name"]) for it in items for f in it["files"]}
    taken = {it["id"] for it in items}
    base = item_id or slug(title or source.stem)
    iid, n = base, 2
    while iid in taken:
        iid, n = f"{base}-{n}", n + 1

    item_dir = kdir(state_dir) / "items" / iid
    entry = {
        "id": iid, "title": title or source.stem, "kind": kind or guess_kind(files),
        "date": date or time.strftime("%Y-%m-%d", time.localtime(source.stat().st_mtime)),
        "added": time.strftime("%Y-%m-%d"), "source": str(source), "summary": "", "useFor": "",
        "status": "new", "files": [], "duplicates": [],
    }
    for f in files:
        digest = sha256(f)
        rel = f.relative_to(source) if source.is_dir() else Path(f.name)
        if digest in known:
            entry["duplicates"].append({"name": str(rel), "sameAs": f"{known[digest][0]}/{known[digest][1]}"})
            continue
        known[digest] = (iid, str(rel))
        dest = item_dir / "original" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        text = extract(f)
        text_path = None
        if text is not None:
            text_path = item_dir / "text" / (str(rel) + ".txt")
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(text)
        entry["files"].append({"name": str(rel), "path": str(dest), "text": str(text_path) if text_path else None,
                               "bytes": f.stat().st_size, "sha256": digest})
    if not entry["files"]:
        raise ValueError(f"Every file in {source} is already in the knowledge folder: "
                         + ", ".join(d["sameAs"] for d in entry["duplicates"]))
    items.append(entry)
    save(state_dir, items)
    return entry


def annotate(state_dir: Path, item_id: str, **fields) -> dict:
    items = load(state_dir)
    for it in items:
        if it["id"] == item_id:
            it.update({k: v for k, v in fields.items() if v is not None})
            save(state_dir, items)
            return it
    raise KeyError(f"No knowledge item {item_id!r}")


def resolve(state_dir: Path, ids: list[str]) -> list[dict]:
    by = {it["id"]: it for it in load(state_dir)}
    return [by[i] for i in ids if i in by]


# --- rendering ------------------------------------------------------------------------

def index_line(it: dict) -> str:
    flag = "" if it.get("status") == "distilled" else " [not yet distilled into requirements]"
    summary = it.get("summary") or "(no summary yet)"
    use = f" Use for: {it['useFor']}" if it.get("useFor") else ""
    return f"- `{it['id']}` ({it['kind']}, {it['date']}, {len(it['files'])} file(s)) **{it['title']}**: {summary}{use}{flag}"


def index_markdown(state_dir: Path, items: list[dict]) -> str:
    out = ["# Project knowledge", "",
           f"Client transcripts, documents and test files. Each item lives in `{kdir(state_dir)}/items/<id>/`: "
           "`original/` holds the files as given, `text/` a readable version. Read only the items a ticket needs.", ""]
    for it in sorted(items, key=lambda i: i["date"], reverse=True):
        out.append(index_line(it))
        for f in it["files"]:
            out.append(f"  - {f['name']} ({f['bytes'] // 1024} KB){': ' + f['text'] if f.get('text') else ''}")
        for d in it.get("duplicates", []):
            out.append(f"  - {d['name']}: duplicate of {d['sameAs']}, not copied")
    return "\n".join(out) + "\n"


def ticket_note(items: list[dict]) -> str:
    """The knowledge a ticket's planner picked, for the implement, review and fix prompts."""
    if not items:
        return ""
    lines = ["## Project knowledge for this ticket", "",
             "The planner picked these client materials as relevant. Read them when the ticket's details depend on them; "
             "they are confidential: never copy their content into code comments, commits or PR text.", ""]
    for it in items:
        lines.append(f"- **{it['title']}** ({it['kind']}, {it['date']}): {it.get('summary') or ''}")
        for f in it["files"]:
            lines.append(f"  - {f.get('text') or f['path']}" + (f" (original: {f['path']})" if f.get("text") else ""))
    return "\n".join(lines)


# --- decisions log -------------------------------------------------------------------

def log_decisions(state_dir: Path, ticket_ref: str, title: str, decisions: list[str], pr: str | None) -> bool:
    """Append a shipped ticket's decisions to decisions.md, newest first; False when it had none."""
    decisions = [d for d in decisions if d and d.strip().lower().rstrip(".") not in ("none", "")]
    if not decisions:
        return False
    path = state_dir / "decisions.md"
    head = "# Decisions\n\nWhat shipped work decided, newest first. Agents follow these unless a ticket says otherwise.\n"
    existing = path.read_text() if path.is_file() else ""
    older = existing.split("\n## ", 1)[1] if "\n## " in existing else ""
    entry = (f"\n## {time.strftime('%Y-%m-%d')} {ticket_ref} {title}{' (' + pr + ')' if pr else ''}\n\n"
             + "\n".join(f"- {d}" for d in decisions) + "\n")
    path.write_text(head + entry + ("\n## " + older if older else ""))
    return True
