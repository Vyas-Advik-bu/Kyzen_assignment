"""
Regression tests for all confirmed QA bugs.
Each test is named after the bug ID from the QA plan.
"""
import asyncio
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from app.agent.orchestrator import _safe_company_type, _compact_evidence
from app.agent.schemas import CompanyType
from app.streaming.bus import JobEventBus
from app.streaming.events import phase_start, done_event
from app.tools.fetch_page import _validate_url, _SSRFError
from app.tools.web_search import web_search
from app.excel.builder import _safe_str, build_excel
from app.agent.schemas import Portfolio, ResolvedCompany, Financials, CompanyType
from app.jobs.store import JobStore


# ── A3: CompanyType enum crash ────────────────────────────────────────────────

class TestA3SafeCompanyType:
    def test_valid_public(self):
        assert _safe_company_type("public") == CompanyType.PUBLIC

    def test_valid_private(self):
        assert _safe_company_type("private") == CompanyType.PRIVATE

    def test_capitalized_coerced(self):
        assert _safe_company_type("Public") == CompanyType.PUBLIC

    def test_unknown_string_becomes_unknown(self):
        assert _safe_company_type("corporation") == CompanyType.UNKNOWN

    def test_publicly_traded_becomes_unknown(self):
        assert _safe_company_type("publicly traded") == CompanyType.UNKNOWN

    def test_none_becomes_unknown(self):
        assert _safe_company_type(None) == CompanyType.UNKNOWN

    def test_int_becomes_unknown(self):
        assert _safe_company_type(42) == CompanyType.UNKNOWN

    def test_empty_string_becomes_unknown(self):
        assert _safe_company_type("") == CompanyType.UNKNOWN


# ── A6+A9: Event bus memory leak + replay off-by-one ─────────────────────────

class TestA6BusMemoryLeak:
    async def test_events_cleaned_up_after_close_and_unsubscribe(self):
        bus = JobEventBus()
        job_id = "test-job-leak"

        # Publish some events
        ev = phase_start(job_id, 0, "resolve", "test")
        bus.publish(ev)

        # Subscribe, consume, unsubscribe
        collected = []
        async def _consume():
            async for event in bus.subscribe(job_id, last_event_id=-1, heartbeat_interval=0.05):
                collected.append(event)

        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.01)
        bus.close_job(job_id)
        await asyncio.wait_for(task, timeout=1.0)

        # After unsubscribe + close, buffer must be freed
        assert job_id not in bus._events
        assert job_id not in bus._closed

    async def test_cleanup_immediate_when_no_subscribers(self):
        bus = JobEventBus()
        job_id = "test-job-nosubs"
        bus.publish(phase_start(job_id, 0, "resolve", "test"))
        assert job_id in bus._events

        bus.close_job(job_id)
        # No subscribers → cleaned up immediately
        assert job_id not in bus._events


class TestA9ReplayOffByOne:
    async def test_replay_excludes_already_received_event(self):
        bus = JobEventBus()
        job_id = "test-replay"

        ev0 = phase_start(job_id, 0, "resolve", "test")
        ev1 = phase_start(job_id, 1, "research", "test")
        bus.publish(ev0)
        bus.publish(ev1)

        # Realistic ordering: subscribe first, close after
        replayed = []

        async def _consume():
            async for event in bus.subscribe(job_id, last_event_id=0, heartbeat_interval=0.05):
                replayed.append(event)

        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.01)  # let consumer start and drain buffered events
        bus.close_job(job_id)
        await asyncio.wait_for(task, timeout=1.0)

        # Should only get seq=1, NOT seq=0 again
        assert len(replayed) == 1
        assert replayed[0].seq == 1

    async def test_fresh_connect_gets_all_events(self):
        bus = JobEventBus()
        job_id = "test-fresh"

        bus.publish(phase_start(job_id, 0, "resolve", "test"))
        bus.publish(phase_start(job_id, 1, "research", "test"))

        replayed = []

        async def _consume():
            async for event in bus.subscribe(job_id, last_event_id=-1, heartbeat_interval=0.05):
                replayed.append(event)

        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.01)
        bus.close_job(job_id)
        await asyncio.wait_for(task, timeout=1.0)

        assert len(replayed) == 2


# ── A7: Excel formula injection ───────────────────────────────────────────────

class TestA7FormulaInjection:
    def test_equals_prefix_sanitized(self):
        assert _safe_str("=SUM(1,2)").startswith(" ")

    def test_plus_prefix_sanitized(self):
        assert _safe_str("+cmd").startswith(" ")

    def test_minus_prefix_sanitized(self):
        assert _safe_str("-1+2").startswith(" ")

    def test_at_prefix_sanitized(self):
        assert _safe_str("@SUM").startswith(" ")

    def test_normal_string_unchanged(self):
        assert _safe_str("Apple Inc") == "Apple Inc"

    def test_none_returns_default(self):
        assert _safe_str(None) == "N/A"

    def test_formula_injection_in_excel_file(self, tmp_path):
        """Ensure the built Excel file does not contain raw formula strings."""
        portfolio = Portfolio(
            company=ResolvedCompany(
                name="=HYPERLINK(\"http://evil.com\",\"click\")",
                ticker="TEST",
                type=CompanyType.PUBLIC,
            ),
            financials=Financials(),
        )
        with patch("app.excel.builder._OUTPUT_DIR", tmp_path):
            path = build_excel(portfolio, "formula-test")

        import openpyxl
        wb = openpyxl.load_workbook(path)
        ws = wb["Overview"]
        # All cell values should be strings, not formulas
        for row in ws.iter_rows():
            for cell in row:
                if cell.value and isinstance(cell.value, str):
                    assert not cell.value.startswith("="), \
                        f"Formula found in cell {cell.coordinate}: {cell.value!r}"


