import asyncio
from collections.abc import Callable, Awaitable
from typing import TypeVar

T = TypeVar("T")


class TimeoutError(Exception):  # noqa: A001
    pass


async def with_timeout(
    fn: Callable[[], Awaitable[T]],
    seconds: float,
    label: str = "operation",
) -> T:
    try:
        return await asyncio.wait_for(fn(), timeout=seconds)
    except asyncio.TimeoutError:
        raise TimeoutError(f"'{label}' timed out after {seconds}s")
