"""
4-phase pipeline: Resolve → Research → Synthesize → Excel
Each phase emits structured events to the bus for live UI visibility.
"""
import asyncio
import json
from typing import Any

from pydantic import ValidationError

from app.agent.loop import research_loop
from app.agent.prompts import (
    RESOLVE_SYSTEM, RESOLVE_USER,
    SYNTHESIZE_SYSTEM, SYNTHESIZE_USER,
    JSON_REPAIR_SYSTEM, JSON_REPAIR_USER,
)
from app.agent.schemas import (
    CompanyType, Financials, FinancialYear, Portfolio,
    ResolvedCompany, ResearchJob, Competitor,
    Evidence, Confidence,
)
from app.excel.builder import build_excel
from app.llm.ollama_client import OllamaClient
from app.observability.logging import get_logger
from app.streaming.bus import JobEventBus
from app.streaming.events import (
    phase_start, portfolio_section_event, done_event, error_event, warning_event,
)
from app.tools.fetch_page import fetch_page
from app.tools.financials import get_company_financials, get_company_profile
from app.tools.registry import Tool, ToolRegistry
from app.tools.web_search import web_search

log = get_logger(__name__)

_MAX_REPAIR_ATTEMPTS = 2
_PIPELINE_TIMEOUT = 1200.0  # 20-minute hard cap — prevents infinite hangs


def _build_registry(llm: OllamaClient, disable_web_search: bool = False) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        name="get_company_profile",
        description="Fetch company profile from Yahoo Finance: sector, employees, description.",
        parameters={
            "type": "object",
            "properties": {"ticker": {"type": "string", "description": "Stock ticker symbol"}},
            "required": ["ticker"],
        },
        fn=get_company_profile,
    ))
    registry.register(Tool(
        name="get_company_financials",
        description="Fetch financial data from Yahoo Finance: revenue, market cap, margins, income statement.",
        parameters={
            "type": "object",
            "properties": {"ticker": {"type": "string", "description": "Stock ticker symbol"}},
            "required": ["ticker"],
        },
        fn=get_company_financials,
    ))
    if not disable_web_search:
        registry.register(Tool(
            name="web_search",
            description="Search the web via Tavily. Use specific queries including company name.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "default": 6},
                },
                "required": ["query"],
            },
            fn=web_search,
        ))
        registry.register(Tool(
            name="fetch_page",
            description="Fetch and summarize a web page. Returns a 150-word summary of the content.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL to fetch"}},
                "required": ["url"],
            },
            fn=lambda url: fetch_page(url, llm),
        ))
    return registry


def _seq(bus: JobEventBus, job_id: str) -> int:
    return bus.next_seq(job_id)


async def _llm_json(
    llm: OllamaClient,
    bus: JobEventBus,
    job_id: str,
    messages: list[dict[str, Any]],
    repair_system: str,
    repair_user_template: str,
) -> dict[str, Any] | None:
    """Call LLM, parse JSON, repair up to _MAX_REPAIR_ATTEMPTS times on failure."""
    response = await llm.chat(messages, temperature=0.0)
    raw = response.content.strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]

    for attempt in range(_MAX_REPAIR_ATTEMPTS + 1):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            if attempt == _MAX_REPAIR_ATTEMPTS:
                bus.publish(warning_event(job_id, _seq(bus, job_id),
                                          f"JSON repair exhausted: {exc}"))
                return None
            bus.publish(warning_event(job_id, _seq(bus, job_id),
                                      f"JSON repair attempt {attempt + 1}: {exc}"))
            repair_msgs = [
                {"role": "system", "content": repair_system},
                {"role": "user", "content": repair_user_template.format(
                    error=str(exc), bad_json=raw
                )},
            ]
            resp = await llm.chat(repair_msgs, temperature=0.0)
            raw = resp.content.strip()
    return None


def _safe_company_type(raw: Any) -> CompanyType:
    """Coerce LLM output to a valid CompanyType — never crash on unexpected strings."""
    if not isinstance(raw, str):
        return CompanyType.UNKNOWN
    try:
        return CompanyType(raw.strip().lower())
    except ValueError:
        return CompanyType.UNKNOWN


