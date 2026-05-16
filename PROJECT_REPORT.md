# Company Research Agent — Complete Project Report

## 1. The One-Liner

An **agentic AI system** that takes a single company name and autonomously produces a structured research portfolio plus a formatted Excel workbook — financials, competitors, risks, news, the works. Every byte of computation runs **locally**: $0 in API costs, zero external keys, no data leaving the machine. It's built to *look and behave like a production service* — streaming, resilient, observable — not a notebook demo.

**The interview hook:** "I built an autonomous research agent that orchestrates a local LLM through a bounded tool-calling loop, streams its reasoning live over Server-Sent Events, and survives brutal failure conditions — rate limits, model hangs, malformed JSON, SSRF attempts — without ever crashing the user's session."

---

## 2. The Problem & The Constraints

**The task:** turn `"Apple"` → a complete, structured company portfolio + downloadable Excel.

**The self-imposed constraints that shaped every decision:**

| Constraint | Consequence |
|---|---|
| **$0 cost** — no paid APIs | Local LLM (Ollama), free data sources (yfinance, DuckDuckGo) |
| **Single machine, single GPU** | One job at a time; semaphore-gated inference |
| **Unreliable components** | Local models hallucinate/hang; DDG rate-limits; pages 404 — *everything* needs a fallback |
| **"Production-leaning"** | Streaming UX, structured logging, circuit breakers, graceful degradation |

The whole engineering story is: **how do you build something trustworthy on top of fundamentally untrustworthy parts** (an 8B local model, a scraping-hostile search engine, arbitrary web pages)?

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
   +- Phase 1 RESEARCH    bounded tool-calling loop (<=12 iterations)
   |     |- get_company_profile(ticker)     -> yfinance
   |     |- get_company_financials(ticker)  -> yfinance
   |     |- web_search(query)               -> DuckDuckGo
   |     |- fetch_page(url)                 -> httpx + trafilatura + LLM summary
   +- Phase 2 SYNTHESIZE  LLM -> strict Portfolio JSON
   +- Phase 3 EXCEL       openpyxl -> 4-sheet workbook + revenue chart
