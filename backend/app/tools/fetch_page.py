"""
fetch_page: fetches a URL, extracts clean text with trafilatura,
then summarizes it with the local LLM before returning.

This per-page summarization is the critical mitigation for the context-window
trap: 3-4 raw pages easily exceed 15k tokens; summaries stay under 700 tokens each.
"""
import asyncio
import ipaddress
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx
import trafilatura

from app.observability.logging import get_logger
from app.resilience.retry import retry_async
from app.resilience.timeout import with_timeout

if TYPE_CHECKING:
    from app.llm.ollama_client import OllamaClient

log = get_logger(__name__)

_FETCH_TIMEOUT = 15.0
_MAX_CHARS_FOR_SUMMARY = 8000  # truncate before summarizing to avoid OOM in prompt

_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata.google.internal"})


class _ClientError(Exception):
    """4xx response — not worth retrying."""


class _SSRFError(Exception):
    """URL targets a private/internal address."""


def _validate_url(url: str) -> None:
    """Raise _SSRFError for non-public URLs (SSRF prevention)."""
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise _SSRFError(f"Unparseable URL: {exc}") from exc

    if parsed.scheme not in ("http", "https"):
        raise _SSRFError(f"Disallowed scheme: {parsed.scheme!r}")

    hostname = parsed.hostname or ""
    if not hostname:
        raise _SSRFError("Missing hostname")

    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise _SSRFError(f"Blocked hostname: {hostname}")

    # Block bare IP addresses that resolve to private/reserved ranges
    try:
        ip = ipaddress.ip_address(hostname)
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved):
            raise _SSRFError(f"Private/reserved IP blocked: {hostname}")
    except ValueError:
        pass  # hostname is a domain name, not a bare IP — allow through

_SUMMARIZE_PROMPT = """\
You are a research assistant. Read the following web page content and write a \
concise factual summary (max 150 words) relevant to company research: revenue, \
headcount, market position, products, or strategy. Omit ads, navigation, and fluff.

CONTENT:
{text}

SUMMARY:"""


async def fetch_page(url: str, llm: "OllamaClient") -> dict[str, str]:
    """
    Fetch `url`, extract main text, summarize with LLM.
    Returns {url, summary} — never the full page text.
    """
    try:
        _validate_url(url)
    except _SSRFError as exc:
        log.warning("fetch_ssrf_blocked", url=url, reason=str(exc))
        return {"url": url, "summary": "", "error": f"Blocked: {exc}"}

    async def _fetch() -> str:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_FETCH_TIMEOUT) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)"},
            )
            if 400 <= resp.status_code < 500:
                raise _ClientError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.text

    try:
        html = await with_timeout(
            lambda: retry_async(_fetch, max_attempts=2,
                                non_retriable=(_ClientError,),
                                label=f"fetch/{url[:60]}"),
            seconds=_FETCH_TIMEOUT + 5,
            label=f"fetch_page/{url[:60]}",
        )
    except _ClientError as exc:
        log.warning("fetch_4xx", url=url, error=str(exc))
        return {"url": url, "summary": "", "error": str(exc)}
    except Exception as exc:
        log.warning("fetch_failed", url=url, error=str(exc))
        return {"url": url, "summary": "", "error": str(exc)}

    # Extract clean article text; fall back to empty string on failure
    text: str = await asyncio.to_thread(
        lambda: trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    )

    if not text.strip():
        return {"url": url, "summary": "", "error": "no extractable content"}

    # Truncate before sending to LLM to avoid context overflow
    text = text[: _MAX_CHARS_FOR_SUMMARY]

    try:
        summary = await llm.generate_short(
            _SUMMARIZE_PROMPT.format(text=text)
        )
    except Exception as exc:
        log.warning("summarize_failed", url=url, error=str(exc))
        # Return truncated raw text as fallback if summarization fails
        summary = text[:500]

    return {"url": url, "summary": summary}
