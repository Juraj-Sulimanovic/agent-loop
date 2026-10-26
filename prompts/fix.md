{{header}}

## Your job: address review round {{round}}

Independent reviewers read the staged change, and a skeptic confirmed each finding below. Each one names a class of bug: fix every instance of that class in the change, not only the cited line. When a finding is wrong, leave the code and dispute it with the reason; the next reviewers drop it unless your reason is wrong. Keep the change scoped to the ticket.

{{findings}}

{{checksLine}} Finish with everything staged (`git add -A`). In `responses`, answer every numbered finding (`n`): `fixed` with what you changed, or `disputed` with why. `stuck` is null unless you could not finish.
