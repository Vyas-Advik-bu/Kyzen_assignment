# Company Research Agent Project Report

## 1. Abstract

An agentic AI system that takes a single company name and autonomously produces a structured research portfolio plus a formatted Excel workbook — financials, competitors, risks, news, the works.

---

## 2. The Problem & The Constraints

The task is to turn the search for `"Apple"` into a complete, structured company portfolio + downloadable Excel.

**The constraints:**

| Constraint | Consequence |
|---|---|
| **Cloud LLM (Gemini)** | Rate-limit-aware client: inter-call delay + 429 backoff; thinking mode disabled to avoid `thought_signature` protocol errors |
| **Free-tier APIs** | Gemini: 15 RPM / 500 RPD; Tavily: 1000 searches/month — every call has to count |
| **Unreliable components** | Models hallucinate/hang; pages 404; APIs rate-limit — everything needs a fallback |
| **"Production-leaning"** | Streaming UX, structured logging, circuit breakers, graceful degradation |

---

## 3. High-Level Architecture

```
Browser (React + Vite)
   |  POST /research                    GET /research/{id}/stream  (SSE)
   v                                    ^
FastAPI  ------------------------------/
   |   spawns asyncio task
   v
run_pipeline()  -- 4 phases, each emits events -->  JobEventBus  --> SSE
   |
   +- Phase 0 RESOLVE     LLM -> {name, ticker, public/private}
   +- Phase 1 RESEARCH    bounded tool-calling loop (<=8 iterations)
   |     |- get_company_profile(ticker)     -> yfinance
   |     |- get_company_financials(ticker)  -> yfinance
   |     |- web_search(query)               -> Tavily
   |     |- fetch_page(url)                 -> httpx + trafilatura + LLM summary
   +- Phase 2 SYNTHESIZE  LLM -> strict Portfolio JSON
   +- Phase 3 EXCEL       openpyxl -> 4-sheet workbook + revenue chart
```

**Two HTTP interactions, deliberately decoupled:**
1. `POST /research` returns immediately (HTTP 202) with a `job_id` — the work runs in a background `asyncio` task.
2. `GET /research/{job_id}/stream` is a long-lived SSE connection replaying everything the agent does, live.

---

## 4. The 4-Phase Pipeline (the heart of it)

All four phases live in `orchestrator.py`, wrapped in a single `async with asyncio.timeout(1200)`, which is a 20-minute hard ceiling so the agent doesn't hang forever.

### Phase 0 — Resolve
The user types `"apple"`. The LLM is asked for strict JSON: official name, stock ticker, public/private classification, exchange. This is the disambiguation + routing step. Knowing the ticker unlocks the structured yfinance path; knowing it's private routes to web-only research. A secondary sanity check (`_verify_ticker`) cross-references the LLM's proposed ticker against yfinance to prevent mismatches (e.g. "Samsung" -> SM Energy instead of Samsung Electronics).

### Phase 1 — Research (the agentic loop)
A real tool-calling agent loop is covered in depth in section 6. The LLM is handed 4 tools and reasons step-by-step: "I have the ticker, fetch financials -> now I need headcount, search the web -> now competitors..." until it emits the literal stop-token `RESEARCH_COMPLETE`.

### Phase 2 — Synthesize
All accumulated evidence is compacted and handed back to the LLM with an explicit JSON schema (every field name spelled out). The model turns messy raw evidence into one clean `Portfolio` object. Structured financial data (revenue, margins, etc.) is extracted directly from tool results before synthesis, bypassing the LLM for those fields to avoid unit-conversion errors.

### Phase 3 — Excel
`openpyxl` builds a 4-sheet workbook (Overview, Income Statement, Key Metrics, Competitors) with a native bar chart of annual revenue.

Each phase publishes a `phase_start` event -> the frontend's progress stepper lights up in real time.

---

## 5. Tech Stack — What & Why

### Backend (Python 3.12)

