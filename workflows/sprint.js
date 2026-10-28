export const meta = {
  name: 'sprint',
  description: 'Plan, implement, review and fix a batch of tickets in parallel worktrees, ending with staged changes and a handoff note per ticket',
  whenToUse:
    'Invoked by the agent-loop sprint and ticket skills in Claude Code. Requires the JSON that `agentloop.py wf start` prints (prompts, schemas, project, tickets, run). Never commits or pushes; the ship skill does that after the user confirms.',
  phases: [
    { title: 'Plan', detail: 'one planner per ticket, in its worktree' },
    { title: 'Build', detail: 'implement, then fix what the review confirmed' },
    { title: 'Review', detail: 'checks, parallel reviewers, skeptics per blocking finding' },
    { title: 'Handoff', detail: 'handoff note, commit message and PR text per ticket' },
  ],
}

// The same loop as scripts/runner.py, for Claude Code's Workflow view. The prompts and output
// schemas come from the plugin's prompts/ folder through the args, so both engines say the same
// thing. Work that needs files or git (checks, snapshots, handoff notes) goes to `agentloop.py wf`.

const A = typeof args === 'string' ? JSON.parse(args) : args
if (!A || !A.prompts || !A.schemas || !Array.isArray(A.tickets) || !A.tickets.length) {
  throw new Error('sprint workflow requires the JSON `agentloop.py wf start` prints')
}
const P = A.project
const S = A.schemas
const REVIEW = P.review || {}
const DEFAULT_LENSES = ['spec', 'correctness', 'standards']
const LENSES = (REVIEW.lenses || DEFAULT_LENSES).filter(l => ['spec', 'correctness', 'standards', 'security'].includes(l))
const VERIFY = REVIEW.verify !== false
const VOTES = Math.max(1, REVIEW.votes || 3)
const MAX_PARALLEL = Math.max(1, Math.min(P.maxParallel || 10, 16))
const MAX_FIX = Math.max(0, Math.min(P.maxFixRounds == null ? 2 : P.maxFixRounds, 4))
const SEVERITY = { blocker: 0, major: 1, minor: 2 }
const ref = t => t.ref || '#' + t.id

// ---- rendering (scripts/runner.py: render, values) ---------------------------

function render(name, values) {
  const text = (A.prompts[name] || '').replace(/\{\{(\w+)\}\}/g, (_, k) => (values[k] == null ? '' : String(values[k])))
  return text.replace(/\n{3,}/g, '\n\n').trim() + '\n'
}

const checksLine = (P.checks || []).length
  ? 'The checks are: ' + P.checks.map(c => '`' + c + '`').join(', ') + '. Run them from the worktree.'
  : 'No checks are configured; run whatever the repository instructions name for linting and tests.'

function knowledgeNote(picked) {
  if (!picked) {
    return P.knowledgeIndex
      ? `## Project knowledge\n\nClient transcripts, documents and test files are indexed in \`${P.knowledgeIndex}\`, with paths to readable text versions. Read the index, then only the items this ticket needs.`
      : ''
  }
  const items = (picked.knowledge || []).map(id => (A.knowledge || []).find(k => k.id === id)).filter(Boolean)
  if (!items.length) return ''
  const lines = ['## Project knowledge for this ticket', '',
    'The planner picked these client materials as relevant. Read them when the ticket\'s details depend on them; they are confidential: never copy their content into code comments, commits or PR text.', '']
  for (const it of items) {
    lines.push(`- **${it.title}** (${it.kind}, ${it.date}): ${it.summary || ''}`)
    for (const f of it.files) lines.push(`  - ${f.text || f.path}` + (f.text ? ` (original: ${f.path})` : ''))
  }
  return lines.join('\n')
}

