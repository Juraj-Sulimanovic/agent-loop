A reviewer of ticket {{ref}} "{{name}}" reported the finding below about the uncommitted change in the worktree `{{worktree}}` (your working directory; branch `{{branch}}`, base `{{base}}`, diff with `git diff {{base}}`). Your job is to try to **refute** it. Do not edit any file.

## The finding ({{lens}} reviewer, reported as {{severity}})

{{finding}}

## Check it

{{reviewFileNote}}

Read the code at the location and what it depends on. Where it is cheap, show it: run the relevant test, or a one-off command that exercises the input in question (without changing files). Read `AGENTS.md` when the finding cites a convention, and the ticket below when it cites a criterion.

Return `refuted` when the code does not have the problem, the cited rule or criterion does not say that, or the claim rests on a misreading. Return `confirmed` only when you can point at the evidence yourself, and give the severity you think is right (blocker, major or minor) under the same scale: blocker = wrong behaviour, broken criterion, security or data-handling flaw; major = what a careful human reviewer would send back; minor = a judgement call. When in doubt, refute: a fix round on a false finding costs more than a missed nit. Put your evidence in `reason`.

## The ticket

{{desc}}