| Tech | Role | Why this one |
|---|---|---|
| **FastAPI** | Web framework | Native async (essential for SSE + concurrent tool calls), automatic OpenAPI, Pydantic-integrated |
| **Uvicorn** | ASGI server | Standard async server for FastAPI |
| **sse-starlette** | SSE responses | Purpose-built `EventSourceResponse`; handles SSE framing correctly |
| **Pydantic v2** | Data modeling/validation | Every boundary (`Portfolio`, `Financials`, API bodies) is a validated model; v2 is fast (Rust core) |
| **pydantic-settings** | Config | 12-factor env-var config with typed defaults |
| **httpx** | Async HTTP | `AsyncClient` + streaming responses — used for Gemini REST calls and page fetching |
| **structlog** | Logging | Structured key-value logs — greppable, machine-parseable |
| **yfinance** | Financial data | Free, no API key — public company financials, income statements |
| **tavily-python** | Web search | Purpose-built for AI agents — no scraping games, clean ranked results |
| **trafilatura** | HTML->text extraction | Best-in-class boilerplate removal — strips nav/ads, keeps article body |
| **openpyxl** | Excel generation | The standard for `.xlsx` with styling + charts |
| **pytest + pytest-asyncio + respx** | Testing | `respx` mocks httpx so tests never touch the network |

**The LLM stack:** Gemini (Google Generative AI) via REST API:
- **`gemini-3.1-flash-lite`** : primary model. Fast, low-latency, strong tool-calling.
- **`gemini-2.5-flash-lite`** : fallback model for if the primary model fails. Still relatively good performance compared to 3.1 flash lite

### Frontend (TypeScript)

| Tech | Role | Why |
|---|---|---|
| **React 18** | UI | Component model fits the live event-stream UI |
| **Vite 5** | Build/dev server | Instant HMR; dev-proxy forwards `/api` -> backend (no CORS pain) |
| **TypeScript** | Type safety | Frontend `types/index.ts` mirrors backend Pydantic models — one schema, two languages |
| **Native `EventSource`** | SSE client | Browser-built-in; auto-reconnects and sends `Last-Event-ID` for free — no library needed |
| **Plain CSS** | Styling | No Tailwind/CSS-in-JS — keeps the dependency tree tiny |

### Infrastructure
- **Docker Compose** — 2 services (backend + frontend), one `docker compose up`. API keys injected as environment variables.
- **Multi-stage frontend Dockerfile** — Node builds, output served by nginx (tiny final image). nginx also reverse-proxies `/api` with `proxy_buffering off` — mandatory for SSE (buffering would batch the live stream into one dump).

---

## 6. The Agentic Research Loop — Deep Dive

This is `loop.py`, and it's the most interesting code in the project. A naive tool-calling loop will misbehave: infinite loops, repeated identical calls, fetching 50 pages. This loop has five independent guardrails:

1. **`MAX_ITERATIONS = 8`** — hard cap on reasoning rounds. The `for...else` clause emits a warning if the cap is hit without a clean finish.

2. **Explicit stop signal** — the model must emit the literal string `RESEARCH_COMPLETE`. No guessing whether it's "done."

3. **Exact-duplicate dedup** — `(tool_name, sorted_args_json)` is hashed into a `seen_calls` set. Models love to re-run the identical search; the duplicate is rejected and the model is told so via a synthetic tool-error message (so it adjusts instead of silently looping).

4. **Per-tool call caps** — `web_search`: 6, `fetch_page`: 3, profile/financials: 2 each. Stops the "fetch-all" trap and semantic oscillation (slightly-reworded-but-equivalent searches).

5. **Globally unique tool-call IDs** — `call_0, call_1, ...` assigned across all iterations. The Gemini message protocol pairs `tool_call_id`s; iteration-local IDs collide and corrupt the conversation history.

Each tool result becomes an evidence dict `{tool, args, result}`. Tool failures don't crash the loop — the exception is caught, serialized as `{"error": ...}`, and fed back so the model can react.

---

## 7. The LLM Layer — `ollama_client.py`

A hand-rolled async Gemini client (named `GeminiClient`, aliased as `OllamaClient` for backward compatibility). Three call modes:
- **`chat()`** — non-streaming, for structured JSON synthesis.
- **`stream_chat()`** — streaming, yields either `str` tokens or `ToolCall` objects, for the research loop.
- **`generate_short()`** — single-prompt, capped at 700 tokens, for per-page summarization.

**Important to note:**