function values(t, picked, extra) {
  const base = {
    ref: ref(t), name: t.name, url: t.url || '', worktree: t.worktree, branch: t.branch, base: t.base,
    desc: t.desc || '(no description)', briefPath: P.briefPath || '', checksLine,
    knowledgeNote: knowledgeNote(picked),
    cwdNote: ` Run \`cd ${t.worktree}\` before anything else and use absolute paths under it.`,
  }
  base.header = render('header', base)
  return Object.assign(base, extra || {})
}

const reviewFileNote = t => t.reviewFile
  ? `\`${REVIEW.file || 'REVIEW.md'}\` at the worktree root is the code owners' guidance for review: what matters, what not to flag, which paths to skip. Where it differs from the defaults below, it wins.`
  : ''

// ---- findings (scripts/runner.py: dedupe, finding_text, blocking, class_key) ---

function findingText(f) {
  const where = f.file ? `${f.file}${f.line ? ':' + f.line : ''}: ` : ''
  return `${where}${f.issue}` + (f.evidence ? `\nEvidence: ${f.evidence}` : '') + (f.fix ? `\nSuggested fix: ${f.fix}` : '')
}

function dedupe(findings) {
  const out = new Map()
  for (const f of [...findings].sort((a, b) => (SEVERITY[a.severity] ?? 3) - (SEVERITY[b.severity] ?? 3))) {
    const key = f.file && f.line ? `${f.file}:${f.line}` : 'text:' + f.issue.trim().toLowerCase()
    const seen = out.get(key)
    if (seen) seen.lens = [...new Set(seen.lens.split(', ').concat(f.lens))].join(', ')
    else out.set(key, Object.assign({}, f))
  }
  return [...out.values()]
}

const blocking = r => r.findings.some(f => f.severity !== 'minor') || r.checks.some(c => !c.ok)
const classKey = f => `${(f.class || f.issue.slice(0, 60)).trim().toLowerCase()}|${f.file || ''}`

function claims(plan) {
  const out = new Set()
  for (const f of plan.files || []) {
    const area = (P.exclusive || []).find(prefix => f.startsWith(prefix))
    out.add(area ? `area:${area}` : `file:${f}`)
  }
  return out
}

// ---- schemas of the helper steps ------------------------------------------------

const CHECKS = {
  type: 'object', additionalProperties: false, required: ['checks', 'sensitive', 'tree'],
  properties: {
    checks: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['cmd', 'ok', 'tail'],
      properties: { cmd: { type: 'string' }, ok: { type: 'boolean' }, tail: { type: 'string' } } } },
    sensitive: { type: 'array', items: { type: 'string' } },
    tree: { type: 'string' },
  },
}
const SAVED = { type: 'object', additionalProperties: false, required: ['path'], properties: { path: { type: 'string' } } }

// ---- Plan -----------------------------------------------------------------------

phase('Plan')
const plans = await parallel(A.tickets.map(t => () =>
  agent(render('plan', values(t)), { label: `plan:${ref(t)}`, phase: 'Plan', schema: S.plan })))

const batch = [], deferred = [], needsHuman = []
const taken = new Map()
A.tickets.forEach((t, i) => {
  const plan = plans[i]
  if (!plan) return deferred.push({ id: t.id, ref: ref(t), reason: 'planner failed; run it again' })
  if (!plan.feasible) return needsHuman.push({ id: t.id, ref: ref(t), name: t.name, question: plan.question || plan.summary })
  if (batch.length >= MAX_PARALLEL) return deferred.push({ id: t.id, ref: ref(t), reason: `over maxParallel (${MAX_PARALLEL})` })
  const mine = claims(plan)
  const clash = [...mine].sort().find(c => taken.has(c))
  if (clash) return deferred.push({ id: t.id, ref: ref(t), reason: `overlaps ${taken.get(clash)} on ${clash.replace(/^\w+:/, '')}` })
  mine.forEach(c => taken.set(c, ref(t)))
  batch.push({ t, plan })
})
log(`Batch: ${batch.map(b => ref(b.t)).join(', ') || 'none'}`)
if (deferred.length) log(`Deferred: ${deferred.map(d => `${d.ref} (${d.reason})`).join('; ')}`)
if (needsHuman.length) log(`Needs you: ${needsHuman.map(n => n.ref).join(', ')}`)

