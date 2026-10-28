// Runs workflows/sprint.js with a scripted fake agent, the way Claude Code's Workflow tool would,
// and prints what happened as JSON for tests/test_workflow.py to check.
//
//   node tests/workflow_harness.mjs <args.json> <scenario>

import { readFileSync } from 'node:fs'

const [argsPath, scenario] = process.argv.slice(2)
const args = JSON.parse(readFileSync(argsPath, 'utf8'))
const src = readFileSync(new URL('../workflows/sprint.js', import.meta.url), 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor

const calls = [], prompts = {}, logs = []
const reviews = {}

function fake(prompt, opts) {
  const label = opts.label
  calls.push(label)
  prompts[label] = prompt
  const id = label.split('#')[1].split('·')[0]
  const kind = label.split(':')[0]
  if (kind === 'plan') {
    if (id === '6') return { feasible: false, question: 'which tenant?', summary: '', files: [], steps: [], risks: [], planMarkdown: '', knowledge: [] }
    const files = { 1: ['a.py'], 2: ['a.py'], 3: ['text.py'], 5: ['c.py'] }[id] || ['x.py']
    return { feasible: true, question: null, summary: 's', files, steps: [], risks: [], planMarkdown: `# Plan ${id}`, knowledge: [] }
  }
  if (kind === 'build' || kind.startsWith('fix')) {
    return { summary: 'ok', checks: [], stuck: null, responses: [{ n: 1, action: 'fixed', note: 'guarded' }] }
  }
  if (kind.startsWith('checks')) {
    const sensitive = scenario === 'risky' && id === '3' ? ['text.py'] : []
    return { checks: [{ cmd: 'make check', ok: true, tail: '' }], sensitive, tree: `tree-${id}-${kind}` }
  }
  if (kind.startsWith('verify')) {
    const vote = label.split('·v')[1]
    return { verdict: scenario === 'risky' && vote === '3' ? 'refuted' : 'confirmed', severity: 'major', reason: 'looked' }
  }
  if (kind === 'handoff') {
    return { summary: `Did ${id}.`, decisions: [], followUps: [], openFindings: [], commitMessage: `feat: ${id}`, prTitle: `T${id}`, prChanges: ['x'], testing: 't', risk: 'r', revert: 'v' }
  }
  // a reviewer: <lens><round>
  const round = kind.replace(/^\D+/, '')
  const lens = kind.replace(/\d+$/, '')
  reviews[id] = (reviews[id] || 0) + 1
  const findings = []
  if (lens === 'correctness' && id === '1' && round === '') findings.push({ severity: 'major', file: 'a.py', line: 3, issue: 'x', evidence: 'e', fix: null, introduced: true, class: 'missing check' })
  if (lens === 'correctness' && id === '5') findings.push({ severity: 'major', file: 'c.py', line: 1, issue: 'y', evidence: 'e', fix: null, introduced: true, class: 'same bug' })
  if (lens === 'standards' && id === '1' && round === '') findings.push({ severity: 'blocker', file: 'old.py', line: 9, issue: 'old crash', evidence: 'e', fix: null, introduced: false, class: 'legacy' })
  if (lens === 'security' && id === '3') findings.push({ severity: 'major', file: 'text.py', line: 2, issue: 'token logged', evidence: 'e', fix: null, introduced: true, class: 'secret in logs' })
  return { findings }
}

const agent = async (prompt, opts) => fake(prompt, opts)
const parallel = async thunks => Promise.all(thunks.map(t => t().catch(() => null)))
const pipeline = async (items, ...stages) => Promise.all(items.map(async (it, i) => {
  let r = it
  for (const s of stages) { try { r = await s(r, it, i) } catch (e) { console.error(e); return null } }
  return r
}))
const fn = new AsyncFunction('agent', 'parallel', 'pipeline', 'phase', 'log', 'args', src)
const result = await fn(agent, parallel, pipeline, () => {}, m => logs.push(m), args)
console.log(JSON.stringify({ result, calls, prompts, logs }))
