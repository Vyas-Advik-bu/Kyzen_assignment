"""Web search via Tavily API."""
import asyncio
from typing import Any

import httpx

from app.config import settings
from app.observability.logging import get_logger
from app.resilience.circuit_breaker import CircuitBreaker
from app.resilience.retry import retry_async

log = get_logger(__name__)

_cb = CircuitBreaker(name="tavily", failure_threshold=3, recovery_timeout=60.0)
_TAVILY_URL = "https://api.tavily.com/search"


async def web_search(query: str, max_results: int = 6) -> list[dict[str, Any]]:
    """Search the web via Tavily. Returns a list of {title, url, content} dicts."""

    async def _call() -> list[dict[str, Any]]:
        await asyncio.sleep(0.3)  # polite pacing

        payload = {
            "api_key": settings.tavily_api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(_TAVILY_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results") or []
        if not results:
            raise RuntimeError(f"Tavily returned no results for: {query!r}")

        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
            }
            for r in results
        ]

    try:
        return await _cb.call(lambda: retry_async(
            _call, max_attempts=3, label=f"tavily/{query[:40]}"
        ))
    except Exception as exc:
        log.warning("web_search_failed", query=query[:80], error=str(exc))
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# DuckDuckGo implementation
# ═══════════════════════════════════════════════════════════════════════════════
#
# import random
# from duckduckgo_search import DDGS
#
# _cb = CircuitBreaker(name="ddg", failure_threshold=4, recovery_timeout=90.0)
# _MIN_DELAY = 1.5
# _MAX_DELAY = 4.0
#
# async def web_search(query: str, max_results: int = 6) -> list[dict[str, Any]]:
#     # Jitter BEFORE the call — DDG aggressively rate-limits without it.
#     await asyncio.sleep(random.uniform(_MIN_DELAY, _MAX_DELAY))
#
#     async def _call():
#         results = await asyncio.to_thread(
#             lambda: list(DDGS().text(query, max_results=max_results))
#         )
#         if not results:
#             raise RuntimeError("DDG returned empty results (likely rate-limited)")
#         return [{"title": r.get("title",""), "url": r.get("href",""),
#                  "snippet": r.get("body","")} for r in results]
#
#     try:
#         return await _cb.call(lambda: retry_async(
#             _call, max_attempts=3, base_delay=5.0, label=f"ddg/{query[:40]}"
#         ))
#     except Exception as exc:
#         log.warning("web_search_failed", query=query[:80], error=str(exc))
#         return []
