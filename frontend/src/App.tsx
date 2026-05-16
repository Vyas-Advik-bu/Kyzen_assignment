import { useResearchStream } from './hooks/useResearchStream'
import { SearchForm } from './components/SearchForm'
import { EventTimeline } from './components/EventTimeline'
import { PhaseStepper } from './components/PhaseStepper'
import { PortfolioView } from './components/PortfolioView'
import type { Portfolio } from './types'

function extractPortfolio(events: ReturnType<typeof useResearchStream>['events']): Portfolio | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i]
    if (ev.type === 'portfolio_section' && (ev.data as { section: string }).section === 'portfolio') {
      return (ev.data as { content: Portfolio }).content
    }
  }
  return null
}

export default function App() {
  const { events, status, streamingText, jobId, startResearch, reset } = useResearchStream()
  const portfolio = status === 'done' ? extractPortfolio(events) : null
  const hasActivity = events.length > 0 || status === 'running'

  return (
    <div className="app">
      <SearchForm status={status} onSubmit={(company, disableWebSearch) => startResearch(company, disableWebSearch)} onReset={reset} />

      {hasActivity && <PhaseStepper events={events} status={status} />}

      {hasActivity && (
        <div className="content">
          {/* Left: live agent timeline */}
          <div className="timeline-panel">
            <h2 className="panel-title">Agent Activity</h2>
            <EventTimeline events={events} streamingText={streamingText} status={status} />
          </div>

          {/* Right: portfolio (shown when done) */}
          {portfolio && jobId && (
            <div className="portfolio-panel">
              <PortfolioView
                portfolio={portfolio}
                jobId={jobId}
                excelReady={status === 'done'}
              />
            </div>
          )}
        </div>
      )}

      {status === 'error' && !portfolio && (
        <div className="error-banner">
          {events.find(e => e.type === 'error') && (
            <p>{String((events.find(e => e.type === 'error')!.data as { message: string }).message)}</p>
          )}
        </div>
      )}
    </div>
  )
}
