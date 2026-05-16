export type EventType =
  | 'phase_start'
  | 'plan'
  | 'tool_call'
  | 'tool_result'
  | 'token'
  | 'warning'
  | 'portfolio_section'
  | 'done'
  | 'error'
  | 'heartbeat'

export interface AgentEvent {
  type: EventType
  job_id: string
  seq: number
  data: Record<string, unknown>
}

export interface PhaseStartData {
  phase: string
  description: string
}

export interface ToolCallData {
  tool: string
  args: Record<string, unknown>
}

export interface ToolResultData {
  tool: string
  result: unknown
  success: boolean
  duration_ms: number
}

export interface PortfolioSectionData {
  section: string
  content: unknown
}

export type JobStatus = 'idle' | 'running' | 'done' | 'error'

export interface ResearchJob {
  job_id: string
  company_name: string
  status: string
  portfolio: Portfolio | null
  excel_ready: boolean
  error: string | null
}

export interface Portfolio {
  company: Company
  headcount: Evidence | null
  financials: Financials
  market_position: Evidence | null
  key_products: string[]
  competitors: Competitor[]
  recent_news: string[]
  risks: string[]
  opportunities: string[]
  analyst_summary: string | null
  data_gaps: string[]
}

export interface Company {
  name: string
  ticker: string | null
  type: string
  exchange: string | null
  sector: string | null
  industry: string | null
  country: string | null
  website: string | null
  description: string | null
  founded: string | null
  headquarters: string | null
}

export interface Financials {
  market_cap: number | null
  enterprise_value: number | null
  pe_ratio: number | null
  revenue_ttm: number | null
  gross_margin: number | null
  net_margin: number | null
  revenue_growth_yoy: number | null
  annual: FinancialYear[]
  currency: string
  data_source: string
}

export interface FinancialYear {
  year: number
  revenue: number | null
  net_income: number | null
  gross_profit: number | null
  operating_income: number | null
  ebitda: number | null
  eps: number | null
}

export interface Competitor {
  name: string
  ticker: string | null
  market_cap: number | null
  revenue_ttm: number | null
  summary: string | null
}

export interface Evidence {
  value: unknown
  source: string
  confidence: 'high' | 'medium' | 'low'
}
