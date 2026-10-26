Does the change follow how this repository says code is written? Read the documented standards first: `AGENTS.md` / `CLAUDE.md`, `CONTRIBUTING.md`, and any conventions files or notes they point to.

- A documented standard the diff breaks: major. Cite the file and the rule.
- Anything a linter or formatter already enforces: skip it; the checks run separately.

Then the smell baseline (Fowler, *Refactoring* ch. 3, as used by Matt Pocock's code-review skill). These are always judgement calls, reported as **minor**, and a documented repository standard that endorses the pattern overrides them:

- Mysterious Name: a name that does not reveal what it does or holds.
- Duplicated Code: the same logic shape in more than one hunk or file.
- Feature Envy: a function that uses another object's data more than its own.
- Data Clumps: the same few fields or parameters travelling together.
- Primitive Obsession: a primitive standing in for a domain concept.
- Repeated Switches: the same switch or if-cascade on the same type in several places.
- Shotgun Surgery: one logical change scattered across many files.
- Divergent Change: one module edited for several unrelated reasons.
- Speculative Generality: abstraction or hooks for needs the ticket does not have.
- Message Chains: long navigation chains the caller should not depend on.
- Middle Man: a function or class that only delegates.
- Refused Bequest: a subclass that ignores most of what it inherits.