- **Thinking mode disabled** — `"thinkingConfig": {"thinkingBudget": 0}` in every payload. Gemini 3.x models have thinking enabled by default; when active, tool call responses include a `thought_signature` field that must be echoed back in subsequent messages. Our message converter doesn't handle this, so thinking is disabled to keep the protocol simple.

- **Single global inference semaphore** (`Semaphore(1)`) — enforces one Gemini call at a time. Controlled backpressure beats silently queuing requests until SSE streams time out.

- **Rate-limit defence** — a 2-second inter-call delay before every request keeps throughput under 15 RPM. On top of that, 429 responses retry with linear backoff (12s, 24s, 36s) — long enough to let the per-minute window drain before re-attempting.

- **Model fallback** — if the primary errors, transparently retry on the fallback model. Applied on the first chunk only for streaming. Once tokens are flowing, a mid-stream failure can't be cleanly restarted.

- **Two-layer timeout defence:**
  - `_CHAT_TIMEOUT = 120s` — total wall-clock per call.
  - `_IDLE_TIMEOUT = 45s` — per-line idle timeout on streaming. Wraps each `__anext__()` on the line iterator in `asyncio.wait_for` — detects a hung connection that accepts TCP but sends zero bytes, in 45s rather than waiting the full 120s.

---

## 8. The Tools Layer

Tools are registered in a `registry.py` `ToolRegistry` that also generates the JSON schemas Gemini needs. Four tools:

**`get_company_profile` / `get_company_financials`** (`financials.py`) : yfinance. yfinance is synchronous and blocking, so every call is wrapped in `asyncio.to_thread()` to avoid stalling the event loop. Returns up to 4 years of income statements. Empty dict from Yahoo (blocked user-agent / bad ticker) is handled, not crashed. Structured data is extracted directly from these results in the orchestrator before the synthesis LLM sees it, bypassing the model for numbers to avoid unit-conversion errors.

**`web_search`** (`web_search.py`) : Tavily. Purpose-built for AI agent use: no scraping, no jitter games, clean ranked results with content snippets. Key subtlety: an empty result list is treated as a soft failure, not "no results exist" — it raises `RuntimeError` so retry + circuit breaker engage. Silently returning `[]` would waste the agent's whole budget chasing nothing.

**`fetch_page`** (`fetch_page.py`) : Fetch URL -> `trafilatura` extracts clean article text -> the LLM summarizes it to ~150 words before returning. This is the mitigation for the context-window trap: 3–4 raw web pages easily blow past 15k tokens and the model loses coherence; summaries stay ~600 tokens each. The agent researches on summaries.

---

## 9. Resilience Engineering

Three composable primitives in `resilience/`, each ~30 lines:

- **`retry_async`** — exponential backoff (`base * 2^n`) + jitter, capped delay. Has a `non_retriable` tuple so 4xx client errors (a 404 page) skip retries — retrying a 404 is pure waste.

- **`CircuitBreaker`** — per-tool. Opens after N consecutive failures (yfinance: 3, Tavily: 3), rejects calls for a recovery window, then half-opens to probe. Prevents hammering a service that's already down.

- **`with_timeout`** — `asyncio.wait_for` wrapper with a labeled `TimeoutError`.

They compose: `CircuitBreaker.call( retry_async( with_timeout(actual_fn) ) )`. Timeout is innermost, retry wraps it, breaker wraps that.

| Failure mode | Mitigation |
|---|---|
| Transient network blip | Retry w/ exponential backoff + jitter |
| Service persistently down | Circuit breaker (open -> half-open -> closed) |
| LLM call hangs | 120s total + 45s idle timeout |
| Whole pipeline hangs | 20-min `asyncio.timeout` ceiling |
| Context window overflow | Per-page LLM summarization (~600 tok vs ~5k raw) |
| Gemini rate limiting | 2s inter-call delay + 429 backoff (12s, 24s, 36s) |
| Malformed LLM JSON | JSON-repair re-prompt loop (<=2 attempts) |
| Agent stuck re-calling a tool | `(tool, args)` dedup hash |
| Agent over-using a tool | Per-tool call caps |
| Private company (no yfinance) | public/private classification -> web-only fallback |
| Wrong ticker from LLM | `_verify_ticker()` cross-checks against yfinance company name |
| Concurrent requests | `Semaphore(1)` -> second request gets `429 + active_job_id` |
| SSE generator throws mid-stream | `try/except` -> guaranteed terminal `error` event |
| Excel file locked (Windows) | Versioned filename fallback `_v2`, `_v3` |
| Connection drops | `Last-Event-ID` replay from in-memory buffer |