if (A.planOnly) {
  return { stoppedAfter: 'plan', plans: batch.map(b => Object.assign({ id: b.t.id, ref: ref(b.t) }, b.plan)), deferred, needsHuman }
}

// ---- Build and review, per ticket, no barrier between tickets -------------------

function checks(t, plan, round) {
  const files = (plan.files || []).join(',')
  const cmd = `${A.al} wf checks ${t.worktree} --run ${A.run} --id ${t.id}${round ? ' --round ' + round : ''}${files ? ` --files '${files}'` : ''}`
  return agent(
    `Run exactly this command and return its JSON output as your result, unchanged:\n\n\`\`\`bash\n${cmd}\n\`\`\`\n\nDo nothing else.`,
    { label: `checks${round || ''}:${ref(t)}`, phase: 'Review', schema: CHECKS, effort: 'low' })
}

async function verdict(t, plan, label, f, votes) {
  const prompt = render('verify', values(t, plan, { lens: f.lens, severity: f.severity, finding: findingText(f), reviewFileNote: reviewFileNote(t) }))
  const labels = votes === 1 ? [label] : Array.from({ length: votes }, (_, k) => `${label}·v${k + 1}`)
  const answers = (await parallel(labels.map(lab => () => agent(prompt, { label: lab, phase: 'Review', schema: S.verify })))).filter(Boolean)
  if (!answers.length) return null
  const confirmed = answers.filter(v => v.verdict === 'confirmed')
  if (confirmed.length * 2 <= answers.length) {
    return { verdict: 'refuted', reason: (answers.find(v => v.verdict === 'refuted') || {}).reason || '', votes: `${confirmed.length}/${answers.length}` }
  }
  const grades = confirmed.map(v => v.severity).sort((a, b) => SEVERITY[a] - SEVERITY[b])
  return { verdict: 'confirmed', severity: grades[Math.floor(grades.length / 2)], reason: confirmed[0].reason, votes: `${confirmed.length}/${answers.length}` }
}

function scopeNote(round, prevTree, curTree, prior, responses) {
  if (!round) return ''
  const answers = new Map((responses || []).map(r => [r.n, r]))
  const lines = prior.map((f, i) => {
    const a = answers.get(i + 1)
    return `${i + 1}. [${f.severity}, ${f.class || 'unclassified'}] ${findingText(f).split('\n')[0]}` +
      (a ? ` -- fixer: ${a.action}: ${a.note}` : ' -- fixer: no answer')
  })
  return ['', `## Scope: re-review round ${round}`, '',
    `The previous round's findings went to a fixer. Review only what the fix changed: \`git diff ${prevTree} ${curTree}\`. ` +
    'Report (a) earlier findings that are still unresolved, keeping their class, and (b) problems the fix itself introduced. ' +
    'Report nothing else: no new findings in code the fix did not touch, nothing already settled. When the fixer disputed ' +
    'a finding with a reason, report it again only if the reason is wrong.', '',
    'Earlier findings and the fixer\'s answers:', '', ...lines].join('\n')
}

