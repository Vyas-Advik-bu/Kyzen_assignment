import asyncio
import random
from collections.abc import Callable, Awaitable
from typing import TypeVar

from app.observability.logging import get_logger

log = get_logger(__name__)
T = TypeVar("T")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    label: str = "operation",
    non_retriable: tuple[type[Exception], ...] = (),
) -> T:
    """Retry an async callable with exponential backoff and optional jitter."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except non_retriable:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            if jitter:
                delay *= random.uniform(0.5, 1.5)
            log.warning("retry", label=label, attempt=attempt, delay_s=round(delay, 2),
                        error=str(exc))
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