async def _verify_ticker(ticker: str, expected_name: str) -> str | None:
    """Return the ticker if yfinance confirms it belongs to expected_name, else None.
    Prevents the LLM confusing company names with unrelated ticker symbols
    (e.g. 'Samsung' → ticker 'SM' which is SM Energy, not Samsung Electronics)."""
    import yfinance as yf
    try:
        info = await asyncio.to_thread(lambda: yf.Ticker(ticker).info or {})
        yf_name = (info.get("longName") or info.get("shortName") or "").lower()
        if not yf_name:
            return ticker  # yfinance returned nothing — keep ticker, let research decide
        # Check if any word from the user's company name appears in the yfinance name
        expected_words = {w for w in expected_name.lower().split() if len(w) > 2}
        if expected_words & set(yf_name.split()):
            return ticker
        log.warning("ticker_mismatch_nulled", ticker=ticker,
                    expected=expected_name, yfinance_name=yf_name)
        return None  # Ticker belongs to a different company — discard it
    except Exception:
        return ticker  # Don't block the pipeline on a lookup failure


def _update_company_from_evidence(
    company: ResolvedCompany, evidence: list[dict[str, Any]]
) -> ResolvedCompany:
    """Back-fill company profile fields directly from the get_company_profile tool result."""
    for item in evidence:
        if item.get("tool") != "get_company_profile":
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        for field in ("sector", "industry", "country", "website", "description", "founded", "headquarters"):
            if getattr(company, field) is None and result.get(field):
                setattr(company, field, str(result[field]))
        if company.exchange is None and result.get("exchange"):
            company.exchange = str(result["exchange"])
        break
    return company


def _financials_from_evidence(evidence: list[dict[str, Any]]) -> Financials | None:
    """Build Financials directly from the get_company_financials tool result.
    Bypasses the synthesis LLM to avoid unit-conversion errors (raw USD from yfinance)."""
    for item in evidence:
        if item.get("tool") != "get_company_financials":
            continue
        result = item.get("result")
        if not isinstance(result, dict) or not result:
            continue
        fins = Financials()
        for field in ("market_cap", "enterprise_value", "pe_ratio", "revenue_ttm",
                      "gross_margin", "net_margin", "revenue_growth_yoy"):
            f = _safe_float(result.get(field))
            if f is not None:
                setattr(fins, field, f)
        annual = []
        for yr in result.get("annual") or []:
            try:
                annual.append(FinancialYear(**{
                    k: _safe_float(v) if k != "year" else int(v)
                    for k, v in yr.items()
                }))
            except Exception:
                pass
        fins.annual = annual
        fins.currency = result.get("currency", "USD")
        fins.data_source = "yfinance"
        return fins
    return None


def _headcount_from_evidence(evidence: list[dict[str, Any]]) -> int | None:
    """Extract employee count directly from get_company_profile tool result."""
    for item in evidence:
        if item.get("tool") == "get_company_profile":
            result = item.get("result")
            if isinstance(result, dict):
                v = result.get("employees")
                if v is not None:
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        pass
    return None


def _compact_evidence(evidence: list[dict[str, Any]]) -> str:
    """Serialize evidence, truncating individual large results to avoid context overflow."""
    compact = []
    for item in evidence:
        e = dict(item)
        result = e.get("result")
        if result is not None:
            s = json.dumps(result, default=str)
            if len(s) > 1500:
                e["result"] = s[:1500] + "…[truncated]"
        compact.append(e)
    return json.dumps(compact, default=str)


def _sanitize_company_name(raw: str) -> str:
    # Strip newlines and null bytes to prevent prompt injection via system message formatting.
    # Max 120 chars — well under the Pydantic 200 limit but enough for any real company name.
    return raw.replace("\n", " ").replace("\r", " ").replace("\x00", "")[:120].strip()


