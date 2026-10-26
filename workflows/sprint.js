export const meta = {
  name: 'sprint',
  description: 'Plan, implement, review and fix a batch of Trello tickets in parallel worktrees, ending with staged changes and a handoff note per ticket',
  whenToUse:
    'Invoked by /agent-loop:sprint and /agent-loop:ticket after `agentloop.py prepare` has created the worktrees. Requires args {project: {root, briefPath, checks, exclusive, maxParallel, maxFixRounds}, tickets: [prepared ticket objects], stopAfter?: "plan"}. Never commits or pushes; /agent-loop:ship does that after the user confirms.',
  phases: [
    { title: 'Plan', detail: 'one planner per ticket, in its worktree' },
    { title: 'Build', detail: 'implement, independent review, fix until clean or out of rounds' },
    { title: 'Handoff', detail: 'handoff note, commit message and PR body per ticket' },
  ],
}

// Order matters for resume: plans run behind one barrier because batching needs every plan,
// and the batch is a pure function of args and the journaled plans, so a resumed run replays
// all planners and only the build agents that had not finished run live.

const ARGS = typeof args === 'string' ? JSON.parse(args) : args
if (!ARGS || !ARGS.project || !Array.isArray(ARGS.tickets) || !ARGS.tickets.length) {
  throw new Error('sprint workflow requires args {project: {root, briefPath, ...}, tickets: [...]}')
}
const P = ARGS.project
const CHECKS = P.checks || []
const EXCLUSIVE = P.exclusive || []
const MAX_PARALLEL = Math.max(1, Math.min(P.maxParallel || 10, 16))
const MAX_FIX = Math.max(0, Math.min(P.maxFixRounds == null ? 2 : P.maxFixRounds, 4))
const tickets = ARGS.tickets.filter(t => t && !t.error)

const checksLine = CHECKS.length
  ? `The checks are: ${CHECKS.map(c => '`' + c + '`').join(', ')}. Run them from the worktree.`
  : 'No checks are configured; run whatever the repository instructions name for linting and tests.'

function header(t) {
  return [
    `You are working on Trello card #${t.id} "${t.name}" (${t.url}).`,
    `Your worktree is \`${t.worktree}\` on branch \`${t.branch}\`, cut from \`${t.base}\`. Work only inside it: \`cd\` there first and use absolute paths under it.`,
    `Read \`${t.worktree}/AGENTS.md\` (or CLAUDE.md) before anything else and follow it; it overrides general habits. Read the project brief at \`${P.briefPath}\` for the sprint goal, client requirements, the rest of the board and what other agents did.`,
    'Never run `git commit`, `git push`, `gh pr create` or anything that changes a remote, a branch or history. Staging with `git add` is allowed and expected.',
    '',
    '## The card',
    '',
    t.desc || '(no description)',
  ].join('\n')
}

const PLAN = {
  type: 'object',
  properties: {
    feasible: { type: 'boolean', description: 'false when the card cannot be done without a human decision' },
    question: { type: 'string', description: 'when not feasible: the one question a human must answer' },
    summary: { type: 'string' },
    files: { type: 'array', items: { type: 'string' }, description: 'repo-relative paths that will change or be created' },
    steps: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
  },
  required: ['feasible', 'summary', 'files', 'steps'],
}

const BUILD = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    checks: { type: 'array', items: { type: 'object', properties: { cmd: { type: 'string' }, ok: { type: 'boolean' } }, required: ['cmd', 'ok'] } },
    stuck: { type: 'string', description: 'set only when you could not finish; what blocks you' },
  },
  required: ['summary', 'checks'],
}

const REVIEW = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['pass', 'fix'] },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['blocker', 'major', 'minor'] },
          file: { type: 'string' },
          line: { type: 'number' },
          issue: { type: 'string' },
          fix: { type: 'string' },
        },
        required: ['severity', 'issue'],
      },
    },
    unmetCriteria: { type: 'array', items: { type: 'string' } },
    checks: { type: 'array', items: { type: 'object', properties: { cmd: { type: 'string' }, ok: { type: 'boolean' } }, required: ['cmd', 'ok'] } },
  },
  required: ['verdict', 'findings', 'unmetCriteria', 'checks'],
}

const HANDOFF = {
  type: 'object',
  properties: {
    commitMessage: { type: 'string' },
    prTitle: { type: 'string' },
    prChanges: { type: 'array', items: { type: 'string' } },
  },
  required: ['commitMessage', 'prTitle', 'prChanges'],
}

// ---- Plan -----------------------------------------------------------------

