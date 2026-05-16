import { useEffect, useRef } from 'react'
import type { AgentEvent, JobStatus } from '../types'
import { ToolCallCard } from './ToolCallCard'

interface Props {
  events: AgentEvent[]
  streamingText: string
  status: JobStatus
}

const PHASE_LABELS: Record<string, string> = {
  resolve: 'Resolving company identity',
  research: 'Researching with live tools',
  synthesize: 'Synthesizing portfolio',
  excel: 'Generating Excel workbook',
}

export function EventTimeline({ events, streamingText, status }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events.length, streamingText])

  // The last phase_start seen is the currently active phase
  const activePhase = [...events]
    .reverse()
    .find(e => e.type === 'phase_start')
  const activePhaseKey = activePhase
    ? (activePhase.data as { phase: string }).phase
    : null

  // Positional pairing: n-th tool_result matches n-th tool_call
  const callEvents: AgentEvent[] = []
  const resultEvents: AgentEvent[] = []
  for (const ev of events) {
    if (ev.type === 'tool_call') callEvents.push(ev)
    if (ev.type === 'tool_result') resultEvents.push(ev)
  }

  let callIndex = 0

  return (
    <div className="timeline">
      {events.map((ev, i) => {
        switch (ev.type) {
          case 'phase_start': {
            const d = ev.data as { phase: string; description: string }
            const isActive = d.phase === activePhaseKey && status === 'running'
            return (
              <div key={i} className="timeline-phase">
                <div className={`phase-dot${isActive ? ' phase-dot-active' : ''}`} />
                <div className="phase-label">
                  <span className="phase-name">{PHASE_LABELS[d.phase] ?? d.phase}</span>
                </div>
              </div>
            )
          }

          case 'tool_call': {
            const resultEv = resultEvents[callIndex++]
            return (
              <div key={i} className="timeline-tool">
                <ToolCallCard callEvent={ev} resultEvent={resultEv} />
              </div>
            )
          }

          case 'tool_result':
            // Already rendered inline with the call card
            return null

          case 'warning':
            return (
              <div key={i} className="timeline-warning">
                ⚠ {(ev.data as { message: string }).message}
              </div>
            )

          case 'error':
            return (
              <div key={i} className="timeline-error">
                ✕ {(ev.data as { message: string }).message}
              </div>
            )

          case 'heartbeat':
          case 'token':
          case 'portfolio_section':
          case 'done':
          case 'plan':
            return null

          default:
            return null
        }
      })}

      {streamingText && (
        <div className="timeline-synthesis">
          <div className="synthesis-label">Synthesizing…</div>
          <div className="synthesis-text">{streamingText}<span className="cursor" /></div>
        </div>
      )}

      <div ref={bottomRef} />
    </div>
  )
}
