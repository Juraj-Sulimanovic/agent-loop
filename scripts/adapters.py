"""Run one agent stage headless in Claude Code, Codex or Cursor and return its JSON result.

`mode` sets what the agent may do in its worktree:

    read   look only (plan, handoff)
    check  look and run commands such as tests, but not edit (review)
    write  edit and run commands (implement, fix)

Each tool's own permission system enforces as much of that as it can; the runner also
fingerprints the worktree around `check` stages.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

TOOLS = ("claude", "codex", "cursor")


class AgentError(RuntimeError):
    pass


def missing_keys(result, schema: dict) -> list[str]:
    if not isinstance(result, dict):
        return ["(not an object)"]
    return [k for k in schema.get("required", []) if k not in result]


def extract_json(text: str):
    """The last JSON object in a reply that may wrap it in prose or a code fence."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder, found, i = json.JSONDecoder(), None, 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, length = decoder.raw_decode(text[i:])
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict):
            found = obj
        i += length  # skip the whole object, so a nested one never wins over its parent
    return found


def run(cmd: list[str], cwd: Path, timeout: int, log_path: Path | None, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        out = subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=timeout,
                             env={**os.environ, "AGENT_LOOP": "1"})
    except subprocess.TimeoutExpired as e:
        raise AgentError(f"{cmd[0]} timed out after {timeout // 60} min") from e
    except FileNotFoundError as e:
        raise AgentError(f"{cmd[0]} is not installed") from e
    if log_path:
        with open(log_path, "a") as f:
            f.write(f"$ {' '.join(cmd[:12])}{' …' if len(cmd) > 12 else ''}\n--- stdout\n{out.stdout}\n--- stderr\n{out.stderr}\n")
    return out


def git_common_dir(cwd: Path) -> str | None:
    out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                         capture_output=True, text=True)
    return out.stdout.strip() or None


# --- Claude Code ------------------------------------------------------------------

def claude_cmd(schema: dict, mode: str, model: str | None, extra: list[str]) -> list[str]:
    cmd = ["claude", "-p", "--output-format", "json", "--json-schema", json.dumps(schema), "--permission-mode", "auto"]
    if mode != "write":
        cmd += ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
    if model:
        cmd += ["--model", model]
    return cmd + extra


def parse_claude(stdout: str, schema: dict) -> dict:
    data = extract_json(stdout)
    if not isinstance(data, dict):
        raise AgentError(f"claude printed no JSON: {stdout[-300:]}")
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        raise AgentError(f"claude failed: {data.get('subtype')}: {str(data.get('result'))[:300]}")
    result = data.get("structured_output")
    if result is None:
        result = extract_json(data.get("result", ""))
    if missing_keys(result, schema):
        raise AgentError(f"claude result missing {missing_keys(result, schema)}")
    return result


def run_claude(prompt, schema, cwd, mode, model, timeout, log_path, extra) -> dict:
    out = run(claude_cmd(schema, mode, model, extra), cwd, timeout, log_path, stdin=prompt)
    if out.returncode and not out.stdout.strip():
        raise AgentError(f"claude exited {out.returncode}: {out.stderr.strip()[-300:]}")
    return parse_claude(out.stdout, schema)


# --- Codex --------------------------------------------------------------------

def codex_cmd(cwd: Path, schema_file: str, out_file: str, mode: str, model: str | None, extra: list[str]) -> list[str]:
    cmd = ["codex", "exec", "-C", str(cwd), "--output-schema", schema_file, "-o", out_file, "--ephemeral", "--color", "never"]
    if mode == "read":
        cmd += ["-s", "read-only"]
    else:
        cmd += ["-s", "workspace-write"]
        common = git_common_dir(cwd)
        if common:  # `git add` in a worktree writes the index under <common>/worktrees/<name>/
            cmd += ["--add-dir", common]
    if model:
        cmd += ["-m", model]
    return cmd + extra + ["-"]


def run_codex(prompt, schema, cwd, mode, model, timeout, log_path, extra) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        schema_file, out_file = os.path.join(tmp, "schema.json"), os.path.join(tmp, "out.json")
        Path(schema_file).write_text(json.dumps(schema))
        out = run(codex_cmd(cwd, schema_file, out_file, mode, model, extra), cwd, timeout, log_path, stdin=prompt)
        text = Path(out_file).read_text() if Path(out_file).is_file() else ""
    if out.returncode:
        raise AgentError(f"codex exited {out.returncode}: {out.stderr.strip()[-300:]}")
    result = extract_json(text or out.stdout)
    if missing_keys(result, schema):
        raise AgentError(f"codex result missing {missing_keys(result, schema)}: {text[-300:]}")
    return result


# --- Cursor ---------------------------------------------------------------------

def cursor_cmd(cwd: Path, mode: str, model: str | None, extra: list[str], resume: str | None = None) -> list[str]:
    cmd = ["cursor-agent", "-p", "--output-format", "json", "--workspace", str(cwd)]
    cmd += ["--mode", "plan"] if mode == "read" else ["--force"]
    if model:
        cmd += ["--model", model]
    if resume:
        cmd += ["--resume", resume]
    return cmd + extra


def cursor_prompt(prompt: str, schema: dict) -> str:
    return (f"{prompt}\n\n## Reply format\n\nEnd your reply with only a JSON object (no code fence, no prose after it) "
            f"matching this JSON Schema:\n\n{json.dumps(schema)}\n")


def parse_cursor(stdout: str) -> tuple[object, str | None]:
    data = extract_json(stdout)
    if not isinstance(data, dict):
        raise AgentError(f"cursor-agent printed no JSON: {stdout[-300:]}")
    if data.get("is_error"):
        raise AgentError(f"cursor-agent failed: {str(data.get('result'))[:300]}")
    return extract_json(data.get("result", "")), data.get("session_id")


def run_cursor(prompt, schema, cwd, mode, model, timeout, log_path, extra) -> dict:
    out = run(cursor_cmd(cwd, mode, model, extra) + [cursor_prompt(prompt, schema)], cwd, timeout, log_path)
    if out.returncode:
        raise AgentError(f"cursor-agent exited {out.returncode}: {(out.stderr or out.stdout).strip()[-300:]}")
    result, session = parse_cursor(out.stdout)
    if missing_keys(result, schema) and session:
        retry = ("Your last reply did not end with a valid JSON object. Reply now with only the JSON object, "
                 f"matching this schema:\n\n{json.dumps(schema)}")
        out = run(cursor_cmd(cwd, mode, model, extra, resume=session) + [retry], cwd, timeout, log_path)
        result, _ = parse_cursor(out.stdout)
    if missing_keys(result, schema):
        raise AgentError(f"cursor-agent result missing {missing_keys(result, schema)}")
    return result


RUNNERS = {"claude": run_claude, "codex": run_codex, "cursor": run_cursor}


def run_agent(*, tool: str, prompt: str, schema: dict, cwd: Path, mode: str = "write", model: str | None = None,
              timeout: int = 1800, log_path: Path | None = None, extra_args: list[str] | None = None, **_) -> dict:
    if tool not in RUNNERS:
        raise AgentError(f"Unknown agent tool {tool!r}; use one of {', '.join(TOOLS)}")
    return RUNNERS[tool](prompt, schema, Path(cwd), mode, model, timeout, log_path, list(extra_args or []))