async function review(t, plan, round, ctx) {
  const suffix = round ? String(round) : ''
  const ran = await checks(t, plan, round)
  if (!ran) return null
  const sensitive = ran.sensitive || []
  const lenses = LENSES.concat(sensitive.length && !LENSES.includes('security') ? ['security'] : [])
  const votes = sensitive.length ? VOTES : 1
  const scope = scopeNote(round, ctx.prevTree, ran.tree, ctx.prior || [], ctx.responses || [])
  const checksNote = (P.checks || []).length ? '' : '\nNo checks are configured: run the ones the repository instructions name, and report a failing one as a blocker.'

  const results = await parallel(lenses.map(lens => () => agent(
    render('review', values(t, plan, {
      lens, scopeNote: scope, reviewFileNote: reviewFileNote(t),
      lensBrief: render(`lenses/${lens}`, { checksNote, sensitive: sensitive.join(', ') }),
    })),
    { label: `${lens}${suffix}:${ref(t)}`, phase: 'Review', schema: S.findings })))
  if (results.every(r => !r)) return null

  let found = dedupe(lenses.flatMap((lens, i) => (results[i] ? results[i].findings.map(f => Object.assign({}, f, { lens })) : [])))
  const preExisting = found.filter(f => f.introduced === false)
  found = found.filter(f => f.introduced !== false)
  const toCheck = found.filter(f => f.severity !== 'minor')
  const refuted = []
  if (VERIFY && toCheck.length) {
    const verdicts = await parallel(toCheck.map((f, i) => () => verdict(t, plan, `verify${suffix}:${ref(t)}·${i + 1}`, f, votes)))
    toCheck.forEach((f, i) => {
      const v = verdicts[i]
      if (!v) f.verified = false
      else if (v.verdict === 'refuted') { f.refuted = v.reason; f.votes = v.votes; refuted.push(f) }
      else Object.assign(f, { severity: v.severity, verified: true, reason: v.reason, votes: v.votes })
    })
    found = found.filter(f => !('refuted' in f))
    if (refuted.length) log(`${ref(t)}: ${refuted.length} finding(s) refuted by the skeptics`)
  }
  return { round, findings: found, preExisting, checks: ran.checks, refuted, sensitive, tree: ran.tree }
}

function fixList(r) {
  const todo = r.findings.filter(f => f.severity !== 'minor')
  const lines = todo.map((f, i) => `${i + 1}. [${f.severity}, ${f.class || 'unclassified'}; ${f.lens} review] ` + findingText(f).replace(/\n/g, '\n   '))
  for (const c of r.checks.filter(c => !c.ok)) {
    lines.push(`- [failing check] \`${c.cmd}\`` + (c.tail ? '\n  Output (tail):\n  ' + c.tail.replace(/\n/g, '\n  ') : ''))
  }
  return { todo, text: lines.join('\n') }
}

async function build(t, plan) {
  const planMd = plan.planMarkdown || plan.summary || ''
  const built = await agent(render('implement', values(t, plan, { plan: planMd })), { label: `build:${ref(t)}`, phase: 'Build', schema: S.build })
  if (!built) return { status: 'needs-human', why: 'implementer failed', rounds: 0, preExisting: [], confirmed: [] }
  if (built.stuck) return { status: 'needs-human', why: built.stuck, rounds: 0, preExisting: [], confirmed: [] }

  let r = await review(t, plan, 0, {})
  const preExisting = r ? r.preExisting.slice() : []
  const confirmed = r ? r.findings.slice() : []
  let rounds = 0
  while (r && blocking(r) && rounds < MAX_FIX) {
    rounds++
    const { todo, text } = fixList(r)
    const fixed = await agent(render('fix', values(t, plan, { round: rounds, findings: text })), { label: `fix${rounds}:${ref(t)}`, phase: 'Build', schema: S.build })
    const lastKeys = new Set(todo.map(classKey))
    const prevTree = r.tree
    r = await review(t, plan, rounds, { prevTree, prior: todo, responses: fixed && fixed.responses })
    if (!r) break
    preExisting.push(...r.preExisting)
    confirmed.push(...r.findings)
    const repeated = r.findings.filter(f => f.severity !== 'minor' && lastKeys.has(classKey(f)))
    if (repeated.length && blocking(r)) {
      const f = repeated[0]
      return { status: 'needs-human', rounds, review: r, preExisting, confirmed,
        why: `the same issue survived a fix (${f.class || f.issue.slice(0, 60)}${f.file ? ' in ' + f.file : ''}); rethink the approach` }
    }
  }
  if (!r) return { status: 'needs-human', why: 'reviewer failed', rounds, preExisting, confirmed }
  if (blocking(r)) return { status: 'needs-human', why: `still failing review after ${rounds} fix round(s)`, rounds, review: r, preExisting, confirmed }
  return { status: 'ready', why: '', rounds, review: r, preExisting, confirmed }
}

