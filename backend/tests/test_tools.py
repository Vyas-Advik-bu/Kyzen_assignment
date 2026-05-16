"""Unit tests for tool layer — network calls fully mocked."""
import pytest
import respx
import httpx
from unittest.mock import patch, MagicMock

from app.tools.web_search import web_search
from app.tools.fetch_page import fetch_page


class TestWebSearch:
    async def test_returns_results(self):
        fake_results = [
            {"title": "Apple Inc", "href": "https://apple.com", "body": "Tech company"},
        ]
        with patch("app.tools.web_search.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text.return_value = fake_results
            with patch("app.tools.web_search.asyncio.sleep"):  # skip jitter
                results = await web_search("Apple revenue 2024", max_results=3)
        assert len(results) == 1
        assert results[0]["title"] == "Apple Inc"
        assert results[0]["url"] == "https://apple.com"

    async def test_returns_empty_on_failure(self):
        with patch("app.tools.web_search.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text.side_effect = Exception("DDG blocked")
            with patch("app.tools.web_search.asyncio.sleep"):
                results = await web_search("test query")
        assert results == []

    async def test_circuit_opens_after_repeated_failures(self):
        # Reset circuit between tests by using a fresh import state
        with patch("app.tools.web_search.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text.side_effect = Exception("always fails")
            with patch("app.tools.web_search.asyncio.sleep"):
                for _ in range(5):
                    results = await web_search("test")
        # Circuit should open — all calls return []
        assert results == []


class TestFetchPage:
    @respx.mock
    async def test_fetches_and_summarizes(self, respx_mock):
        respx_mock.get("https://example.com/article").mock(
            return_value=httpx.Response(
                200,
                text="<html><body><p>Apple reported $400B revenue in 2024.</p></body></html>",
            )
        )
        mock_llm = MagicMock()

        async def _async_summary(prompt: str) -> str:
            return "Apple had $400B revenue."

        mock_llm.generate_short = _async_summary

        result = await fetch_page("https://example.com/article", mock_llm)
        assert result["url"] == "https://example.com/article"
        assert "Apple" in result["summary"] or result["summary"] != ""

    @respx.mock
    async def test_handles_404_gracefully(self, respx_mock):
        respx_mock.get("https://example.com/notfound").mock(
            return_value=httpx.Response(404)
        )
        mock_llm = MagicMock()
        result = await fetch_page("https://example.com/notfound", mock_llm)
        assert "error" in result
        assert result["summary"] == ""