```

**Two HTTP interactions, deliberately decoupled:**
1. `POST /research` returns immediately (HTTP 202) with a `job_id` — the work runs in a background `asyncio` task.
2. `GET /research/{job_id}/stream` is a long-lived SSE connection replaying everything the agent does, live.

This decoupling is what makes reconnection possible: the work isn't tied to the HTTP request that started it.

---

## 4. The 4-Phase Pipeline (the heart of it)

All four phases live in `orchestrator.py`, wrapped in a single `async with asyncio.timeout(1200)` — a **20-minute hard ceiling** so nothing can hang forever.

### Phase 0 — Resolve
The user types `"apple"`. The LLM is asked for strict JSON: official name, stock ticker, public/private classification, exchange. This is the **disambiguation + routing** step — knowing the ticker unlocks the structured yfinance path; knowing it's private routes to web-only research.

### Phase 1 — Research (the agentic loop)
A real **tool-calling agent loop** — covered in depth in section 6. The LLM is handed 4 tools and reasons step-by-step: "I have the ticker, fetch financials -> now I need headcount, search the web -> now competitors..." until it emits the literal stop-token `RESEARCH_COMPLETE`.

### Phase 2 — Synthesize
All accumulated evidence is compacted and handed back to the LLM with an **explicit JSON schema** (every field name spelled out). The model turns messy raw evidence into one clean `Portfolio` object.

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
| **httpx** | Async HTTP | `AsyncClient` + streaming responses — needed to stream tokens from Ollama |
| **structlog** | Logging | Structured key-value logs — greppable, machine-parseable |
| **yfinance** | Financial data | **Free**, no API key — public company financials |
| **duckduckgo-search (`ddgs`)** | Web search | **Free**, no API key |
| **trafilatura** | HTML->text extraction | Best-in-class boilerplate removal — strips nav/ads, keeps article body |
| **openpyxl** | Excel generation | The standard for `.xlsx` with styling + charts |
| **pytest + pytest-asyncio + respx** | Testing | `respx` mocks httpx so tests never touch the network |

**The LLM stack:** **Ollama** runtime serving two open-weight models:
- **`qwen3:8b`** — primary orchestrator. Best open-weight *tool-caller* at ~5GB. Critically, qwen3 has a "thinking mode" that we explicitly **disable** (`"think": false`) — left on, it burns 60–120s of inference on hidden reasoning before producing output, blowing every timeout.
- **`llama3.1:8b`** — fallback. Deliberately a **different model family** so a failure mode specific to qwen3 doesn't take down the fallback too. Failure *diversity*, not just redundancy.

### Frontend (TypeScript)

| Tech | Role | Why |
|---|---|---|
| **React 18** | UI | Component model fits the live event-stream UI |
| **Vite 5** | Build/dev server | Instant HMR; dev-proxy forwards `/api` -> backend (no CORS pain) |
| **TypeScript** | Type safety | Frontend `types/index.ts` mirrors backend Pydantic models — one schema, two languages |
| **Native `EventSource`** | SSE client | Browser-built-in; **auto-reconnects** and sends `Last-Event-ID` for free — no library needed |
| **Plain CSS** | Styling | No Tailwind/CSS-in-JS — keeps the dependency tree tiny |

### Infrastructure
- **Docker Compose** — 3 services (Ollama + backend + frontend), one `docker compose up`. Ollama service reserves the NVIDIA GPU.
- **Multi-stage frontend Dockerfile** — Node builds, output served by **nginx** (tiny final image). nginx also reverse-proxies `/api` with `proxy_buffering off` — **mandatory for SSE** (buffering would batch the live stream into one dump).

---

## 6. The Agentic Research Loop — Deep Dive

This is `loop.py`, and it's the most interesting code in the project. A naive tool-calling loop on a small local model **will** misbehave: infinite loops, repeated identical calls, fetching 50 pages. This loop has **five independent guardrails**:

1. **`MAX_ITERATIONS = 12`** — hard cap on reasoning rounds. The `for...else` clause emits a warning if the cap is hit without a clean finish.

2. **Explicit stop signal** — the model must emit the literal string `RESEARCH_COMPLETE`. No guessing whether it's "done."

3. **Exact-duplicate dedup** — `(tool_name, sorted_args_json)` is hashed into a `seen_calls` set. Small models *love* to re-run the identical search; the duplicate is rejected and the model is told so via a synthetic tool-error message (so it adjusts instead of silently looping).

4. **Per-tool call caps** — `web_search`: 6, `fetch_page`: 3, profile/financials: 2 each. Stops the "fetch-all" trap and semantic oscillation (slightly-reworded-but-equivalent searches).

5. **Globally unique tool-call IDs** — `call_0, call_1, ...` assigned across *all* iterations. Ollama's message protocol pairs `tool_call_id`s; iteration-local IDs collide and corrupt the conversation history.

Each tool result becomes an **evidence dict** `{tool, args, result}`. Tool failures don't crash the loop — the exception is caught, serialized as `{"error": ...}`, and fed back so the model can react.

**Why this matters in an interview:** it shows you understand that agentic systems aren't "call the LLM in a while-loop" — they need *bounded, observable, self-correcting* control flow.

---

## 7. The LLM Layer — `ollama_client.py`

A hand-rolled async Ollama client. Three call modes:
- **`chat()`** — non-streaming, for structured JSON synthesis.
- **`stream_chat()`** — streaming, yields either `str` tokens or `ToolCall` objects, for the research loop.
- **`generate_short()`** — single-prompt, capped at 700 tokens, for per-page summarization.

**The hard-won details:**

- **Single global inference semaphore** (`Semaphore(1)`) — one GPU can't do two inferences at once. Controlled backpressure beats Ollama silently queuing requests until SSE streams time out.

- **Model fallback** — if the primary errors *on the first chunk*, transparently retry on llama3.1. First-chunk-only: once tokens are flowing, a mid-stream failure can't be cleanly restarted.

- **Two-layer timeout defense:**
  - `_CHAT_TIMEOUT = 120s` — total wall-clock per call.
  - `_IDLE_TIMEOUT = 45s` — **per-line idle timeout**. This was a real bug fix: if Ollama accepts the TCP connection but sends *zero bytes* (a hung model), a total-timeout still makes you wait the full 120s. The idle timeout wraps each `__anext__()` on the line iterator in `asyncio.wait_for` — declares a hang after 45s of silence.

- **qwen3 thinking disabled** — `_model_options()` injects `"think": false` for any qwen3 model, applied to all three call paths.

---

## 8. The Tools Layer

Tools are registered in a `registry.py` `ToolRegistry` that also generates the JSON schemas Ollama needs. Four tools:

**`get_company_profile` / `get_company_financials`** (`financials.py`) — yfinance. yfinance is *synchronous* and blocking, so every call is wrapped in `asyncio.to_thread()` to avoid stalling the event loop. Returns up to 4 years of income statements. Empty dict from Yahoo (blocked user-agent / bad ticker) is handled, not crashed.

**`web_search`** (`web_search.py`) — DuckDuckGo. DDG is *aggressively* anti-bot, so: **mandatory 1.5–4s randomized jitter before every call** (not just on retry — proactively staying under the radar). Key subtlety: an **empty result list is treated as a soft rate-limit, not "no results exist"** — it raises `RuntimeError` so retry + circuit breaker engage. Silently returning `[]` would waste the agent's whole budget chasing nothing.

**`fetch_page`** (`fetch_page.py`) — the cleverest tool. Fetch URL -> `trafilatura` extracts clean article text -> **the local LLM summarizes it to ~150 words** before returning. This is *the* mitigation for the **context-window trap**: 3–4 raw web pages easily blow past 15k tokens and the model loses coherence; summaries stay ~600 tokens each. The agent researches on *summaries*, not raw HTML.

---

## 9. Resilience Engineering — the part that makes it "production-leaning"

Three composable primitives in `resilience/`, each ~30 lines:

- **`retry_async`** — exponential backoff (`base * 2^n`) + jitter, capped delay. Has a `non_retriable` tuple so 4xx client errors (a 404 page) skip retries — retrying a 404 is pure waste.

- **`CircuitBreaker`** — per-tool. Opens after N consecutive failures (yfinance: 3, DDG: 4), rejects calls for a recovery window, then **half-opens** to probe. Prevents hammering a service that's already down.

- **`with_timeout`** — `asyncio.wait_for` wrapper with a labeled `TimeoutError`.

They **compose**: `CircuitBreaker.call( retry_async( with_timeout(actual_fn) ) )`. Timeout is innermost, retry wraps it, breaker wraps that.

**The complete defense matrix:**

| Failure mode | Mitigation |
|---|---|
| Transient network blip | Retry w/ exponential backoff + jitter |
| Service persistently down | Circuit breaker (open -> half-open -> closed) |
| LLM call hangs | 120s total + 45s idle timeout |
| Whole pipeline hangs | 20-min `asyncio.timeout` ceiling |
| Context window overflow | Per-page LLM summarization (~600 tok vs ~5k raw) |
| DDG rate limiting | Proactive 1.5–4s jitter; empty = retryable error |
| Malformed LLM JSON | JSON-repair re-prompt loop (<=2 attempts) |
| Agent stuck re-calling a tool | `(tool, args)` dedup hash |
| Agent over-using a tool | Per-tool call caps |
| Private company (no yfinance) | public/private classification -> web-only fallback |
| Concurrent requests | `Semaphore(1)` -> second request gets `429 + active_job_id` |
| SSE generator throws mid-stream | `try/except` -> guaranteed terminal `error` event |
| Excel file locked (Windows) | Versioned filename fallback `_v2`, `_v3` |
| Connection drops | `Last-Event-ID` replay from in-memory buffer |

**JSON-repair loop:** local models emit broken JSON constantly — trailing commas, markdown fences, truncation. `_llm_json()` strips fences, tries `json.loads`, and on failure **re-prompts the model with the parse error and the bad output**, asking it to fix itself. Up to 2 repair attempts before degrading gracefully.

---

## 10. The Streaming Architecture — SSE + Event Bus

**Why SSE over WebSockets:** the data flow is one-directional (server->client). SSE is plain HTTP, the browser's `EventSource` **auto-reconnects and resends `Last-Event-ID` for free**, and there's no handshake overhead. WebSockets would be over-engineering.

**The `JobEventBus`** (`bus.py`) is an in-memory pub/sub:
- Every event is **buffered** in a per-job list *and* fanned out to live subscriber queues.
- **Reconnection replay:** subscribe with `last_event_id` -> replays `events[last_event_id + 1:]`. The `+1` is the off-by-one fix — `last_event_id` is the seq already *received*, so replay must *exclude* it. Fresh connections pass `-1` -> replay everything.
- **Memory management:** a `_closed` set marks finished jobs. When the last subscriber of a closed job disconnects, `_cleanup()` frees the buffer. Without this, every completed job leaks its event list forever.
- **Heartbeats** every 15s keep the connection alive through proxies.

**10 typed event types** (`events.py`): `phase_start`, `tool_call`, `tool_result`, `token`, `warning`, `portfolio_section`, `done`, `error`, `heartbeat`, `plan`. Every event is a Pydantic `AgentEvent` with `type`, `job_id`, `seq`, `data`.

**The SSE endpoint's safety net:** the generator is wrapped in `try/except`. Once HTTP 200 is committed you *cannot* send a 500 — so on any mid-stream exception it emits a terminal `error` event. The frontend is **never left hanging**.

---

## 11. Excel Generation — `builder.py`

Four sheets: **Overview** (company facts + key products + data gaps), **Income Statement** (multi-year + native bar chart), **Key Metrics** (+ risks/opportunities), **Competitors**.

The defensive theme: **a single `None` passed to an openpyxl chart crashes the whole phase.** So:
- `_safe()` normalizes `None`/`NaN`/`inf` -> `"N/A"`.
- `_millions()` / `_pct()` format numbers human-readably (`$2.95B`, `24.3%`).
- The revenue chart only renders if **>=2 valid numeric data points** exist.
- **Windows file-lock fallback** — if `job.xlsx` is open in Excel, save retries as `_v2`, `_v3` instead of crashing.

---

## 12. The Frontend

A focused React app — one screen, live updates.

- **`useResearchStream`** hook (`useResearchStream.ts`) — owns all SSE state. POSTs the job, opens `EventSource`, accumulates events, derives `status`. A `submittingRef` guards against **rage-clicks** firing duplicate POSTs before React re-renders — and it's reset in a `finally` so error paths can't leak the lock.
- **`PhaseStepper`** — the 4-step progress bar (Resolve->Research->Synthesize->Export) with pending/active/done/error states and animated connectors.
- **`EventTimeline`** — the live agent feed. Tool calls and results are paired **positionally** (n-th result <-> n-th call) and rendered as `ToolCallCard`s with spinners -> durations -> result previews.
- **`PortfolioView`** — the final structured report + Excel download button.
- **`SearchForm`** — input + a "disable web search" toggle (financial-APIs-only fast path).

TypeScript interfaces in `types/index.ts` **mirror the backend Pydantic models** exactly — the contract is enforced on both ends.

---

## 13. The QA / Security Hardening Round

A dedicated adversarial QA pass found **15 bugs + 3 security issues**, all fixed, all with regression tests. Highlights worth naming in an interview:

- **SSRF prevention** — the LLM controls `fetch_page` URLs. A malicious/confused model could hit `http://localhost:11434` or `http://169.254.169.254/` (cloud metadata!). `_validate_url()` enforces an http/https allowlist and blocks private/loopback/link-local/reserved IPs via the `ipaddress` module.
- **Excel formula injection** — a company named `=HYPERLINK("evil.com")` would execute when the `.xlsx` is opened. `_safe_str()` space-prefixes any cell starting with `= + - @` or control chars -> Excel treats it as inert text.
- **HTTP header injection** — the download filename is derived from the company name; CRLF in it could inject headers. `_safe_download_filename()` strips everything non-alphanumeric.
- **TOCTOU race** — two fast requests could both pass the "is a job running?" check. Fixed by acquiring the semaphore **synchronously in the request handler** — no `await` between check and acquire, so no context switch can slip through.
- **Prompt-injection hardening** — company names are sanitized (newlines/null-bytes stripped, length-capped) before entering system prompts.
- **`CompanyType` coercion** — LLMs return `"Public"`, `"publicly traded"`, `42`... anything not a clean enum value crashes the `StrEnum` constructor. `_safe_company_type()` coerces junk -> `UNKNOWN`.

