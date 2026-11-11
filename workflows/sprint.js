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

const COMMITS = !!P.commitAtHandoff
const batch = [], deferred = [], needsHuman = []
const taken = new Map()
const later = [] // { t, plan, on }: tickets that wait for others of this run (commit mode only)
A.tickets.forEach((t, i) => {
  const plan = plans[i]
  if (!plan) return deferred.push({ id: t.id, ref: ref(t), reason: 'planner failed; run it again' })
  if (!plan.feasible) return needsHuman.push({ id: t.id, ref: ref(t), name: t.name, question: plan.question || plan.summary })
  const blockers = (t.waitFor || []).filter(id => A.tickets.some(x => x.id === id))
  if (COMMITS && blockers.length) return later.push({ t, plan, on: new Set(blockers) })
  const mine = claims(plan)
  const clashes = [...mine].sort().filter(c => taken.has(c))
  if (clashes.length) {
    if (COMMITS) return later.push({ t, plan, on: new Set(clashes.map(c => taken.get(c).id)) })
    return deferred.push({ id: t.id, ref: ref(t), reason: `overlaps ${ref(taken.get(clashes[0]))} on ${clashes[0].replace(/^\w+:/, '')}` })
  }
  if (batch.length >= MAX_PARALLEL) {
    if (COMMITS) return later.push({ t, plan, on: new Set() })
    return deferred.push({ id: t.id, ref: ref(t), reason: `over maxParallel (${MAX_PARALLEL})` })
  }
  mine.forEach(c => taken.set(c, t))
  batch.push({ t, plan })
})
log(`Batch: ${batch.map(b => ref(b.t)).join(', ') || 'none'}`)
for (const w of later) {
  log(`${ref(w.t)}: waits for ${[...w.on].map(id => '#' + id).join(', ') || 'a free slot'}`)
}
if (deferred.length) log(`Deferred: ${deferred.map(d => `${d.ref} (${d.reason})`).join('; ')}`)
if (needsHuman.length) log(`Needs you: ${needsHuman.map(n => n.ref).join(', ')}`)

if (A.planOnly) {
  return {
    stoppedAfter: 'plan', plans: batch.map(b => Object.assign({ id: b.t.id, ref: ref(b.t) }, b.plan)),
    waiting: later.map(w => ({ id: w.t.id, ref: ref(w.t), waitsFor: [...w.on] })), deferred, needsHuman,
  }
}

// ---- Build and review, per ticket, no barrier between tickets -------------------

