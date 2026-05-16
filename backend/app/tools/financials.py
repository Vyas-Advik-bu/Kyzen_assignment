"""
yfinance-backed tools — $0 cost, no API key.
Covers publicly traded companies only; callers must handle the None case.
"""
import asyncio
from typing import Any

import yfinance as yf

from app.observability.logging import get_logger
from app.resilience.circuit_breaker import CircuitBreaker
from app.resilience.retry import retry_async

log = get_logger(__name__)

_cb = CircuitBreaker(name="yfinance", failure_threshold=3, recovery_timeout=120.0)


def _fetch_info(ticker: str) -> dict[str, Any]:
    t = yf.Ticker(ticker)
    return t.info or {}


def _fetch_financials(ticker: str) -> dict[str, Any]:
    t = yf.Ticker(ticker)
    result: dict[str, Any] = {}

    try:
        inc = t.income_stmt
        if inc is not None and not inc.empty:
            rows = []
            for col in inc.columns[:4]:  # up to 4 years
                year = col.year if hasattr(col, "year") else int(str(col)[:4])
                def _get(field: str) -> float | None:
                    try:
                        v = inc.loc[field, col]
                        return None if v != v else float(v)  # NaN check
                    except KeyError:
                        return None
                rows.append({
                    "year": year,
                    "revenue": _get("Total Revenue"),
                    "gross_profit": _get("Gross Profit"),
                    "operating_income": _get("Operating Income"),
                    "net_income": _get("Net Income"),
                    "ebitda": _get("EBITDA"),
                })
            result["annual"] = rows
    except Exception as exc:
        log.warning("financials_income_stmt_error", ticker=ticker, error=str(exc))

    return result


async def get_company_profile(ticker: str) -> dict[str, Any]:
    """Return company info dict from yfinance (sector, employees, description, etc.)."""
    async def _call() -> dict[str, Any]:
        return await asyncio.to_thread(_fetch_info, ticker)

    try:
        info = await _cb.call(lambda: retry_async(_call, max_attempts=2, label=f"yf.info/{ticker}"))
        if not info:
            # Yahoo Finance returned an empty dict — likely a blocked user-agent or invalid ticker
            log.warning("yfinance_empty_response", ticker=ticker)
            return {}
        return {
            "name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "country": info.get("country"),
            "website": info.get("website"),
            "description": (info.get("longBusinessSummary") or "")[:1200],
            "employees": info.get("fullTimeEmployees"),
            "founded": info.get("foundingDate"),
            "headquarters": info.get("city"),
            "exchange": info.get("exchange"),
        }
    except Exception as exc:
        log.warning("get_company_profile_failed", ticker=ticker, error=str(exc))
        return {}


async def get_company_financials(ticker: str) -> dict[str, Any]:
    """Return structured financial data: market cap, margins, annual income statements."""
    async def _call_info() -> dict[str, Any]:
        return await asyncio.to_thread(_fetch_info, ticker)

    async def _call_fins() -> dict[str, Any]:
        return await asyncio.to_thread(_fetch_financials, ticker)

    try:
        info, fins = await asyncio.gather(
            _cb.call(lambda: retry_async(_call_info, max_attempts=2, label=f"yf.info2/{ticker}")),
            _cb.call(lambda: retry_async(_call_fins, max_attempts=2, label=f"yf.fins/{ticker}")),
        )
        return {
            "market_cap": info.get("marketCap"),
            "enterprise_value": info.get("enterpriseValue"),
            "pe_ratio": info.get("trailingPE"),
            "revenue_ttm": info.get("totalRevenue"),
            "gross_margin": info.get("grossMargins"),
            "net_margin": info.get("profitMargins"),
            "revenue_growth_yoy": info.get("revenueGrowth"),
            "currency": info.get("financialCurrency", "USD"),
            **fins,
        }
    except Exception as exc:
        log.warning("get_company_financials_failed", ticker=ticker, error=str(exc))
        return {}