---

## 14. Testing

**70 tests, all green, ~2 seconds, fully network-mocked** (`respx` for httpx, `patch`/`AsyncMock` elsewhere):
- `test_qa_regressions.py` — 38 tests, one per confirmed bug, named by bug ID.
- `test_excel.py`, `test_resilience.py`, `test_schemas.py` — 32 tests on the core primitives.

Notable test-craft: the DDG-empty-results test patches **both** the pre-call jitter sleep *and* the retry-backoff sleep, so a test exercising 3 retries runs instantly instead of taking 30 seconds.

There's also a **CLI dry-run** (`cli.py`) — `python -m app.agent.cli "Apple Inc"` runs the entire pipeline with no web server, no frontend. It was built *first*, to prove the data flow end-to-end before the FastAPI layer existed.

---

## 15. What We'd Use Without the $0 Constraint

This is a strong interview question — here's the honest answer for each layer:

| Layer | Built (free) | Would use with budget | Why it's better |
|---|---|---|---|
| **LLM** | Ollama qwen3:8b / llama3.1:8b local | **Claude (Opus/Sonnet)** or GPT-4-class API | Vastly better tool-calling reliability, native structured outputs, bigger context — most of the JSON-repair / dedup / cap machinery becomes unnecessary |
| **Web search** | DuckDuckGo scraping | **Tavily / Brave / Serper / Bing API** | Built for programmatic use — no rate-limit jitter games, ranked results, no soft-bans |
| **Financial data** | yfinance (scrapes Yahoo) | **Financial Modeling Prep / Alpha Vantage / Polygon** | yfinance breaks when Yahoo changes its page; real APIs have SLAs, private-company coverage, more history |
| **Page extraction** | trafilatura | trafilatura is genuinely good — **keep it**, maybe add a JS-rendering fallback (Playwright) for SPA-heavy sites |
| **Job store** | in-memory dict + LRU | **Redis / Postgres** | Survives restarts; enables horizontal scaling |
| **Event bus** | in-memory pub/sub | **Redis Pub/Sub or a message queue** | Lets multiple backend instances share one job's stream |
| **Concurrency** | `Semaphore(1)`, one job at a time | **A GPU worker pool / job queue (Celery, RQ, Temporal)** | Many concurrent jobs; durable, retryable workflow steps |
| **Embeddings/RAG** | none | **A vector DB over evidence** | Currently evidence is just truncated; embeddings would let synthesis retrieve the *most relevant* evidence instead of the *first 16k chars* |
| **Observability** | structlog to stdout | **OpenTelemetry traces + Grafana/Datadog** | Per-phase latency, token spend, tool success rates as real dashboards |
| **Deploy** | Docker Compose, single host | **Kubernetes** + GPU node pool | Real autoscaling, rolling deploys, health-managed |