function checks(t, plan, round) {
  const files = (plan.files || []).join(',')
  const cmd = `${A.al} wf checks ${t.worktree} --run ${A.run} --id ${t.id} --base ${t.base}${round ? ' --round ' + round : ''}${files ? ` --files '${files}'` : ''}`
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
  const failing = ran.checks.filter(c => !c.ok).map(c => c.cmd)
  log(`${ref(t)}: checks ${failing.length ? 'failing: ' + failing.join(', ') : `${ran.checks.length} passing`}` +
    (ran.sensitive && ran.sensitive.length ? ` · high risk (${ran.sensitive.join(', ')})` : ''))
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
  const blockingCount = found.filter(f => f.severity !== 'minor').length
  log(`${ref(t)}: review${round ? ' round ' + round : ''} · ${blockingCount} blocking, ${found.length - blockingCount} minor` +
    (refuted.length ? `, ${refuted.length} refuted` : '') + (preExisting.length ? `, ${preExisting.length} pre-existing` : ''))
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
  log(`${ref(t)}: implemented, now reviewing`)

  let r = await review(t, plan, 0, {})
  const preExisting = r ? r.preExisting.slice() : []
  const confirmed = r ? r.findings.slice() : []
  let rounds = 0
  while (r && blocking(r) && rounds < MAX_FIX) {
    rounds++
    const { todo, text } = fixList(r)
    log(`${ref(t)}: fix round ${rounds} for ${todo.length} finding(s)`)
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
    `Then run \`cd ${P.root} && ${A.al} wf handoff ${A.run} --id ${t.id}\`. It writes the handoff note (and, when the project commits at handoff, the commit) and prints the result.`,
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

function finish(t, plan) {
  return build(t, plan)
    .catch(e => ({ status: 'needs-human', why: `build crashed: ${e && e.message}`, rounds: 0, preExisting: [], confirmed: [] }))
    .then(res => handoff(t, plan, res).then(h => {
      log(`${ref(t)}: ${res.status === 'ready' ? 'ready to ship' : 'needs you: ' + res.why}`)
      return Object.assign({ t }, res, { handoff: h })
    }))
}

let done
if (!COMMITS) {
  // Without a commit at handoff nothing can build on a finished ticket: overlaps wait for the next run.
  done = (await pipeline(batch, ({ t, plan }) => finish(t, plan))).filter(Boolean)
} else {
  // Tickets that overlap a running one, or that its tracker says wait for one, start when it is done:
  // `wf stack` moves the waiting ticket onto the finished one's commit, a planner plans it again on
  // the new code, and it starts unless it now overlaps another running ticket.
  const results = new Map()      // id -> the finished ticket's result
  const order = []               // ids in the order they finished
  const holding = new Map()      // claim -> the running ticket that holds it
  const running = new Map()      // id -> its chain
  const stacks = new Map()       // id -> how many times it was moved onto another ticket
  for (const n of needsHuman) results.set(n.id, { status: 'needs-human' })
  for (const d of deferred) results.set(d.id, { status: 'deferred' })

  const STACK = {
    type: 'object', additionalProperties: false, required: ['ok', 'reason', 'base', 'tree', 'resolve'],
    properties: {
      ok: { type: 'boolean' }, reason: { type: 'string' }, base: { type: 'string' }, tree: { type: 'string' },
      // a finished ticket that `wf stack` is putting under another needs an agent first
      resolve: { type: ['object', 'null'], additionalProperties: false, required: ['id', 'ref', 'why', 'prompt'],
        properties: { id: { type: 'string' }, ref: { type: 'string' }, why: { type: 'string' }, prompt: { type: 'string' } } },
    },
  }

  function start(t, plan) {
    const mine = claims(plan)
    mine.forEach(c => holding.set(c, t))
    running.set(t.id, finish(t, plan).then(r => {
      results.set(t.id, r)
      order.push(t.id)
      for (const c of mine) if (holding.get(c) === t) holding.delete(c)
      running.delete(t.id)
      return r
    }))
  }

  async function restart(w) {
    const onto = [...w.on].sort((a, b) => order.indexOf(b) - order.indexOf(a))[0]
    const needs = [...w.on].filter(id => id !== onto)
    const n = (stacks.get(w.t.id) || 0) + 1
    stacks.set(w.t.id, n)
    const cmd = `cd ${P.root} && ${A.al} wf stack ${A.run} --id ${w.t.id} --onto ${onto}${needs.length ? ' --needs ' + needs.join(',') : ''}`
    // Blockers on separate branches go in a line first; a conflict or a failing check there gets a
    // resolve agent, then `wf stack` carries on from where it stopped.
    let moved
    for (let k = 0; k < 6; k++) {
      moved = await agent(
        `Run exactly this command and return its JSON output as your result, unchanged:\n\n\`\`\`bash\n${cmd}\n\`\`\`\n\nDo nothing else.`,
        { label: `stack${n > 1 ? n : ''}${k ? '.' + (k + 1) : ''}:${ref(w.t)}`, phase: 'Plan', schema: STACK, effort: 'low' })
      if (!moved || !moved.resolve) break
      log(`${moved.resolve.ref}: ${moved.resolve.why}; a resolve agent combines them for ${ref(w.t)}`)
      await agent(moved.resolve.prompt, { label: `resolve${k + 1}:${moved.resolve.ref}`, phase: 'Build', schema: S.build })
    }
    if (!moved || !moved.ok) {
      needsHuman.push({ id: w.t.id, ref: ref(w.t), name: w.t.name,
        question: `cannot build on ${'#' + onto}: ${moved ? moved.reason : 'the stack step failed'}` })
      results.set(w.t.id, { status: 'needs-human' })
      return
    }
    const t = Object.assign({}, w.t, { base: moved.base })
    log(`${ref(t)}: moved onto #${onto} (${moved.base}); planning again on the new code`)
    const plan = await agent(render('plan', values(t)), { label: `plan${n + 1}:${ref(t)}`, phase: 'Plan', schema: S.plan })
    if (!plan || !plan.feasible) {
      needsHuman.push({ id: t.id, ref: ref(t), name: t.name, question: plan ? plan.question || plan.summary : 'the planner failed' })
      results.set(t.id, { status: 'needs-human' })
      return
    }
    return { t, plan }
  }

  // Start what is free; move waiting tickets whose blockers are done onto them.
  async function wake() {
    for (const w of later.slice()) {
      if (running.size >= MAX_PARALLEL) break
      if (![...w.on].every(id => results.has(id))) continue
      later.splice(later.indexOf(w), 1)
      const failed = [...w.on].filter(id => results.get(id).status !== 'ready')
      if (failed.length) {
        deferred.push({ id: w.t.id, ref: ref(w.t), reason: `waits on ${failed.map(id => '#' + id).join(', ')}, which did not finish ready` })
        results.set(w.t.id, { status: 'deferred' })
        continue
      }
      const next = w.on.size ? await restart(w) : { t: w.t, plan: w.plan }
      if (!next) continue
      const clashes = [...claims(next.plan)].filter(c => holding.has(c))
      if (clashes.length) {
        const on = new Set(clashes.map(c => holding.get(c).id))
        log(`${ref(next.t)}: overlaps ${[...on].map(id => '#' + id).join(', ')} on ${clashes[0].replace(/^\w+:/, '')}; waits for it`)
        later.push({ t: next.t, plan: next.plan, on })
        continue
      }
      start(next.t, next.plan)
    }
  }

  for (const { t, plan } of batch) start(t, plan)
  await wake()
  while (running.size) {
    await Promise.race(running.values())
    await wake()
  }
  for (const w of later) {
    deferred.push({ id: w.t.id, ref: ref(w.t), reason: `waits on ${[...w.on].map(id => '#' + id).join(', ')}, which did not finish` })
  }
  done = [...results.values()].filter(r => r && r.t)
}

return {
  ready: done.filter(r => r.status === 'ready').map(r => ({
    id: r.t.id, ref: ref(r.t), name: r.t.name, branch: r.t.branch, base: r.t.base, rounds: r.rounds,
    risk: r.review && r.review.sensitive && r.review.sensitive.length ? 'high' : 'normal',
    preExisting: (r.preExisting || []).length,
    commitMessage: r.handoff.commitMessage, prTitle: r.handoff.prTitle, prChanges: r.handoff.prChanges,
  })),
  needsHuman: needsHuman.concat(done.filter(r => r.status !== 'ready').map(r => ({ id: r.t.id, ref: ref(r.t), name: r.t.name, question: r.why }))),
  deferred,
}
