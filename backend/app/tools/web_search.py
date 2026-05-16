"""
DuckDuckGo web search — $0, no API key.
Includes mandatory inter-call jitter to avoid 429 rate limiting.
"""
import asyncio
import random
from typing import Any

from duckduckgo_search import DDGS

from app.observability.logging import get_logger
from app.resilience.circuit_breaker import CircuitBreaker
from app.resilience.retry import retry_async

log = get_logger(__name__)

_cb = CircuitBreaker(name="ddg", failure_threshold=4, recovery_timeout=90.0)

# DDG is aggressive with rate limiting. We never call it faster than this.
_MIN_DELAY = 1.5
_MAX_DELAY = 4.0


async def web_search(query: str, max_results: int = 6) -> list[dict[str, Any]]:
    """
    Search DuckDuckGo and return a list of {title, url, snippet} dicts.
    Adds randomized jitter before the call to avoid triggering rate limits.
    """
    # Jitter BEFORE the call — not just on failure — to stay under DDG's radar.
    jitter = random.uniform(_MIN_DELAY, _MAX_DELAY)
    await asyncio.sleep(jitter)

    async def _call() -> list[dict[str, Any]]:
        results = await asyncio.to_thread(
            lambda: list(DDGS().text(query, max_results=max_results))
        )
        if not results:
            # Empty response = soft rate-limit from DDG, not "no results exist".
            # Raise so retry + circuit breaker can respond appropriately.
            raise RuntimeError("DDG returned empty results (likely rate-limited)")
        return [
            {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
            for r in results
        ]

    try:
        return await _cb.call(lambda: retry_async(
            _call, max_attempts=3, base_delay=5.0, label=f"ddg/{query[:40]}"
        ))
    except Exception as exc:
        log.warning("web_search_failed", query=query[:80], error=str(exc))
        return []
