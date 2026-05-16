import type { AgentEvent, JobStatus } from '../types'

const PHASES = [
  { key: 'resolve', label: 'Resolve', desc: 'Identify company' },
  { key: 'research', label: 'Research', desc: 'Gather data' },
  { key: 'synthesize', label: 'Synthesize', desc: 'Build portfolio' },
  { key: 'excel', label: 'Export', desc: 'Generate Excel' },
]

type PhaseState = 'pending' | 'active' | 'done' | 'error'

function getPhaseStates(events: AgentEvent[], status: JobStatus): PhaseState[] {
  const started = events
    .filter(e => e.type === 'phase_start')
    .map(e => (e.data as { phase: string }).phase)

  const lastIdx = PHASES.reduce((best, p, i) =>
    started.includes(p.key) ? i : best, -1)

  return PHASES.map((_, i) => {
    if (i > lastIdx) return 'pending'
    if (i < lastIdx) return 'done'
    if (status === 'error') return 'error'
    if (status === 'done') return 'done'
    return 'active'
  })
}

interface Props {
  events: AgentEvent[]
  status: JobStatus
}

export function PhaseStepper({ events, status }: Props) {
  const states = getPhaseStates(events, status)

  return (
    <div className="phase-stepper">
      {PHASES.map((phase, i) => {
        const s = states[i]
        return (
          <div key={phase.key} className={`phase-step phase-step-${s}`}>
            <div className="phase-bubble">
              {s === 'done' ? '✓' : s === 'error' ? '✕' : String(i + 1)}
              {s === 'active' && <span className="phase-bubble-ring" />}
            </div>
            <div className="phase-step-label">{phase.label}</div>
            <div className="phase-step-desc">{phase.desc}</div>
          </div>
        )
      })}
    </div>
  )
}
