# Company Research Agent

A production-leaning agentic system that researches any company on demand, producing a structured portfolio and Excel workbook. All computation runs locally — $0 API cost, no external keys required.

## Architecture

```
User Input
    │
    ▼
FastAPI (SSE stream)
    │
    ▼
4-Phase Pipeline
├── Phase 0 · RESOLVE    — LLM identifies company, ticker, public/private
├── Phase 1 · RESEARCH   — Bounded tool-calling loop (12 iter max)
│   ├── get_company_profile(ticker)    → yfinance (free)
│   ├── get_company_financials(ticker) → yfinance (free)
│   ├── web_search(query)              → DuckDuckGo (free, with jitter)
│   └── fetch_page(url)               → httpx + trafilatura + LLM summary
├── Phase 2 · SYNTHESIZE — LLM structures evidence → Portfolio JSON
└── Phase 3 · EXCEL      — openpyxl builds formatted workbook + chart
    │
    ▼
SSE Events (streamed to frontend)
React UI (live timeline + tool call cards + portfolio view)
```

## Resilience Design

| Concern | Mitigation |
|---|---|
| Tool network failures | Retry (exponential backoff + jitter) per tool |
| Persistent tool failures | Circuit breaker per tool (opens after 3 failures, 60s recovery) |
| LLM timeouts | 120s cap per inference call |
| Context window overflow | Per-page LLM summarization (~600 tokens/page, not raw text) |
| DDG rate limiting | Mandatory 1.5–4s inter-call jitter |
| Malformed LLM JSON | JSON-repair re-prompt loop (up to 2 repair attempts) |
| Stubborn tool retry loops | (tool, args) deduplication hash in research loop |
| Private companies (no yfinance) | Public/private classification → web-only fallback |
| GPU contention | asyncio.Semaphore(1) on all Ollama inference calls |
| Concurrent requests | 429 + active_job_id returned; one job at a time |
| SSE silent hang on exceptions | try/except in streaming generator → guaranteed error event |
| Excel file locked on Windows | Versioned filename fallback (_v2, _v3) |
| SSE reconnection | Last-Event-ID replay from in-memory event buffer |

## Models

| Role | Model | Notes |
|---|---|---|
| Primary orchestrator | `qwen3:8b` | Best open-weight tool-caller at ~5GB |
| Fallback | `llama3.1:8b` | Different model family for failure diversity |

## Data Sources (all free, no API keys)

- **yfinance** — structured financials for public companies (revenue, margins, income statements)
- **DuckDuckGo** via `ddgs` — web search for qualitative research
- **httpx + trafilatura** — page fetching + clean text extraction

## Setup

### Prerequisites
- Python 3.12+
- Node 18+
- [Ollama](https://ollama.ai) installed and running

### Backend

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate  # Windows
pip install -e ".[dev]"

# Pull models (one-time, ~10GB total)
ollama pull qwen3:8b
ollama pull llama3.1:8b

uvicorn app.main:app --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:5173](http://localhost:5173)

### Docker (full stack — recommended)

```bash
docker compose up
```

That's it. On first run Docker will:
1. Start Ollama
2. Automatically pull `qwen3:8b` and `llama3.1:8b` (~10 GB total — one-time download)
3. Start the backend and frontend

Open [http://localhost:5173](http://localhost:5173) once the logs show `Application startup complete`.

> **First run takes 5–15 minutes** depending on your internet speed (model downloads). Subsequent runs start in seconds — models are cached in a Docker volume.

**CPU vs GPU:**
- The default `docker compose up` runs on **CPU** (works on any machine)
- For NVIDIA GPU acceleration (faster inference), use: `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up`

## CLI Dry-Run (no frontend needed)

```bash
cd backend
.venv/Scripts/activate
python -m app.agent.cli "Apple Inc"
python -m app.agent.cli "Stripe" --verbose
```

## Tests

```bash
cd backend
pytest tests/ --ignore=tests/test_tools.py -v
# 70 tests, all unit-tested with mocked network
```

## Project Structure

```
backend/
  app/
    main.py               FastAPI app — routes + SSE streaming
    config.py             pydantic-settings config
    agent/
      schemas.py          Pydantic models: Portfolio, Financials, Evidence…
      orchestrator.py     4-phase pipeline
      loop.py             Raw tool-calling research loop
      prompts.py          LLM prompts for each phase
      cli.py              CLI dry-run entry point
    llm/
      ollama_client.py    Streaming Ollama client + model fallback + semaphore
    tools/
      registry.py         Tool registry + schema generation
      financials.py       yfinance tools
      web_search.py       DuckDuckGo search with jitter
      fetch_page.py       Page fetch + LLM summarization
    resilience/
      retry.py            Exponential backoff retry
      circuit_breaker.py  Per-tool circuit breaker
      timeout.py          Async timeout wrapper
    streaming/
      events.py           Typed SSE event constructors
      bus.py              In-memory event bus with replay
    excel/
      builder.py          openpyxl workbook builder
    jobs/
      store.py            In-memory job store
    observability/
      logging.py          structlog configuration
  tests/                  37 unit tests

frontend/
  src/
    hooks/
      useResearchStream.ts  SSE hook with proper cleanup
    components/
      SearchForm.tsx
      EventTimeline.tsx
      ToolCallCard.tsx
      PortfolioView.tsx
    types/index.ts
    App.tsx
```
