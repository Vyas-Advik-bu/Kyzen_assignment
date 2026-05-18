"""Company profile and financial data via yfinance."""
import asyncio
from typing import Any

import yfinance as yf

from app.observability.logging import get_logger
from app.resilience.circuit_breaker import CircuitBreaker
from app.resilience.retry import retry_async

log = get_logger(__name__)

_cb = CircuitBreaker(name="yfinance", failure_threshold=3, recovery_timeout=120.0)


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if f != f else f  # NaN guard
    except (TypeError, ValueError):
        return None


def _fetch_info(ticker: str) -> dict:
    return yf.Ticker(ticker).info or {}


def _fetch_income(ticker: str) -> list[dict]:
    try:
        inc = yf.Ticker(ticker).income_stmt
        if inc is None or inc.empty:
            return []
        rows = []
        for col in inc.columns[:4]:
            def _row(label: str) -> float | None:
                if label in inc.index:
                    return _safe_float(inc.loc[label, col])
                return None
            rows.append({
                "year": col.year,
                "revenue": _row("Total Revenue"),
                "gross_profit": _row("Gross Profit"),
                "operating_income": _row("Operating Income"),
                "net_income": _row("Net Income"),
                "ebitda": _row("EBITDA"),
            })
        return rows
    except Exception:
        return []


async def get_company_profile(ticker: str) -> dict[str, Any]:
    """Return company profile from yfinance: sector, employees, description, etc."""
    async def _call() -> dict[str, Any]:
        info = await asyncio.to_thread(_fetch_info, ticker)
        return {
            "name": info.get("longName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "country": info.get("country"),
            "website": info.get("website"),
            "description": info.get("longBusinessSummary") or "",
            "employees": info.get("fullTimeEmployees"),
            "founded": info.get("foundingDate"),
            "headquarters": info.get("city"),
            "exchange": info.get("exchange"),
        }

    try:
        return await _cb.call(lambda: retry_async(
            _call, max_attempts=2, label=f"yf.profile/{ticker}"
        ))
    except Exception as exc:
        log.warning("get_company_profile_failed", ticker=ticker, error=str(exc))
        return {}


async def get_company_financials(ticker: str) -> dict[str, Any]:
    """Return structured financials from yfinance: market cap, margins, income statements."""
    async def _call() -> dict[str, Any]:
        info, annual = await asyncio.gather(
            asyncio.to_thread(_fetch_info, ticker),
            asyncio.to_thread(_fetch_income, ticker),
        )
        return {
            "market_cap": _safe_float(info.get("marketCap")),
            "enterprise_value": _safe_float(info.get("enterpriseValue")),
            "pe_ratio": _safe_float(info.get("trailingPE")),
            "revenue_ttm": _safe_float(info.get("totalRevenue")),
            "gross_margin": _safe_float(info.get("grossMargins")),
            "net_margin": _safe_float(info.get("profitMargins")),
            "revenue_growth_yoy": _safe_float(info.get("revenueGrowth")),
            "currency": info.get("financialCurrency", "USD"),
            "annual": annual,
        }

    try:
        return await _cb.call(lambda: retry_async(
            _call, max_attempts=2, label=f"yf.financials/{ticker}"
        ))
    except Exception as exc:
        log.warning("get_company_financials_failed", ticker=ticker, error=str(exc))
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
# FMP (Financial Modeling Prep) implementation
# ═══════════════════════════════════════════════════════════════════════════════
#
# import httpx
# from app.config import settings
#
# _BASE = "https://financialmodelingprep.com/api/v3"
#
# async def _fmp_get(path):
#     url = f"{_BASE}{path}"
#     params = {"apikey": settings.fmp_api_key}
#     async with httpx.AsyncClient(timeout=15.0) as client:
#         resp = await client.get(url, params=params)
#         resp.raise_for_status()
#         return resp.json()
#
# async def get_company_profile(ticker): ...
# async def get_company_financials(ticker): ...
