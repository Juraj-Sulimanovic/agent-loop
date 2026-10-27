import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { PhaseCount, Snapshot, TicketRow } from '../types'

// A live view of the newest agent-loop run in this project. The runner writes
// <state>/runs/<id>/run.json, one <ticket>.<label>.prompt.md per agent call it
// starts and <ticket>.<label>.json when that call finishes, and log.txt.
// This module only reads those files.

const PANE = 'agent-loop'
const POLL_MS = 2000
const STALLED_MS = 15 * 60 * 1000 // no write for this long, and not finished: probably stopped
const PHASES = ['Plan', 'Build', 'Review', 'Handoff'] as const

// Change the tag when Snapshot changes shape: it declines what an earlier version stored.
const snapshot = atom({ plugin: 'agent-loop-panel', key: 'snapshot' } as const, null, { shape: 'snapshot-1' })

let stateDir: string | null | undefined // undefined: not looked up yet; null: no agent-loop project here
let openedFor = ''

type Run = {
  id?: string
  started?: string
  created?: string
  finished?: string | null
  agents?: Record<string, string | null>
  prepared?: { id: string }[]
  tickets?: Record<string, { ref?: string; name?: string; stage?: string; status?: string; rounds?: number; risk?: string; why?: string }>
}

const parent = (dir: string) => dir.replace(/\/[^/]+\/?$/, '') || '/'

async function findStateDir($: EngineInterface): Promise<string | null> {
  const root: string = await $.session.root()
  for (let dir = root; ; dir = parent(dir)) {
    if (await $.fs.exists(`${dir}/.agent-loop/config.json`)) return `${dir}/.agent-loop`
    if (dir === '/' || dir === parent(dir)) break
  }
  const git = await $.process.run(['git', '-C', root, 'rev-parse', '--path-format=absolute', '--git-common-dir'])
  const common = git.exitCode === 0 ? git.stdout.trim() : ''
  if (common && (await $.fs.exists(`${common}/agent-loop/config.json`))) return `${common}/agent-loop`
  return null
}

function phaseOf(stage: string): TicketRow['phase'] {
  if (stage === 'done' || stage === 'planned') return 'Done'
  if (stage === 'queued' || stage === 'plan') return 'Plan'
  if (stage === 'implement' || stage.startsWith('fix')) return 'Build'
  if (stage === 'handoff') return 'Handoff'
  return 'Review' // review*, verify*, checks
}

async function load($: EngineInterface): Promise<Snapshot | null> {
  if (stateDir === undefined) stateDir = await findStateDir($)
  if (!stateDir) return null
  const runsDir = `${stateDir}/runs`
  if (!(await $.fs.exists(runsDir))) return null

  // The newest run: the run.json written last.
  let newest: { name: string; mtimeMs: number } | undefined
  for (const entry of await $.fs.list(runsDir)) {
    if (entry.kind !== 'dir') continue
    const stat = await $.fs.stat(`${runsDir}/${entry.name}/run.json`).catch(() => undefined)
    if (stat && (!newest || stat.mtimeMs > newest.mtimeMs)) newest = { name: entry.name, mtimeMs: stat.mtimeMs }
  }
  if (!newest) return null
  const dir = `${runsDir}/${newest.name}`
  let run: Run
  try {
    run = JSON.parse(await $.fs.read(`${dir}/run.json`))
  } catch {
    return null // caught mid-write on an old runner; the next poll reads it
  }

  // Agent calls started but not finished: a prompt without its result.
  const files = new Set((await $.fs.list(dir)).map(e => e.name))
  const running = new Map<string, string[]>()
  for (const name of files) {
    const [, ticket, label] = /^([^.]+)\.(.+)\.prompt\.md$/.exec(name) ?? []
    if (ticket && label && !files.has(`${ticket}.${label}.json`)) running.set(ticket, [...(running.get(ticket) ?? []), label])
  }

  const order = (run.prepared ?? []).map(t => t.id)
  const ids = [...order, ...Object.keys(run.tickets ?? {}).filter(id => !order.includes(id))]
  const tickets: TicketRow[] = ids.map(id => {
    const t = run.tickets?.[id] ?? {}
    const stage = t.stage ?? 'queued'
    return {
      id,
      ref: t.ref ?? `#${id}`,
      name: t.name ?? '',
      phase: phaseOf(stage),
      stage,
      status: t.status ?? '',
      rounds: t.rounds ?? 0,
      risk: t.risk === 'high' ? 'high' : 'normal',
      why: t.why ?? '',
      running: (running.get(id) ?? []).sort(),
    }
  })

  // A ticket is in the batch once its implementer started; the later phases count only those.
  const built = tickets.filter(t => files.has(`${t.id}.implement.prompt.md`))
  const past = (t: TicketRow, phase: (typeof PHASES)[number]) =>
    t.phase === 'Done' || PHASES.indexOf(t.phase as (typeof PHASES)[number]) > PHASES.indexOf(phase)
  const phases: PhaseCount[] = PHASES.map(name => {
    const pool = name === 'Plan' ? tickets : built
    return { name, done: pool.filter(t => past(t, name)).length, total: pool.length }
  })

  const log = (await $.fs.read(`${dir}/log.txt`).catch(() => ''))
    .split('\n')
    .filter(Boolean)
    .slice(-4)
    .map(line => line.replace(/^\d{4}-\d\d-\d\dT/, ''))
  const tools = Object.entries(run.agents ?? {})
    .filter(([, tool]) => tool)
    .map(([stage, tool]) => `${stage}=${tool}`)
    .join(' ')
  const now = await $.clock.now()
  const started = Date.parse(run.started ?? run.created ?? '') || newest.mtimeMs

  return {
    runId: run.id ?? newest.name,
    state: run.finished ? 'finished' : now - newest.mtimeMs > STALLED_MS ? 'stalled' : 'running',
    startedMs: started,
    updatedMs: run.finished ? Date.parse(run.finished) || newest.mtimeMs : now,
    tools,
    phases,
    tickets,
    log,
  }
}