async def run_pipeline(job: ResearchJob, bus: JobEventBus) -> None:
    """
    Full research pipeline. Publishes events throughout.
    Handles its own exceptions — errors surface as error events, not crashes.
    """
    job_id = job.job_id
    safe_name = _sanitize_company_name(job.company_name)
    llm = OllamaClient()

    try:
        async with asyncio.timeout(_PIPELINE_TIMEOUT):
            # ── Phase 0: Resolve ──────────────────────────────────────────────────
            bus.publish(phase_start(job_id, _seq(bus, job_id), "resolve",
                                    f"Identifying '{safe_name}'"))
            resolve_data = await _llm_json(
                llm, bus, job_id,
                messages=[
                    {"role": "system", "content": RESOLVE_SYSTEM},
                    {"role": "user", "content": RESOLVE_USER.format(company_name=safe_name)},
                ],
                repair_system="Fix this JSON.",
                repair_user_template=JSON_REPAIR_USER,
            )
            if resolve_data is None:
                bus.publish(error_event(job_id, _seq(bus, job_id),
                                        "Failed to identify company", phase="resolve"))
                return

            resolved_ticker = resolve_data.get("ticker")
            resolved_name = resolve_data.get("name", job.company_name)

            # Sanity-check: if the LLM gave a ticker, verify yfinance agrees it belongs
            # to this company (guards against e.g. "Samsung" → ticker "SM" = SM Energy).
            if resolved_ticker:
                resolved_ticker = await _verify_ticker(resolved_ticker, resolved_name)

            company = ResolvedCompany(
                name=resolved_name,
                ticker=resolved_ticker,
                type=_safe_company_type(resolve_data.get("type")),
                exchange=resolve_data.get("exchange"),
            )
            bus.publish(portfolio_section_event(job_id, _seq(bus, job_id), "company",
                                                company.model_dump()))
            log.info("resolved", job_id=job_id, company=company.name, ticker=company.ticker)

            # ── Phase 1: Research ─────────────────────────────────────────────────
            bus.publish(phase_start(job_id, _seq(bus, job_id), "research",
                                    "Gathering data via tools"))
            registry = _build_registry(llm, disable_web_search=job.disable_web_search)
            evidence = await research_loop(company, llm, registry, bus, job_id)

            # Extract structured fields directly from tool results — no LLM unit conversion.
            company = _update_company_from_evidence(company, evidence)
            prefetched_fins = _financials_from_evidence(evidence)
            prefetched_headcount = _headcount_from_evidence(evidence)

            # ── Phase 2: Synthesize ───────────────────────────────────────────────
            bus.publish(phase_start(job_id, _seq(bus, job_id), "synthesize",
                                    "Synthesizing portfolio from evidence"))
            evidence_json = _compact_evidence(evidence)
            portfolio_data = await _llm_json(
                llm, bus, job_id,
                messages=[
                    {"role": "system", "content": SYNTHESIZE_SYSTEM},
                    {"role": "user", "content": SYNTHESIZE_USER.format(
                        company_name=company.name,
                        ticker=company.ticker or "N/A",
                        company_type=company.type,
                        evidence_json=evidence_json[:16000],
                    )},
                ],
                repair_system=JSON_REPAIR_SYSTEM,
                repair_user_template=JSON_REPAIR_USER,
            )

            portfolio = _build_portfolio(
                company, portfolio_data or {},
                prefetched_fins=prefetched_fins,
                prefetched_headcount=prefetched_headcount,
            )
            bus.publish(portfolio_section_event(job_id, _seq(bus, job_id), "portfolio",
                                                portfolio.model_dump()))

            # ── Phase 3: Excel ────────────────────────────────────────────────────
            bus.publish(phase_start(job_id, _seq(bus, job_id), "excel",
                                    "Generating Excel workbook"))
            excel_path = build_excel(portfolio, job_id)

            from app.jobs.store import job_store
            job_store.update(job_id, status="done", portfolio=portfolio, excel_ready=True)
            bus.publish(done_event(job_id, _seq(bus, job_id)))
            log.info("pipeline_done", job_id=job_id, excel=excel_path)

    except TimeoutError:
        log.error("pipeline_timeout", job_id=job_id)
        bus.publish(error_event(job_id, _seq(bus, job_id),
                                f"Pipeline timed out after {int(_PIPELINE_TIMEOUT / 60)} minutes"))
        from app.jobs.store import job_store
        job_store.update(job_id, status="error", error="timeout")
    except Exception as exc:
        log.exception("pipeline_error", job_id=job_id, error=str(exc))
        bus.publish(error_event(job_id, _seq(bus, job_id), str(exc)))
        from app.jobs.store import job_store
        job_store.update(job_id, status="error", error=str(exc))
    finally:
        bus.close_job(job_id)
        await llm.aclose()