phase('Plan')
const plans = await parallel(tickets.map(t => () =>
  agent(
    [
      header(t),
      '',
      '## Your job: plan, do not implement',
      '',
      `Read the code the card touches and write a plan to \`${P.root}/.agent-loop/plans/${t.id}.md\`: the approach, the files that change, the steps, and the risks. Reuse what exists; name only code that exists on the branch.`,
      'List every repo-relative file you expect to change or create in `files`; another planner works beside you and two tickets touching the same file are not run together, so be complete rather than minimal.',
      'Set feasible=false only when the card cannot be done without a decision a human must make (contradicting requirements, missing access, an unanswerable ambiguity), and put that one question in `question`. Ordinary uncertainty is a risk, not a blocker.',
    ].join('\n'),
    { label: `plan:#${t.id}`, phase: 'Plan', schema: PLAN },
  )))

// ---- Batch: no two tickets touching the same file or exclusive area --------

function claims(plan) {
  const out = new Set()
  for (const f of plan.files || []) {
    const area = EXCLUSIVE.find(prefix => f.startsWith(prefix))
    out.add(area ? `area:${area}` : `file:${f}`)
  }
  return out
}

const batch = [], deferred = [], needsHuman = []
const taken = new Map()
tickets.forEach((t, i) => {
  const plan = plans[i]
  if (!plan) return deferred.push({ id: t.id, reason: 'planner failed; run it again' })
  if (!plan.feasible) return needsHuman.push({ id: t.id, name: t.name, question: plan.question || plan.summary })
  if (batch.length >= MAX_PARALLEL) return deferred.push({ id: t.id, reason: `over maxParallel (${MAX_PARALLEL})` })
  const mine = claims(plan)
  const clash = [...mine].find(c => taken.has(c))
  if (clash) return deferred.push({ id: t.id, reason: `overlaps #${taken.get(clash)} on ${clash.replace(/^\w+:/, '')}` })
  mine.forEach(c => taken.set(c, t.id))
  batch.push({ t, plan })
})
log(`Batch: ${batch.map(b => '#' + b.t.id).join(', ') || 'none'}`)
if (deferred.length) log(`Deferred: ${deferred.map(d => `#${d.id} (${d.reason})`).join('; ')}`)
if (needsHuman.length) log(`Needs a human: ${needsHuman.map(n => '#' + n.id).join(', ')}`)

if (ARGS.stopAfter === 'plan') {
  return { stoppedAfter: 'plan', plans: batch.map(b => ({ id: b.t.id, ...b.plan })), deferred, needsHuman }
}

// ---- Build: implement -> review -> fix, per ticket, no barrier --------------

function blocking(review) {
  return review.findings.filter(f => f.severity !== 'minor').length > 0 || review.unmetCriteria.length > 0
    || review.checks.some(c => !c.ok)
}

function reviewPrompt(t, round) {
  return [
    `You are reviewing card #${t.id} "${t.name}" (${t.url}). The change is uncommitted in \`${t.worktree}\` on \`${t.branch}\`, cut from \`${t.base}\`; read it with \`git -C ${t.worktree} diff ${t.base}\` plus \`git -C ${t.worktree} status\` for new files. Do not edit any file.`,
    `Read \`${t.worktree}/AGENTS.md\` (or CLAUDE.md): its conventions are the review standard. Read the project brief at \`${P.briefPath}\` for the requirements the card serves.`,
    '',
    '## The card',
    '',
    t.desc || '(no description)',
    '',
    '## Review',
    '',
    '1. Spec: each acceptance criterion the change does not yet meet goes in `unmetCriteria`, quoted.',
    '2. Correctness: real bugs, broken edge cases, missing error handling the conventions require, tests that do not test the behaviour. Cite file and line.',
    '3. Conventions: departures from the repository instructions.',
    `4. ${checksLine} Record each result.`,
    'Severity: blocker = wrong behaviour or a failing check; major = a convention or criterion miss a reviewer would send back; minor = taste. Do not pad: an empty findings list is a fine answer. verdict is fix when anything is blocker or major, a criterion is unmet, or a check fails.',
    round ? `This is re-review round ${round}: judge the current state, not the history.` : '',
  ].join('\n')
}

const built = await pipeline(
  batch,
  ({ t }) => agent(
    [
      header(t),
      '',
      '## Your job: implement',
      '',
      `The plan is in \`${P.root}/.agent-loop/plans/${t.id}.md\`; follow it unless the code proves it wrong, and say so if it does.`,
      `Implement the card in full, with the tests the repository instructions ask for. ${checksLine} Leave every check passing.`,
      'Finish with all your changes staged (`git add -A` in the worktree) and nothing else changed. If you cannot finish, stage what is sound and say what blocks you in `stuck`.',
    ].join('\n'),
    { label: `build:#${t.id}`, phase: 'Build', schema: BUILD },
  ).then(build => ({ build })),
  async (prev, { t }) => {
    if (!prev || !prev.build) return { t, status: 'needs-human', why: 'implementer failed' }
    if (prev.build.stuck) return { t, status: 'needs-human', why: prev.build.stuck, build: prev.build }
    let review = await agent(reviewPrompt(t, 0), { label: `review:#${t.id}`, phase: 'Build', schema: REVIEW })
    let round = 0
    while (review && blocking(review) && round < MAX_FIX) {
      round++
      const todo = review.findings.filter(f => f.severity !== 'minor')
      await agent(
        [
          header(t),
          '',
          `## Your job: address review round ${round}`,
          '',
          'An independent reviewer read the staged change and found the problems below. Fix each one, or, when a finding is wrong, leave the code and say why in your summary. Keep the change scoped to the card.',
          '',
          ...todo.map(f => `- [${f.severity}] ${f.file ? f.file + (f.line ? ':' + f.line : '') + ': ' : ''}${f.issue}${f.fix ? ' (suggested: ' + f.fix + ')' : ''}`),
          ...review.unmetCriteria.map(c => `- [unmet criterion] ${c}`),
          ...review.checks.filter(c => !c.ok).map(c => `- [failing check] \`${c.cmd}\``),
          '',
          `${checksLine} Finish with everything staged.`,
        ].join('\n'),
        { label: `fix${round}:#${t.id}`, phase: 'Build', schema: BUILD },
      )
      review = await agent(reviewPrompt(t, round), { label: `review${round}:#${t.id}`, phase: 'Build', schema: REVIEW })
    }
    if (!review) return { t, status: 'needs-human', why: 'reviewer failed', build: prev.build }
    const clean = !blocking(review)
    return {
      t, build: prev.build, review, rounds: round,
      status: clean ? 'ready' : 'needs-human',
      why: clean ? '' : `still failing review after ${round} fix round(s)`,
    }
  },
  (res, { t }) => {
    if (!res) return { t, status: 'needs-human', why: 'build stage crashed' }
    return agent(
      [
        `Card #${t.id} "${t.name}" (${t.url}) has been implemented in \`${t.worktree}\` (branch \`${t.branch}\`, base \`${t.base}\`). Status: ${res.status}${res.why ? ' (' + res.why + ')' : ''}. Do not edit code.`,
        `Read the staged diff (\`git -C ${t.worktree} diff --cached ${t.base}\`), the plan at \`${P.root}/.agent-loop/plans/${t.id}.md\`, and \`${t.worktree}/AGENTS.md\` for the commit message and pull request conventions.`,
        '',
        `Write \`${P.root}/.agent-loop/handoffs/${t.id}.md\` exactly in this shape; the next agents on this project read it to learn what happened here:`,
        '',
        '```',
        '---',
        `id: ${t.id}`,
        `title: ${t.name}`,
        `status: ${res.status}`,
        `branch: ${t.branch}`,
        `base: ${t.base.replace(/^origin\//, '')}`,
        `worktree: ${t.worktree}`,
        `card: ${t.url}`,
        `updated: ${ARGS.stamp || ''}`,
        '---',
        '',
        '## Summary',
        'Two or three sentences: what the change does, in a reviewer\'s words.',
        '',
        '## Decisions',
        '- Choices another agent on this codebase should know and follow (a shape, a name, a place for things). None is fine.',
        '',
        '## Follow-ups',
        '- Work this ticket surfaced but did not do. None is fine.',
        '',
        '## Open review findings',
        '- Unresolved findings from the last review, or "None".',
        '',
        '## Commit message',
        'one line, per the repository conventions',
        '',
        '## PR title',
        'a sentence, per the repository conventions',
        '',
        '## PR changes',
        '- three to five bullets, one behaviour each',
        '```',
        '',
        res.review && res.review.findings.length
          ? 'Last review findings:\n' + res.review.findings.map(f => `- [${f.severity}] ${f.file || ''} ${f.issue}`).join('\n')
          : 'The last review raised nothing.',
        res.why ? `Why it needs a human: ${res.why}. Say that first in the Summary.` : '',
        '',
        'Return the commit message, PR title and PR change bullets you wrote.',
      ].join('\n'),
      { label: `handoff:#${t.id}`, phase: 'Handoff', schema: HANDOFF, effort: 'low' },
    ).then(h => ({ ...res, handoff: h }))
  },
)

const done = built.filter(Boolean)
return {
  ready: done.filter(r => r.status === 'ready').map(r => ({ id: r.t.id, name: r.t.name, branch: r.t.branch, rounds: r.rounds, ...(r.handoff || {}) })),
  needsHuman: [
    ...needsHuman,
    ...done.filter(r => r.status !== 'ready').map(r => ({ id: r.t.id, name: r.t.name, question: r.why })),
  ],
  deferred,
}
