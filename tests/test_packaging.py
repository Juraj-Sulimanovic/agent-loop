"""The three plugin manifests (Claude Code, Codex, Cursor) agree and point at files that exist.

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = {tool: json.loads((ROOT / f".{tool}-plugin" / "plugin.json").read_text()) for tool in ("claude", "codex", "cursor")}


class PackagingTest(unittest.TestCase):
    def test_one_name_and_version(self):
        self.assertEqual({m["name"] for m in MANIFESTS.values()}, {"agent-loop"})
        self.assertEqual(len({m["version"] for m in MANIFESTS.values()}), 1, {t: m["version"] for t, m in MANIFESTS.items()})

    def test_paths_exist(self):
        for tool, m in MANIFESTS.items():
            for key in ("skills", "hooks"):
                if key in m:
                    self.assertTrue((ROOT / m[key]).exists(), f"{tool} {key}: {m[key]}")
        self.assertTrue((ROOT / "hooks" / "hooks.json").is_file())  # Claude Code and Codex default

    def test_hooks_call_the_cli(self):
        for name, var in (("hooks.json", "${CLAUDE_PLUGIN_ROOT}"), ("cursor-hooks.json", "${CURSOR_PLUGIN_ROOT}")):
            text = (ROOT / "hooks" / name).read_text()
            self.assertIn(f"{var}/scripts/agentloop.py", text)
            self.assertIn("brief --hook", text)

    def test_marketplaces_point_here(self):
        claude = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        codex = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
        self.assertEqual(claude["plugins"][0]["source"], "./")
        self.assertEqual(codex["plugins"][0]["source"], {"source": "local", "path": "./"})

    def test_every_skill_has_frontmatter(self):
        for skill in (ROOT / "skills").iterdir():
            text = (skill / "SKILL.md").read_text()
            self.assertTrue(text.startswith(f"---\nname: {skill.name}\n"), skill.name)
            self.assertIn("\ndescription: ", text.split("\n---", 1)[0])


if __name__ == "__main__":
    unittest.main()
