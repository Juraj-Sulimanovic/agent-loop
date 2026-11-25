# Security

agent-loop runs coding agents that edit code in your repositories, reads tracker credentials from
your environment, and prepares commands that push to GitHub. Please report security problems
privately.

## Reporting a problem

Use **Report a vulnerability** on the repository's
[Security tab](https://github.com/Juraj-Sulimanovic/agent-loop/security/advisories/new). Include
what you found, how to reproduce it, and what an attacker could do with it. Do not open a public
issue.

I aim to answer within a week. Fixes go into the next release, and the changelog credits
you unless you ask not to be named.

## Supported versions

Only the latest release gets security fixes.

## What counts

For example: an agent getting write access outside its ticket worktree, a credential written to
a file or a log, a command run without the approval the skills promise, or untrusted ticket or
document content steering an agent into actions the user did not approve.
