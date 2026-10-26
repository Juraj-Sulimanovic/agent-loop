Does the code work, for every input it will see? Trace the changed paths with real values, not by reading the happy path.

- Logic errors, off-by-one and boundary conditions (empty, zero, one, very large, missing, duplicate, unicode, time zones and month ends).
- Error handling: failures swallowed, wrong error raised, partial writes left behind, the error contract the repository documents not followed.
- API and library misuse; resources not closed; concurrency and ordering assumptions.
- Security and data handling: missing authorization checks, injection, secrets or personal data written to logs, output or files they should not reach.
- Tests that pass without testing the behaviour (asserting on mocks, no assertion on the outcome).
{{checksNote}}
