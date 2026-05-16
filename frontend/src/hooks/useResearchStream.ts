import { useEffect, useRef, useCallback, useState } from 'react'
import type { AgentEvent, JobStatus } from '../types'

interface StreamState {
  events: AgentEvent[]
  status: JobStatus
  streamingText: string
  jobId: string | null
}

interface UseResearchStreamReturn extends StreamState {
  startResearch: (companyName: string, disableWebSearch?: boolean) => Promise<void>
  reset: () => void
}

const API_BASE = '/api'

export function useResearchStream(): UseResearchStreamReturn {
  const [state, setState] = useState<StreamState>({
    events: [],
    status: 'idle',
    streamingText: '',
    jobId: null,
  })

  const esRef = useRef<EventSource | null>(null)
  const lastSeqRef = useRef<number>(0)
  // Guards against rage-clicks firing multiple POSTs before the first setState re-renders
  const submittingRef = useRef<boolean>(false)

  const closeStream = useCallback(() => {
    if (esRef.current) {
      esRef.current.close()
      esRef.current = null
    }
  }, [])

  const openStream = useCallback((jobId: string) => {
    closeStream()

    const url = `${API_BASE}/research/${jobId}/stream`
    const es = new EventSource(url)
    esRef.current = es

    const handleEvent = (e: MessageEvent, type: string) => {
      try {
        const event: AgentEvent = JSON.parse(e.data)
        lastSeqRef.current = event.seq

        setState(prev => {
          const newEvents = [...prev.events, event]
          let newStreaming = prev.streamingText
          let newStatus = prev.status

          if (type === 'token') {
            newStreaming += (event.data as { text: string }).text
          }
          if (type === 'portfolio_section' || type === 'done' || type === 'error') {
            newStreaming = ''
          }
          if (type === 'done') newStatus = 'done'
          if (type === 'error') newStatus = 'error'

          return { ...prev, events: newEvents, streamingText: newStreaming, status: newStatus }
        })

        if (type === 'done' || type === 'error') {
          closeStream()
        }
      } catch {
        // ignore malformed events
      }
    }

    for (const t of [
      'phase_start', 'plan', 'tool_call', 'tool_result', 'token',
      'warning', 'portfolio_section', 'done', 'error', 'heartbeat',
    ]) {
      es.addEventListener(t, (e) => handleEvent(e as MessageEvent, t))
    }

    es.onerror = () => {
      // SSE auto-reconnects; when job is done the server closes the connection
      // which triggers onerror — that's fine, just close cleanly
      if (state.status === 'done' || state.status === 'error') {
        closeStream()
      }
    }
  }, [closeStream, state.status])

  // Cleanup on unmount
  useEffect(() => {
    return () => closeStream()
  }, [closeStream])

  const startResearch = useCallback(async (companyName: string, disableWebSearch = false) => {
    if (submittingRef.current) return
    submittingRef.current = true
    closeStream()
    setState({ events: [], status: 'running', streamingText: '', jobId: null })
    lastSeqRef.current = 0

    try {
      const resp = await fetch(`${API_BASE}/research`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company_name: companyName, disable_web_search: disableWebSearch }),
      })

      if (resp.status === 429) {
        const err = await resp.json()
        setState(prev => ({
          ...prev,
          status: 'error',
          events: [{
            type: 'error', job_id: '', seq: 0,
            data: { message: `Another job is running (${err.detail?.active_job_id ?? 'unknown'})` },
          }],
        }))
        return
      }

      if (!resp.ok) {
        setState(prev => ({
          ...prev,
          status: 'error',
          events: [{ type: 'error', job_id: '', seq: 0, data: { message: `HTTP ${resp.status}` } }],
        }))
        return
      }

      const job = await resp.json()
      setState(prev => ({ ...prev, jobId: job.job_id }))
      openStream(job.job_id)
    } catch (err) {
      setState(prev => ({
        ...prev,
        status: 'error',
        events: [{ type: 'error', job_id: '', seq: 0, data: { message: `Network error: ${err}` } }],
      }))
    } finally {
      submittingRef.current = false
    }
  }, [closeStream, openStream])

  const reset = useCallback(() => {
    closeStream()
    setState({ events: [], status: 'idle', streamingText: '', jobId: null })
  }, [closeStream])

  return { ...state, startResearch, reset }
}
