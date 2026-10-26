This change touches paths the project marks sensitive ({{sensitive}}). Look for what would hurt if it shipped:

- Authentication and authorization: a check removed or bypassed, a new route or action without one, trust in client-supplied identity.
- Injection: SQL, shell, template, path traversal, deserialization of untrusted input.
- Secrets and credentials: written to code, logs, error messages or files; tokens with wider scope or longer life than needed.
- Personal and client data: reaching logs, analytics, prompts, test fixtures or the repository.
- CI, build and deploy files: new third-party actions or scripts, widened permissions, secrets exposed to untrusted runs, steps that skip checks.
- Migrations and data changes: irreversible steps, locks on large tables, data loss on rollback.

Every security problem the change introduces is a blocker unless it is clearly harmless; say why in the evidence.