**The honest framing:** the free constraint didn't make the project *worse* — it made it *harder and more interesting*. Most of the engineering depth (retry/breaker/timeout layering, JSON repair, dedup, context-window summarization, idle-timeout hang detection) **exists precisely because the components are unreliable**. With Claude's API, the agent loop would be 40 lines. With Ollama, it's a genuine distributed-systems-in-miniature problem. That's the story to tell.

---

## 16. Honest Limitations & Tradeoffs

Naming these *unprompted* in an interview signals senior judgment:

- **Single-job-at-a-time** — correct for one GPU, but it's a throughput ceiling. Stated tradeoff, not an oversight.
- **In-memory everything** — job store and event bus die on restart. Fine for a take-home; a real deploy needs Redis/Postgres.
- **No auth** — the API is open. Out of scope here; would need API keys + rate limiting.
- **Data quality is model-bound** — an 8B local model *will* occasionally get a number wrong. Mitigated (not eliminated) by structured yfinance data being preferred and a `data_gaps` field that makes uncertainty explicit. The system is honest about what it doesn't know.
- **DDG scraping is fragile by nature** — the jitter helps, but a real product needs a real search API.
- **No semantic evidence ranking** — synthesis truncates evidence at 16k chars rather than retrieving the most relevant pieces.

---