# ── A8: Content-Disposition filename ─────────────────────────────────────────

class TestA8SafeFilename:
    def test_safe_filename_strips_special_chars(self):
        from app.main import _safe_download_filename
        result = _safe_download_filename("Apple\r\nInc")
        assert "\r" not in result
        assert "\n" not in result

    def test_safe_filename_normal(self):
        from app.main import _safe_download_filename
        result = _safe_download_filename("Apple Inc")
        assert result == "Apple_Inc_research.xlsx"

    def test_safe_filename_injection_chars_removed(self):
        from app.main import _safe_download_filename
        result = _safe_download_filename("'; DROP TABLE--")
        assert "'" not in result
        assert ";" not in result

    def test_safe_filename_empty_fallback(self):
        from app.main import _safe_download_filename
        result = _safe_download_filename("!!!")
        assert result == "research_research.xlsx"


# ── B1: SSRF protection ───────────────────────────────────────────────────────

class TestB1SSRF:
    def test_http_allowed(self):
        _validate_url("http://example.com/page")  # must not raise

    def test_https_allowed(self):
        _validate_url("https://reuters.com/article")

    def test_localhost_blocked(self):
        with pytest.raises(_SSRFError):
            _validate_url("http://localhost:11434/api/chat")

    def test_loopback_ip_blocked(self):
        with pytest.raises(_SSRFError):
            _validate_url("http://127.0.0.1/secret")

    def test_private_ip_blocked(self):
        with pytest.raises(_SSRFError):
            _validate_url("http://192.168.1.1/admin")

    def test_link_local_blocked(self):
        with pytest.raises(_SSRFError):
            _validate_url("http://169.254.169.254/metadata")

    def test_file_scheme_blocked(self):
        with pytest.raises(_SSRFError):
            _validate_url("file:///etc/passwd")

    def test_ftp_scheme_blocked(self):
        with pytest.raises(_SSRFError):
            _validate_url("ftp://example.com/data")

    def test_metadata_hostname_blocked(self):
        with pytest.raises(_SSRFError):
            _validate_url("http://metadata.google.internal/")


# ── A12: Empty search results treated as failure (retried) ────────────────────

class TestA12EmptySearchResults:
    async def test_empty_results_raises_and_retries(self):
        call_count = 0

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"results": []}  # Tavily returns empty

        async def fake_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_response

        mock_client = MagicMock()
        mock_client.post = fake_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.tools.web_search.httpx.AsyncClient", return_value=mock_client), \
             patch("app.tools.web_search.asyncio.sleep"), \
             patch("app.resilience.retry.asyncio.sleep"):
            result = await web_search("test query", max_results=3)

        assert call_count > 1  # confirmed retry happened
        assert result == []    # graceful [] after exhaustion

    async def test_nonempty_results_returned_normally(self):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "results": [{"title": "Apple", "url": "https://apple.com", "content": "tech"}]
        }

        async def fake_post(*args, **kwargs):
            return mock_response

        mock_client = MagicMock()
        mock_client.post = fake_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.tools.web_search.httpx.AsyncClient", return_value=mock_client), \
             patch("app.tools.web_search.asyncio.sleep"):
            result = await web_search("Apple revenue 2025")

        assert len(result) == 1
        assert result[0]["title"] == "Apple"


# ── A13: Evidence compaction ──────────────────────────────────────────────────

class TestA13EvidenceCompaction:
    def test_large_result_truncated(self):
        evidence = [{"tool": "fetch_page", "args": {}, "result": "x" * 5000}]
        out = _compact_evidence(evidence)
        import json
        data = json.loads(out)
        assert len(data[0]["result"]) < 2000

    def test_small_result_unchanged(self):
        evidence = [{"tool": "web_search", "args": {}, "result": "short"}]
        out = _compact_evidence(evidence)
        import json
        data = json.loads(out)
        assert data[0]["result"] == "short"

    def test_multiple_tools_all_present(self):
        evidence = [
            {"tool": "web_search", "args": {}, "result": "r1"},
            {"tool": "get_company_financials", "args": {}, "result": "r2"},
        ]
        out = _compact_evidence(evidence)
        import json
        data = json.loads(out)
        assert len(data) == 2


# ── A15: Excel file cleanup on job eviction ───────────────────────────────────

class TestA15ExcelCleanup:
    def test_excel_deleted_on_lru_eviction(self, tmp_path):
        store = JobStore()
        # Patch _MAX_STORED_JOBS to 2 for this test
        import app.jobs.store as store_module
        original = store_module._MAX_STORED_JOBS
        store_module._MAX_STORED_JOBS = 2

        with patch.object(store_module, "_OUTPUT_DIR", tmp_path):
            # Create 2 jobs and their fake Excel files
            j1 = store.create("Company A")
            j2 = store.create("Company B")
            (tmp_path / f"{j1.job_id}.xlsx").write_bytes(b"fake")
            (tmp_path / f"{j2.job_id}.xlsx").write_bytes(b"fake")

            # Third job triggers eviction of j1
            store.create("Company C")

            assert not (tmp_path / f"{j1.job_id}.xlsx").exists(), \
                "Excel file for evicted job should be deleted"
            assert (tmp_path / f"{j2.job_id}.xlsx").exists(), \
                "Excel file for non-evicted job should remain"

        store_module._MAX_STORED_JOBS = original
