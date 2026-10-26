Does the change do what the ticket asks, all of it, and only that? Quote the ticket line for each finding.

- An acceptance criterion or requirement that is missing or only partly met: blocker when the core behaviour is missing, otherwise major.
- Something that looks implemented but behaves differently from what the ticket says (a different message, a different boundary, a different default): blocker.
- Behaviour the ticket did not ask for (scope creep): minor, or major when it changes something other code or users rely on.

Check the tests against the criteria too: a criterion with no test that would fail if it broke is major.
