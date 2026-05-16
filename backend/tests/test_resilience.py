"""Unit tests for resilience primitives — no network required."""
import asyncio
import pytest
from unittest.mock import AsyncMock

from app.resilience.retry import retry_async
from app.resilience.circuit_breaker import CircuitBreaker, CircuitOpen
from app.resilience.timeout import with_timeout, TimeoutError


class TestRetry:
    async def test_succeeds_on_first_try(self):
        fn = AsyncMock(return_value="ok")
        result = await retry_async(fn, max_attempts=3, base_delay=0)
        assert result == "ok"
        assert fn.call_count == 1

    async def test_retries_then_succeeds(self):
        fn = AsyncMock(side_effect=[ValueError("fail"), ValueError("fail"), "ok"])
        result = await retry_async(fn, max_attempts=3, base_delay=0, jitter=False)
        assert result == "ok"
        assert fn.call_count == 3

    async def test_raises_after_max_attempts(self):
        fn = AsyncMock(side_effect=RuntimeError("always fails"))
        with pytest.raises(RuntimeError, match="always fails"):
            await retry_async(fn, max_attempts=2, base_delay=0)
        assert fn.call_count == 2


class TestCircuitBreaker:
    async def test_passes_through_on_success(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        fn = AsyncMock(return_value=42)
        result = await cb.call(fn)
        assert result == 42

    async def test_opens_after_threshold(self):
        cb = CircuitBreaker(name="test", failure_threshold=2, recovery_timeout=9999)
        fn = AsyncMock(side_effect=RuntimeError("boom"))
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(fn)
        assert cb.is_open

    async def test_raises_circuit_open_when_open(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=9999)
        fn = AsyncMock(side_effect=RuntimeError("x"))
        with pytest.raises(RuntimeError):
            await cb.call(fn)
        with pytest.raises(CircuitOpen):
            await cb.call(AsyncMock(return_value="ok"))

    async def test_recovers_after_timeout(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0.01)
        fn = AsyncMock(side_effect=RuntimeError("x"))
        with pytest.raises(RuntimeError):
            await cb.call(fn)
        assert cb.is_open
        await asyncio.sleep(0.02)
        assert not cb.is_open

    async def test_resets_failures_on_success(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        fail = AsyncMock(side_effect=RuntimeError("x"))
        ok = AsyncMock(return_value="ok")
        with pytest.raises(RuntimeError):
            await cb.call(fail)
        await cb.call(ok)
        assert cb._failures == 0
        assert not cb.is_open


class TestTimeout:
    async def test_succeeds_within_timeout(self):
        result = await with_timeout(lambda: asyncio.sleep(0, result="done"), seconds=1)  # type: ignore[call-arg]
        # asyncio.sleep doesn't take result kwarg — use coroutine directly
        async def _fast():
            return "done"
        result = await with_timeout(_fast, seconds=1)
        assert result == "done"

    async def test_raises_on_timeout(self):
        async def _slow():
            await asyncio.sleep(10)

        with pytest.raises(TimeoutError):
            await with_timeout(_slow, seconds=0.01)
