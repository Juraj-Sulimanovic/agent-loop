export type TicketRow = {
  id: string
  ref: string
  name: string
  phase: 'Plan' | 'Build' | 'Review' | 'Handoff' | 'Done'
  stage: string
  status: string
  rounds: number
  risk: 'high' | 'normal'
  why: string
  running: string[]
}

export type PhaseCount = { name: 'Plan' | 'Build' | 'Review' | 'Handoff'; done: number; total: number }

export type Snapshot = {
  runId: string
  state: 'running' | 'finished' | 'stalled'
  startedMs: number
  updatedMs: number
  tools: string
  phases: PhaseCount[]
  tickets: TicketRow[]
  log: string[]
}

declare module 'claude-code' {
  interface PluginState {
    'agent-loop-panel': { snapshot: Shaped<Snapshot | null> }
  }
}