**JSON-repair loop:** models emit broken JSON occasionally — trailing commas, markdown fences, truncation. `_llm_json()` strips fences, tries `json.loads`, and on failure re-prompts the model with the parse error and the bad output, asking it to fix itself. Up to 2 repair attempts before degrading gracefully.

---

## 10. The Streaming Architecture — SSE + Event Bus

**Why SSE over WebSockets:** the data flow is one-directional (server->client). SSE is plain HTTP, the browser's `EventSource` auto-reconnects and resends `Last-Event-ID` for free, and there's no handshake overhead. WebSockets would be over-engineering.

**The `JobEventBus`** (`bus.py`) is an in-memory pub/sub:
- Every event is buffered in a per-job list and fanned out to live subscriber queues.
- **Reconnection replay:** subscribe with `last_event_id` -> replays `events[last_event_id + 1:]`. The `+1` is the off-by-one fix — `last_event_id` is the seq already received, so replay must exclude it. Fresh connections pass `-1` -> replay everything.
- **Memory management:** a `_closed` set marks finished jobs. When the last subscriber of a closed job disconnects, `_cleanup()` frees the buffer. Without this, every completed job leaks its event list forever.
- **Heartbeats** every 15s keep the connection alive through proxies.

10 typed event types (`events.py`): `phase_start`, `tool_call`, `tool_result`, `token`, `warning`, `portfolio_section`, `done`, `error`, `heartbeat`, `plan`. Every event is a Pydantic `AgentEvent` with `type`, `job_id`, `seq`, `data`.

**The SSE endpoint's safety net:** the generator is wrapped in `try/except`. Once HTTP 200 is committed you cannot send a 500, so on any mid-stream exception it emits a terminal `error` event. The frontend is never left hanging.

---

## 11. Excel Generation — `builder.py`

Four sheets: Overview (company facts + description + key products + data gaps), Income Statement (multi-year + native bar chart), Key Metrics (+ risks/opportunities), Competitors.

The defensive theme: a single `None` passed to an openpyxl chart crashes the whole phase. So:
- `_safe()` normalizes `None`/`NaN`/`inf` -> `"N/A"`.
- `_millions()` / `_pct()` format numbers human-readably (`$2.95B`, `24.3%`).
- The revenue chart only renders if >=2 valid numeric data points exist.
- **Windows file-lock fallback** — if `job.xlsx` is open in Excel, save retries as `_v2`, `_v3` instead of crashing.

---

## 12. The Frontend

A simple, focused React app.

- **`useResearchStream`** hook (`useResearchStream.ts`) — owns all SSE state. POSTs the job, opens `EventSource`, accumulates events, derives `status`. A `submittingRef` guards against rage-clicks firing duplicate POSTs before React re-renders, and it's reset in a `finally` so error paths can't leak the lock.
- **`PhaseStepper`** - the 4-step progress bar (Resolve->Research->Synthesize->Export) with pending/active/done/error states and animated connectors.
- **`EventTimeline`** - the live agent feed. Tool calls and results are paired positionally (n-th result <-> n-th call) and rendered as `ToolCallCard`s with spinners -> durations -> result previews.
- **`PortfolioView`** - the final structured report + Excel download button.
- **`SearchForm`** - input + a "disable web search" toggle (financial-APIs-only fast path).

TypeScript interfaces in `types/index.ts` mirror the backend Pydantic models exactly. The contract is enforced on both ends.

---

## 13. The QA / Security Hardening Round

A dedicated adversarial QA pass found 15 bugs + 3 security issues, all fixed, all with regression tests. Highlights worth naming in an interview:

