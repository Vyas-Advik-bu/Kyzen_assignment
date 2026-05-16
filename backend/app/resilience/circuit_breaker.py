import asyncio
import time
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from typing import TypeVar

from app.observability.logging import get_logger

log = get_logger(__name__)
T = TypeVar("T")


class CircuitOpen(Exception):
    """Raised when the circuit breaker is open and calls are being rejected."""


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 60.0

    _failures: int = field(default=0, init=False, repr=False)
    _opened_at: float | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.recovery_timeout:
            return False  # half-open: allow a probe
        return True

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            if self.is_open:
                raise CircuitOpen(f"Circuit '{self.name}' is open")

        try:
            result = await fn()
            async with self._lock:
                if self._failures > 0:
                    log.info("circuit_recovered", name=self.name)
                self._failures = 0
                self._opened_at = None
            return result
        except CircuitOpen:
            raise
        except Exception as exc:
            async with self._lock:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._opened_at = time.monotonic()
                    log.warning("circuit_opened", name=self.name, failures=self._failures)
            raise exc