async function handoff(t, plan, res) {
  const r = res.review
  const pre = dedupe(res.preExisting || [])
  const sensitive = (r && r.sensitive) || []
  const last = r && r.findings.length
    ? 'Last review findings:\n' + r.findings.map(f => `- [${f.severity}] ${f.file || ''} ${f.issue}`).join('\n')
    : 'The last review raised nothing.'
  const file = `${A.runDir}/${t.id}.handoff.json`
  const extra = { status: res.status, why: res.why || '', rounds: res.rounds, sensitive, preExisting: pre,
    findings: (res.confirmed || []).map(f => ({ severity: f.severity, class: f.class, file: f.file, issue: f.issue, lens: f.lens, introduced: f.introduced })) }
  const saveNote = [
    '## Save the handoff',
    '',
    `Write \`${file}\`: one JSON object holding every field of your answer (summary, decisions, followUps, openFindings, commitMessage, prTitle, prChanges, testing, risk, revert) plus these fields exactly as given:`,
    '',
    '```json',
    JSON.stringify(extra, null, 2),
    '```',
    '',
    `Then run \`${A.al} wf handoff ${A.run} --id ${t.id}\`. It writes the handoff note and prints its path.`,
  ].join('\n')
  const h = await agent(render('handoff', values(t, null, {
    status: res.status + (res.why ? ` (${res.why})` : ''),
    plan: plan.planMarkdown || plan.summary || '',
    stat: '(read it with `git diff --cached --stat ' + t.base + '`)',
    diff: '(read it with `git diff --cached ' + t.base + '`)',
    lastReview: last,
    whyNote: res.why ? `Why it needs a human: ${res.why}.` : '',
    summaryNote: res.why ? '; when it needs a human, say why first' : '',
    preExistingNote: pre.length ? 'Problems the reviewers found in code this change did not touch (listed separately as pre-existing; do not repeat them in openFindings):\n' + pre.map(f => '- ' + findingText(f).split('\n')[0]).join('\n') : '',
    riskNote: sensitive.length ? `This change touches sensitive paths (${sensitive.join(', ')}); say in \`risk\` what a human reviewer must check there.` : '',
    saveNote,
  })), { label: `handoff:${ref(t)}`, phase: 'Handoff', schema: S.handoff, effort: 'low' })
  return h || {}
}

const built = await pipeline(
  batch,
  ({ t, plan }) => build(t, plan).catch(e => ({ status: 'needs-human', why: `build crashed: ${e && e.message}`, rounds: 0, preExisting: [], confirmed: [] })),
  (res, { t, plan }) => handoff(t, plan, res).then(h => Object.assign({ t }, res, { handoff: h })),
)

const done = built.filter(Boolean)
return {
  ready: done.filter(r => r.status === 'ready').map(r => ({
    id: r.t.id, ref: ref(r.t), name: r.t.name, branch: r.t.branch, rounds: r.rounds,
    risk: r.review && r.review.sensitive && r.review.sensitive.length ? 'high' : 'normal',
    preExisting: (r.preExisting || []).length,
    commitMessage: r.handoff.commitMessage, prTitle: r.handoff.prTitle, prChanges: r.handoff.prChanges,
  })),
  needsHuman: needsHuman.concat(done.filter(r => r.status !== 'ready').map(r => ({ id: r.t.id, ref: ref(r.t), name: r.t.name, question: r.why }))),
  deferred,
}
