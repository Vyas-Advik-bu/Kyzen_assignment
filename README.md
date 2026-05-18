# Company Research Agent

An agentic system that researches any company on demand, producing a structured portfolio and Excel workbook with live streaming progress.

## Architecture

```
User Input
    │
    ▼
FastAPI (SSE stream)
    │
    ▼
4-Phase Pipeline
├── Phase 0 · RESOLVE    - LLM identifies company, ticker, public/private
├── Phase 1 · RESEARCH   - Bounded tool-calling loop (8 iter max)
│   ├── get_company_profile(ticker)    → yfinance
│   ├── get_company_financials(ticker) → yfinance
│   ├── web_search(query)              → Tavily
│   └── fetch_page(url)               → httpx + trafilatura + LLM summary
├── Phase 2 · SYNTHESIZE - LLM structures evidence → Portfolio JSON
└── Phase 3 · EXCEL      - openpyxl builds formatted workbook + chart
    │
    ▼
SSE Events (streamed to frontend)
React UI (live timeline + tool call cards + portfolio view)
```

## Resilience Design

| Concern | Mitigation |
|---|---|
| Tool network failures | Retry with exponential backoff per tool |
| Persistent tool failures | Circuit breaker per tool (opens after 3 failures, 60s recovery) |
| LLM timeouts | 120s cap per inference call |
| Context window overflow | Per-page LLM summarization (~600 tokens/page) |
| Gemini rate limiting | 2s inter-call delay + 429 backoff (12s, 24s, 36s) |
| Malformed LLM JSON | JSON-repair re-prompt loop (up to 2 attempts) |
| Stubborn tool retry loops | (tool, args) deduplication hash in research loop |
| Private companies (no ticker) | Public/private classification → web-only fallback |
| LLM inference concurrency | asyncio.Semaphore(1) — one Gemini call at a time |
| Concurrent requests | One active job at a time; 429 returned otherwise |
| SSE silent hang on exceptions | try/except in streaming generator → guaranteed error event |
| Excel file locked on Windows | Versioned filename fallback (_v2, _v3) |
| SSE reconnection | Last-Event-ID replay from in-memory event buffer |

## Models

| Role | Model | Notes |
|---|---|---|
| Primary | `gemini-3.1-flash-lite` | 15 RPM / 500 RPD on free tier |
| Fallback | `gemini-2.5-flash-lite` | Used on primary model error or timeout |

## Data Sources

- **yfinance** — structured financials for public companies (revenue, margins, income statements)
- **Tavily** — web search optimised for AI agents
- **httpx + trafilatura** — page fetching and clean text extraction

## Setup

### Prerequisites
- Python 3.12+
- Node 18+
- A `backend/.env` file with your API keys (see below)

### API Keys

| Key | Free tier | Link |
|---|---|---|
| `GEMINI_API_KEY` | 15 RPM / 500 RPD | https://aistudio.google.com/apikey |
| `TAVILY_API_KEY` | 1000 searches/month | https://app.tavily.com |

Create `backend/.env`:

```
GEMINI_API_KEY=your_key_here
TAVILY_API_KEY=your_key_here
```

### Backend

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate  # Windows
pip install -e ".[dev]"
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

The backend reads `GEMINI_API_KEY` and `TAVILY_API_KEY` from environment. Pass them at runtime:

```bash
GEMINI_API_KEY=your_key TAVILY_API_KEY=your_key docker compose up
```

Or export them in your shell before running `docker compose up`.

Open [http://localhost:5173](http://localhost:5173) once the logs show `Application startup complete`.

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
      loop.py             Tool-calling research loop
      prompts.py          LLM prompts for each phase
      cli.py              CLI dry-run entry point
    llm/
      ollama_client.py    Gemini client — streaming, rate limiting, fallback
    tools/
      registry.py         Tool registry + schema generation
      financials.py       yfinance tools
      web_search.py       Tavily web search
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
  tests/                  70 unit tests

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

---

## Alternative: Local Stack (no API keys)

The original implementation runs entirely locally with no external API dependencies. The code for each component is preserved in the codebase as commented-out blocks.

| Component | Local alternative |
|---|---|
| LLM | [Ollama](https://ollama.ai) — `qwen3:8b` (primary), `llama3.1:8b` (fallback) |
| Web search | DuckDuckGo via `duckduckgo-search` |
| Financials | yfinance (unchanged) |

To restore the local stack:

1. Install and run Ollama, then pull the models:
   ```bash
   ollama pull qwen3:8b
   ollama pull llama3.1:8b
   ```

2. In `backend/app/config.py`, uncomment the Ollama settings block and remove the Gemini keys.

3. In `backend/app/llm/ollama_client.py`, swap the active implementation with the commented Ollama client at the bottom of the file.

4. In `backend/app/tools/web_search.py`, swap to the commented DuckDuckGo implementation.

5. Update `pyproject.toml` dependencies accordingly (`duckduckgo-search`, remove `tavily-python`).

The Docker setup for the local stack is preserved in `docker-compose.yml` (commented Ollama services at the bottom) and `docker-compose.gpu.yml` for NVIDIA GPU acceleration.
