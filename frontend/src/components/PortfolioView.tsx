import type { Portfolio, Financials, FinancialYear } from '../types'

function fmt(v: number | null | undefined, suffix = ''): string {
  if (v == null || isNaN(v)) return '—'
  if (Math.abs(v) >= 1e9) return `$${(v / 1e9).toFixed(2)}B${suffix}`
  if (Math.abs(v) >= 1e6) return `$${(v / 1e6).toFixed(1)}M${suffix}`
  return `$${v.toLocaleString()}${suffix}`
}

function pct(v: number | null | undefined): string {
  if (v == null || isNaN(v)) return '—'
  return `${(v * 100).toFixed(1)}%`
}

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric-card">
      <span className="metric-label">{label}</span>
      <span className="metric-value">{value}</span>
    </div>
  )
}

function FinancialsSection({ fin }: { fin: Financials }) {
  return (
    <div className="portfolio-section">
      <h3>Financial Overview</h3>
      <div className="metrics-grid">
        <MetricCard label="Market Cap" value={fmt(fin.market_cap)} />
        <MetricCard label="Revenue (TTM)" value={fmt(fin.revenue_ttm)} />
        <MetricCard label="Enterprise Value" value={fmt(fin.enterprise_value)} />
        <MetricCard label="P/E Ratio" value={fin.pe_ratio?.toFixed(1) ?? '—'} />
        <MetricCard label="Gross Margin" value={pct(fin.gross_margin)} />
        <MetricCard label="Net Margin" value={pct(fin.net_margin)} />
        <MetricCard label="Revenue Growth" value={pct(fin.revenue_growth_yoy)} />
      </div>
      {fin.annual.length > 0 && (
        <table className="financials-table">
          <thead>
            <tr>
              <th>Year</th>
              <th>Revenue</th>
              <th>Gross Profit</th>
              <th>Net Income</th>
              <th>EBITDA</th>
            </tr>
          </thead>
          <tbody>
            {[...fin.annual].sort((a, b) => b.year - a.year).map((yr: FinancialYear) => (
              <tr key={yr.year}>
                <td>{yr.year}</td>
                <td>{fmt(yr.revenue)}</td>
                <td>{fmt(yr.gross_profit)}</td>
                <td>{fmt(yr.net_income)}</td>
                <td>{fmt(yr.ebitda)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

interface Props {
  portfolio: Portfolio
  jobId: string
  excelReady: boolean
}

export function PortfolioView({ portfolio, jobId, excelReady }: Props) {
  const { company, financials, competitors, key_products, recent_news,
          risks, opportunities, analyst_summary, data_gaps, headcount,
          market_position } = portfolio

  return (
    <div className="portfolio">
      {/* Header */}
      <div className="portfolio-header">
        <div>
          <h2 className="company-name">{company.name}</h2>
          <div className="company-meta">
            {company.ticker && <span className="badge badge-ticker">{company.ticker}</span>}
            {company.exchange && <span className="badge">{company.exchange}</span>}
            {company.sector && <span className="badge">{company.sector}</span>}
            {company.country && <span className="badge">{company.country}</span>}
          </div>
        </div>
        {excelReady && (
          <a
            href={`/api/research/${jobId}/excel`}
            className="btn-download"
            download
          >
            Download Excel
          </a>
        )}
      </div>

      {/* Analyst Summary */}
      {analyst_summary && (
        <div className="portfolio-section">
          <h3>Analyst Summary</h3>
          <p className="summary-text">{analyst_summary}</p>
        </div>
      )}

      {/* Company Profile */}
      <div className="portfolio-section">
        <h3>Company Profile</h3>
        <div className="profile-grid">
          {company.description && <p className="description">{company.description}</p>}
          <div className="profile-fields">
            {headcount?.value != null && (
              <div className="profile-field">
                <span className="field-label">Employees</span>
                <span>{String(headcount.value)}</span>
                <span className={`confidence confidence-${headcount.confidence}`}>
                  {headcount.confidence}
                </span>
              </div>
            )}
            {market_position?.value != null && (
              <div className="profile-field">
                <span className="field-label">Market Position</span>
                <span>{String(market_position.value)}</span>
              </div>
            )}
            {company.founded && (
              <div className="profile-field">
                <span className="field-label">Founded</span>
                <span>{company.founded}</span>
              </div>
            )}
            {company.headquarters && (
              <div className="profile-field">
                <span className="field-label">HQ</span>
                <span>{company.headquarters}</span>
              </div>
            )}
            {company.website && (
              <div className="profile-field">
                <span className="field-label">Website</span>
                <a href={company.website} target="_blank" rel="noreferrer">{company.website}</a>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Financials */}
      <FinancialsSection fin={financials} />

      {/* Key Products */}
      {key_products.length > 0 && (
        <div className="portfolio-section">
          <h3>Key Products & Services</h3>
          <ul className="bullet-list">
            {key_products.map((p, i) => <li key={i}>{p}</li>)}
          </ul>
        </div>
      )}

      {/* Competitors */}
      {competitors.length > 0 && (
        <div className="portfolio-section">
          <h3>Key Competitors</h3>
          <table className="competitors-table">
            <thead>
              <tr><th>Company</th><th>Ticker</th><th>Market Cap</th><th>Revenue</th><th>Notes</th></tr>
            </thead>
            <tbody>
              {competitors.map((c, i) => (
                <tr key={i}>
                  <td>{c.name}</td>
                  <td>{c.ticker ?? '—'}</td>
                  <td>{fmt(c.market_cap)}</td>
                  <td>{fmt(c.revenue_ttm)}</td>
                  <td className="notes">{c.summary ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Risks & Opportunities */}
      {(risks.length > 0 || opportunities.length > 0) && (
        <div className="portfolio-section two-col">
          {risks.length > 0 && (
            <div>
              <h3>Key Risks</h3>
              <ul className="bullet-list risk">{risks.map((r, i) => <li key={i}>{r}</li>)}</ul>
            </div>
          )}
          {opportunities.length > 0 && (
            <div>
              <h3>Opportunities</h3>
              <ul className="bullet-list opp">{opportunities.map((o, i) => <li key={i}>{o}</li>)}</ul>
            </div>
          )}
        </div>
      )}

      {/* Recent News */}
      {recent_news.length > 0 && (
        <div className="portfolio-section">
          <h3>Recent News</h3>
          <ul className="bullet-list">{recent_news.map((n, i) => <li key={i}>{n}</li>)}</ul>
        </div>
      )}

      {/* Data Gaps */}
      {data_gaps.length > 0 && (
        <div className="portfolio-section data-gaps">
          <h3>Data Gaps</h3>
          <ul>{data_gaps.map((g, i) => <li key={i}>⚠ {g}</li>)}</ul>
        </div>
      )}
    </div>
  )
}
