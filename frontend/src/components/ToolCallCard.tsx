import type { AgentEvent } from '../types'

interface Props {
  callEvent: AgentEvent
  resultEvent?: AgentEvent
}

function formatArgs(args: Record<string, unknown>): string {
  const entries = Object.entries(args)
    .filter(([k]) => k !== 'llm')  // hide injected llm reference
    .map(([k, v]) => `${k}: ${JSON.stringify(v)}`)
  return entries.join(', ')
}

function formatResult(result: unknown): string {
  if (typeof result === 'string') return result.slice(0, 200)
  try {
    const s = JSON.stringify(result, null, 2)
    return s.length > 300 ? s.slice(0, 300) + '…' : s
  } catch {
    return String(result)
  }
}

const TOOL_ICONS: Record<string, string> = {
  web_search: '🔍',
  fetch_page: '📄',
  get_company_financials: '📊',
  get_company_profile: '🏢',
}

export function ToolCallCard({ callEvent, resultEvent }: Props) {
  const { tool, args } = callEvent.data as { tool: string; args: Record<string, unknown> }
  const result = resultEvent?.data as { result: unknown; success: boolean; duration_ms: number } | undefined
  const icon = TOOL_ICONS[tool] ?? '⚙️'
  const pending = !resultEvent

  return (
    <div className={`tool-card ${pending ? 'tool-pending' : result?.success ? 'tool-success' : 'tool-error'}`}>
      <div className="tool-header">
        <span className="tool-icon">{icon}</span>
        <span className="tool-name">{tool}</span>
        <span className="tool-args">{formatArgs(args)}</span>
        {result && (
          <span className="tool-duration">{result.duration_ms}ms</span>
        )}
        {pending && <span className="tool-spinner" />}
      </div>
      {result && (
        <div className="tool-result">
          <pre>{formatResult(result.result)}</pre>
        </div>
      )}
    </div>
  )
}