const minutes = (ms: number) => {
  const s = Math.max(0, Math.round(ms / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`
}

function statusLine(s: Snapshot | null): string | undefined {
  if (!s || s.state !== 'running') return undefined
  const done = s.tickets.filter(t => t.phase === 'Done').length
  const active = s.tickets.find(t => t.phase !== 'Done')
  return `agent-loop ${done}/${s.tickets.length}${active ? ` · ${active.ref} ${active.stage}` : ''}`
}

async function tick($: EngineInterface) {
  const next = await load($)
  const before = await read($, snapshot)
  if (JSON.stringify(before) !== JSON.stringify(next)) await update($, snapshot, () => next)
  $.ui.status(statusLine(next))
  if (next && next.state === 'running' && openedFor !== next.runId) {
    openedFor = next.runId // open once per run; closing the pane keeps it closed
    void $.ui.open({ id: PANE, title: 'agent-loop' })
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'agent-loop-panel', description: 'Show the live progress of the agent-loop run' })
    stateDir = undefined
    await tick($)
    $.clock.every(POLL_MS, () => tick($))
    return next(e)
  })

  on('command.run', { command: 'agent-loop-panel' }, async $ => {
    stateDir = undefined // the session may have moved into a project since it started
    await tick($)
    const opened = await $.ui.open({ id: PANE, title: 'agent-loop', closeOnEscape: true })
    if (!(await read($, snapshot))) return { text: 'No agent-loop run in this project yet. The pane updates when one starts.' }
    return { text: opened.isPlaced ? 'agent-loop pane opened.' : 'agent-loop pane is waiting for a wider terminal.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const s = await read($, snapshot)
    if (!s) return <Text dimColor>No agent-loop run in this project yet.</Text>
    const now = await $.clock.now()
    const elapsed = minutes((s.state === 'running' ? now : s.updatedMs) - s.startedMs)
    const label = { running: 'running', finished: 'finished', stalled: 'no progress for 15 min (stopped?)' }[s.state]
    const color = { running: 'suggestion', finished: 'success', stalled: 'warning' }[s.state]

    return (
      <Box flexDirection="column">
        <Text>
          <Text bold>run {s.runId}</Text>
          <Text color={color}> · {label}</Text>
          <Text dimColor> · {elapsed}{s.tools ? ` · ${s.tools}` : ''}</Text>
        </Text>
        <Box flexDirection="row" marginTop={1}>
          <Box flexDirection="column" width={18} borderStyle="round" borderDimColor paddingX={1}>
            <Text bold>Phases</Text>
            {s.phases.map(p => {
              const isDone = p.total > 0 && p.done === p.total
              const isNow = !isDone && s.tickets.some(t => t.phase === p.name)
              return (
                <Text color={isDone ? 'success' : isNow ? 'suggestion' : undefined} dimColor={!isDone && !isNow}>
                  {isDone ? '✔' : isNow ? '›' : ' '} {p.name.padEnd(8)}
                  {p.done}/{p.total}
                </Text>
              )
            })}
          </Box>
          <Box flexDirection="column" flexGrow={1} borderStyle="round" borderDimColor paddingX={1}>
            <Text bold>Tickets · {s.tickets.length}</Text>
            {s.tickets.map(t => {
              const icon =
                t.status === 'ready' ? '✔' : t.status === 'needs-human' ? '✖' : t.status === 'deferred' ? '↷' : t.status === 'planned' ? '✔' : '●'
              const tone =
                t.status === 'ready' || t.status === 'planned' ? 'success' : t.status === 'needs-human' ? 'error' : t.status === 'deferred' ? 'warning' : 'suggestion'
              const detail =
                t.phase === 'Done'
                  ? t.why || t.status
                  : t.running.length
                    ? t.running.join(', ')
                    : t.stage
              return (
                <Text wrap="truncate-end">
                  <Text color={tone}>{icon} </Text>
                  <Text bold>{t.ref}</Text>
                  <Text> {t.name} </Text>
                  <Text dimColor>
                    · {detail}
                    {t.rounds ? ` · ${t.rounds} fix round${t.rounds > 1 ? 's' : ''}` : ''}
                  </Text>
                  {t.risk === 'high' && <Text color="warning"> · high risk</Text>}
                </Text>
              )
            })}
          </Box>
        </Box>
        {s.log.map(line => (
          <Text dimColor wrap="truncate-end">
            {line}
          </Text>
        ))}
      </Box>
    )
  })
}
