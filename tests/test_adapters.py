"""Adapters: the command line each tool gets, and parsing of recorded headless output.

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import adapters  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["files", "note"],
          "properties": {"files": {"type": "array", "items": {"type": "string"}}, "note": {"type": ["string", "null"]}}}


class CommandTest(unittest.TestCase):
    def test_claude_modes(self):
        write = adapters.claude_cmd(SCHEMA, "write", None, [])
        self.assertEqual(write[:4], ["claude", "-p", "--output-format", "json"])
        self.assertEqual(json.loads(write[write.index("--json-schema") + 1]), SCHEMA)
        self.assertNotIn("--disallowedTools", write)
        check = adapters.claude_cmd(SCHEMA, "check", "opus", ["--effort", "low"])
        self.assertIn("--disallowedTools", check)
        self.assertEqual(check[-4:], ["--model", "opus", "--effort", "low"])

    def test_codex_modes(self):
        read = adapters.codex_cmd(Path("/w"), "s.json", "o.json", "read", None, [])
        self.assertEqual(read[read.index("-s") + 1], "read-only")
        self.assertEqual(read[-1], "-")
        write = adapters.codex_cmd(Path(__file__).parent, "s.json", "o.json", "write", "gpt-5", ["--ignore-user-config"])
        self.assertEqual(write[write.index("-s") + 1], "workspace-write")
        self.assertIn("--add-dir", write)  # the git common dir, for `git add` in a worktree
        self.assertEqual(write[-4:], ["-m", "gpt-5", "--ignore-user-config", "-"])

    def test_cursor_modes(self):
        self.assertEqual(adapters.cursor_cmd(Path("/w"), "read", None, [])[-2:], ["--mode", "plan"])
        self.assertIn("--trust", adapters.cursor_cmd(Path("/w"), "read", None, []))   # fresh worktrees are untrusted
        self.assertIn("--force", adapters.cursor_cmd(Path("/w"), "check", None, []))
        self.assertEqual(adapters.cursor_cmd(Path("/w"), "write", None, [], resume="abc")[-2:], ["--resume", "abc"])

    def test_unknown_tool(self):
        with self.assertRaises(adapters.AgentError):
            adapters.run_agent(tool="gemini", prompt="", schema=SCHEMA, cwd=Path("."))


class ParseTest(unittest.TestCase):
    def test_claude_recorded_output(self):
        got = adapters.parse_claude((FIXTURES / "claude-print.json").read_text(), SCHEMA)
        self.assertEqual(got["files"], ["a.py"])

    def test_claude_error(self):
        with self.assertRaises(adapters.AgentError):
            adapters.parse_claude(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}), SCHEMA)

    def test_extract_json_from_prose_and_fences(self):
        self.assertEqual(adapters.extract_json('Done.\n```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(adapters.extract_json('first {"a": 1} then {"b": {"c": 2}}'), {"b": {"c": 2}})
        self.assertIsNone(adapters.extract_json("no json here"))

    def test_codex_recorded_output(self):
        calls = []

        def fake_run(cmd, cwd, timeout, log_path, stdin=None):
            calls.append((cmd, stdin))
            Path(cmd[cmd.index("-o") + 1]).write_text((FIXTURES / "codex-last-message.json").read_text())
            return subprocess.CompletedProcess(cmd, 0, "", "")

        orig, adapters.run = adapters.run, fake_run
        try:
            got = adapters.run_agent(tool="codex", prompt="list files", schema=SCHEMA, cwd=Path("/tmp"), mode="read")
        finally:
            adapters.run = orig
        self.assertEqual(got, {"files": ["a.py"], "note": None})
        self.assertEqual(calls[0][1], "list files")

    def test_cursor_parses_and_retries_once(self):
        replies = ['{"type":"result","is_error":false,"session_id":"s1","result":"I listed them, all good."}',
                   (FIXTURES / "cursor-print.json").read_text()]
        calls = []

        def fake_run(cmd, cwd, timeout, log_path, stdin=None):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, replies[len(calls) - 1], "")

        orig, adapters.run = adapters.run, fake_run
        try:
            got = adapters.run_agent(tool="cursor", prompt="list files", schema=SCHEMA, cwd=Path("/tmp"), mode="check")
        finally:
            adapters.run = orig
        self.assertEqual(got["files"], ["a.py"])
        self.assertIn("matching this JSON Schema", calls[0][-1])
        self.assertIn("--resume", calls[1])

    def test_cursor_gives_up_after_retry(self):
        bad = '{"type":"result","is_error":false,"session_id":"s1","result":"no json"}'
        orig = adapters.run
        adapters.run = lambda cmd, cwd, timeout, log_path, stdin=None: subprocess.CompletedProcess(cmd, 0, bad, "")
        try:
            with self.assertRaises(adapters.AgentError):
                adapters.run_agent(tool="cursor", prompt="x", schema=SCHEMA, cwd=Path("/tmp"))
        finally:
            adapters.run = orig


if __name__ == "__main__":
    unittest.main()