- **SSRF prevention** - the LLM controls `fetch_page` URLs. A malicious/confused model could hit `http://localhost:8000` or `http://169.254.169.254/` (cloud metadata!). `_validate_url()` enforces an http/https allowlist and blocks private/loopback/link-local/reserved IPs via the `ipaddress` module.
- **Excel formula injection** - a company named `=HYPERLINK("evil.com")` would execute when the `.xlsx` is opened. `_safe_str()` space-prefixes any cell starting with `= + - @` or control chars -> Excel treats it as inert text.
- **HTTP header injection** - the download filename is derived from the company name; CRLF in it could inject headers. `_safe_download_filename()` strips everything non-alphanumeric.
- **TOCTOU race** - two fast requests could both pass the "is a job running?" check. Fixed by acquiring the semaphore synchronously in the request handler. No `await` between check and acquire, so no context switch can slip through.
- **Prompt-injection hardening** - company names are sanitized (newlines/null-bytes stripped, length-capped) before entering system prompts.
- **`CompanyType` coercion** - LLMs return `"Public"`, `"publicly traded"`, `42`... anything not a clean enum value crashes the `StrEnum` constructor. `_safe_company_type()` coerces junk -> `UNKNOWN`.

---

## 14. Testing

70 tests, all green, ~2 seconds, fully network-mocked (`respx` for httpx, `patch`/`AsyncMock` elsewhere):
- `test_qa_regressions.py`: 38 tests, one per confirmed bug, named by bug ID.
- `test_excel.py`, `test_resilience.py`, `test_schemas.py`; 32 tests on the core primitives.

Notable test-craft: the Tavily-empty-results test patches both `httpx.AsyncClient` (to return empty results) and the retry-backoff sleep, so a test exercising 3 retries runs instantly instead of taking 30 seconds.

There's also a CLI dry-run (`cli.py`). `python -m app.agent.cli "Apple Inc"` runs the entire pipeline with no web server, no frontend. It was built first, to prove the data flow end-to-end before the FastAPI layer existed.

---

## 15. Future Additions

| Layer | Current | Production upgrade | Why it's better |
|---|---|---|---|
| **LLM** | Gemini 3.1 Flash Lite (free tier) | Claude Opus / GPT-4-class | Better tool-calling reliability, bigger context, native structured outputs — most of the JSON-repair / dedup machinery becomes unnecessary |
| **Web search** | Tavily (free tier) | Tavily paid / Bing API | Higher rate limits, more results, news-specific indexes |
| **Financial data** | yfinance (scrapes Yahoo) | Financial Modeling Prep / Polygon | yfinance breaks when Yahoo changes its page; real APIs have SLAs, private-company coverage, more history |
| **Page extraction** | trafilatura | trafilatura is genuinely good — keep it, maybe add a JS-rendering fallback (Playwright) for SPA-heavy sites | — |
| **Job store** | in-memory dict + LRU | Redis / Postgres | Survives restarts; enables horizontal scaling |
| **Event bus** | in-memory pub/sub | Redis Pub/Sub or a message queue | Lets multiple backend instances share one job's stream |
| **Concurrency** | `Semaphore(1)`, one job at a time | A worker pool / job queue (Celery, RQ, Temporal) | Many concurrent jobs; durable, retryable workflow steps |
| **Embeddings/RAG** | none | A vector DB over evidence | Currently evidence is truncated at 16k chars; embeddings let synthesis retrieve the most relevant pieces |
| **Observability** | structlog to stdout | OpenTelemetry traces + Grafana/Datadog | Per-phase latency, token spend, tool success rates as real dashboards |
| **Deploy** | Docker Compose, single host | Kubernetes + autoscaling | Real autoscaling, rolling deploys, health-managed |

---

## 16. Limitations & Tradeoffs

- **Single-job-at-a-time** — correct for rate-limit budget, but it's a throughput ceiling. Stated tradeoff, not an oversight.
- **In-memory everything** — job store and event bus die on restart. Fine for a take-home; a real deploy needs Redis/Postgres.
- **No auth** — the API is open. Out of scope here; would need API keys + rate limiting per user.
- **Data quality is model-bound** — the LLM will occasionally get qualitative details wrong. Mitigated (not eliminated) by structured yfinance data being extracted directly from tool results and a `data_gaps` field that makes uncertainty explicit. The system is honest about what it doesn't know.
- **Free-tier rate limits** — 15 RPM / 500 RPD on Gemini means the pipeline is sequential and each run takes ~2–3 minutes. Acceptable for a demo; a paid tier or smarter batching would fix it.
- **No semantic evidence ranking** — synthesis truncates evidence at 16k chars rather than retrieving the most relevant pieces.

---
