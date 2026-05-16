import { useState, type FormEvent } from 'react'
import type { JobStatus } from '../types'

interface Props {
  status: JobStatus
  onSubmit: (company: string, disableWebSearch: boolean) => void
  onReset: () => void
}

export function SearchForm({ status, onSubmit, onReset }: Props) {
  const [value, setValue] = useState('')
  const [disableWebSearch, setDisableWebSearch] = useState(false)
  const running = status === 'running'

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (value.trim()) onSubmit(value.trim(), disableWebSearch)
  }

  return (
    <div className="search-form">
      <h1 className="title">Company Research Agent</h1>
      <p className="subtitle">
        Enter a company name to generate a comprehensive research portfolio.
      </p>
      <form onSubmit={handleSubmit} className="form-row">
        <input
          className="company-input"
          type="text"
          placeholder="e.g. Apple, Stripe, OpenAI..."
          value={value}
          onChange={e => setValue(e.target.value)}
          disabled={running}
          autoFocus
        />
        {status === 'idle' || status === 'running' ? (
          <button
            className={`btn-primary ${running ? 'btn-loading' : ''}`}
            type="submit"
            disabled={running || !value.trim()}
          >
            {running ? 'Researching…' : 'Research'}
          </button>
        ) : (
          <button className="btn-secondary" type="button" onClick={onReset}>
            New Search
          </button>
        )}
      </form>
      <label className="search-option">
        <input
          type="checkbox"
          checked={disableWebSearch}
          onChange={e => setDisableWebSearch(e.target.checked)}
          disabled={running}
        />
        <span>Disable web search (use financial APIs only)</span>
      </label>
    </div>
  )
}