def _build_portfolio(
    company: ResolvedCompany,
    data: dict[str, Any],
    prefetched_fins: Financials | None = None,
    prefetched_headcount: int | None = None,
) -> Portfolio:
    """Map raw LLM synthesis dict → validated Portfolio, tolerating missing fields.

    Structured financial data is taken from prefetched_fins (direct from yfinance)
    when available, avoiding LLM unit-conversion errors. Qualitative fields come
    from the LLM synthesis dict.
    """
    # Financials: prefer direct yfinance extraction over LLM synthesis
    if prefetched_fins is not None:
        financials = prefetched_fins
    else:
        financials = Financials()
        fin_data = data.get("financials", {}) or {}
        for field in ("market_cap", "enterprise_value", "pe_ratio", "revenue_ttm",
                      "gross_margin", "net_margin", "revenue_growth_yoy"):
            v = fin_data.get(field)
            if v is not None:
                try:
                    setattr(financials, field, float(v))
                except (TypeError, ValueError):
                    pass
        annual = []
        for yr in (fin_data.get("annual") or []):
            try:
                annual.append(FinancialYear(**{k: _safe_float(v) if k != "year" else int(v)
                                               for k, v in yr.items()}))
            except Exception:
                pass
        financials.annual = annual

    competitors = []
    for c in (data.get("competitors") or [])[:5]:
        try:
            competitors.append(Competitor(
                name=c.get("name", ""),
                ticker=c.get("ticker"),
                market_cap=_safe_float(c.get("market_cap")),
                revenue_ttm=_safe_float(c.get("revenue_ttm")),
                summary=c.get("summary"),
            ))
        except Exception:
            pass

    # Headcount: prefer direct yfinance value (high confidence) over LLM synthesis
    headcount_value = prefetched_headcount if prefetched_headcount is not None else data.get("headcount")
    headcount_source = "yfinance" if prefetched_headcount is not None else "synthesis"
    headcount_confidence = Confidence.HIGH if prefetched_headcount is not None else Confidence.MEDIUM
    headcount = Evidence(
        value=headcount_value,
        source=headcount_source,
        confidence=headcount_confidence,
    ) if headcount_value is not None else None

    market_pos_raw = data.get("market_position")
    market_position = Evidence(
        value=market_pos_raw,
        source="synthesis",
        confidence=Confidence.MEDIUM,
    ) if market_pos_raw is not None else None

    return Portfolio(
        company=company,
        headcount=headcount,
        financials=financials,
        market_position=market_position,
        key_products=_coerce_str_list(data.get("key_products")),
        competitors=competitors,
        recent_news=_coerce_str_list(data.get("recent_news")),
        risks=_coerce_str_list(data.get("risks")),
        opportunities=_coerce_str_list(data.get("opportunities")),
        analyst_summary=data.get("analyst_summary"),
        data_gaps=_coerce_str_list(data.get("data_gaps")),
    )


def _coerce_str_list(raw: Any) -> list[str]:
    """Normalize a list that the LLM may have returned as strings, dicts, or mixed."""
    if not raw:
        return []
    result = []
    for item in raw:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            # e.g. {"field": "founded", "confidence_level": "low"} → "founded (low confidence)"
            field = item.get("field") or item.get("name") or item.get("gap") or str(item)
            confidence = item.get("confidence_level") or item.get("confidence")
            result.append(f"{field} ({confidence} confidence)" if confidence else str(field))
        else:
            result.append(str(item))
    return result


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if f != f else f  # NaN check
    except (TypeError, ValueError):
        return None