## 17. Likely Interview Questions — Prepared Answers

**"Why an agent loop instead of a fixed script?"**
Different companies need different research paths — a public company routes through yfinance; a private one is web-only; some need 2 searches, some need 6. A fixed script either over-fetches or misses data. The agent adapts per company. The *bounds* (iteration cap, dedup, per-tool caps) keep that adaptability from becoming chaos.

**"How do you stop it looping forever?"**
Five independent guardrails: a 12-iteration hard cap, an explicit `RESEARCH_COMPLETE` stop token, `(tool,args)` dedup, per-tool call caps, and a 20-minute pipeline timeout above it all. Defense in depth — no single mechanism is trusted alone.

**"How does the context window not overflow?"**
`fetch_page` never returns raw HTML — it returns a ~150-word LLM summary. Raw pages are ~5k tokens each; summaries ~600. The agent reasons over summaries; synthesis gets compacted evidence capped at 16k chars.

**"Why SSE, not WebSockets?"**
One-directional data, plain HTTP, and `EventSource` gives auto-reconnect + `Last-Event-ID` replay for free. WebSockets would add a bidirectional protocol I don't need.

**"What happens when a tool fails?"**
It composes through three layers: timeout -> retry-with-backoff -> circuit breaker. If it still fails, the exception is caught, serialized as an error, and fed back to the agent so it can adapt. One tool failing never crashes the job.

**"Walk me through a request."**
`POST /research` -> 202 + job_id, background task spawned -> browser opens SSE stream -> 4 phases run, each emitting events through the bus -> events stream live to the timeline UI -> `done` event -> portfolio renders, Excel downloadable. If the connection drops, `EventSource` reconnects with `Last-Event-ID` and replays exactly the missed events.

**"What was the hardest bug?"**
The zero-byte Ollama hang. A total 120s timeout still makes the user wait the full two minutes for a model that connected but produced nothing. The fix was a *per-line idle timeout* — wrap each `__anext__()` on the streaming line iterator in a 45s `wait_for`, so silence is detected as a hang in 45s, not 120s.

---

## TL;DR

This isn't "I called an LLM API." It's an autonomous agent with a bounded, self-correcting control loop, a three-layer resilience stack, live streaming with reconnection-replay, and a security-hardened boundary — built deliberately on *unreliable free components* so that every piece of resilience engineering had to be real. 70 passing tests, runs at $0, one `docker compose up`.
