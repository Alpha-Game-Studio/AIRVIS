"""Bridging helpers between the async pipeline and the legacy sync API."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def run_blocking(coro: Coroutine[Any, Any, T], *, timeout: float | None = None) -> T:
    """Run ``coro`` to completion from synchronous code.

    Works both when no event loop is running (the common case for the CLI and
    the threaded HTTP server) and when called from inside a running loop, where
    the coroutine is handed to a dedicated worker thread instead of deadlocking.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_with_timeout(coro, timeout))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, _with_timeout(coro, timeout))
        return future.result()


async def _with_timeout(coro: Coroutine[Any, Any, T], timeout: float | None) -> T:
    if timeout is None:
        return await coro
    return await asyncio.wait_for(coro, timeout)


async def call_maybe_async(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Call ``func`` and await it when it returns an awaitable."""
    result = func(*args, **kwargs)
    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
        return await result
    return result


async def to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
    """``asyncio.to_thread`` wrapper kept in one place for easier testing."""
    return await asyncio.to_thread(func, *args, **kwargs)


__all__ = ["call_maybe_async", "run_blocking", "to_thread"]
