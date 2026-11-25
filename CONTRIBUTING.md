# Contributing

Issues and pull requests are welcome.

## Before you start

- **Bugs:** open an issue with the bug form. Remove tokens, client data and private code from
  anything you paste.
- **Small fixes** (a bug, a doc, a prompt wording): open a pull request directly.
- **Larger features:** open an issue first, so that we agree on the approach before you spend the
  time.

## Making a change

1. Fork the repository and make a branch.
2. Keep one change per pull request, with a test that fails without it.
3. Run the tests and the plugin validation:

   ```bash
   python3 -m unittest discover -s tests
   claude plugin validate .
   ```

   The tests need Python 3.10 or later and `git`. The Workflow tests also need Node; without it
   they are skipped. No test calls an agent tool.
4. Use conventional commit messages: `feat: …`, `fix: …`, `docs: …`, `chore: …`.
5. In the pull request, say what changed, why, and how you tested it.

agent-loop has two engines for the same loop: `workflows/sprint.js` (Claude Code) and
`scripts/runner.py` (Codex, Cursor and the terminal). A change to the loop usually needs both,
with a test for each.

AI-assisted contributions are fine. You are responsible for what you submit: read it, test it,
and be ready to explain it.

## Security

Please do not open a public issue for a security problem. See [SECURITY.md](SECURITY.md).
