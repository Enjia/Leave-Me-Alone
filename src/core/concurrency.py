"""Controlled concurrency utilities.

Provides reusable primitives that replace the repeated
``asyncio.gather + cancel + re-raise`` patterns scattered across the
orchestrator modules.

Public API
----------
- ``gather_with_cancel``   – Run coroutines concurrently; cancel survivors on first error.
- ``wait_first_exception`` – Like ``asyncio.wait(FIRST_EXCEPTION)`` with auto-cleanup.
- ``ConcurrencyLimiter``   – Semaphore-based concurrency gate with optional timeout.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ------------------------------------------------------------------
# gather_with_cancel
# ------------------------------------------------------------------

async def gather_with_cancel(
    *coros: Coroutine[Any, Any, T],
    return_exceptions: bool = False,
) -> list[T | BaseException]:
    """Run coroutines concurrently and cancel all on first exception.

    Unlike bare ``asyncio.gather``, this function guarantees that if any
    task raises, all remaining tasks are cancelled and awaited before the
    exception propagates.  This eliminates the repeated cancel-loop
    boilerplate in ``stage_runner.py`` and ``round_runner.py``.

    Parameters
    ----------
    *coros:
        Coroutines to run concurrently.
    return_exceptions:
        If ``True``, exceptions are returned in the result list instead
        of being raised.  Behaves like ``asyncio.gather(..., return_exceptions=True)``.
    """
    if not coros:
        return []

    tasks = [asyncio.ensure_future(coro) for coro in coros]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=return_exceptions)
        return list(results)
    except BaseException:
        await _cancel_tasks(tasks)
        raise


async def wait_first_exception(
    *coros: Coroutine[Any, Any, T],
) -> list[T]:
    """Run coroutines; return all results or raise the first exception.

    Semantics:
    1. All coroutines run concurrently.
    2. If any raises, the remaining are cancelled and the exception propagates.
    3. If all succeed, their results are returned in input order.

    This is the "fail-fast" variant used by ``_await_worker_deliveries``.
    """
    if not coros:
        return []

    tasks = [asyncio.ensure_future(coro) for coro in coros]
    try:
        done: set[asyncio.Task[T]] = set()
        pending: set[asyncio.Task[T]] = set(tasks)

        while pending:
            newly_done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in newly_done:
                exception = task.exception()
                if exception is not None:
                    await _cancel_tasks(pending)
                    raise exception
            done |= newly_done

        return [task.result() for task in tasks]
    except BaseException:
        await _cancel_tasks(tasks)
        raise


# ------------------------------------------------------------------
# ConcurrencyLimiter
# ------------------------------------------------------------------

class ConcurrencyLimiter:
    """Semaphore-based concurrency gate with optional per-task timeout.

    Usage::

        limiter = ConcurrencyLimiter(max_concurrent=3)

        async with limiter:
            await do_work()

        # or with timeout:
        result = await limiter.run(do_work(), timeout_sec=30)
    """

    def __init__(self, max_concurrent: int = 5) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._active_count = 0

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def active_count(self) -> int:
        return self._active_count

    async def __aenter__(self) -> ConcurrencyLimiter:
        await self._semaphore.acquire()
        self._active_count += 1
        return self

    async def __aexit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: object) -> None:
        self._active_count -= 1
        self._semaphore.release()

    async def run(
        self,
        coro: Awaitable[T],
        *,
        timeout_sec: float | None = None,
    ) -> T:
        """Acquire the semaphore, run *coro*, and release.

        If *timeout_sec* is set, the coroutine is wrapped in
        ``asyncio.wait_for`` so it is cancelled on timeout.
        """
        async with self:
            if timeout_sec is not None and timeout_sec > 0:
                return await asyncio.wait_for(coro, timeout=timeout_sec)  # type: ignore[arg-type]
            return await coro  # type: ignore[misc]

    async def run_many(
        self,
        coros: list[Coroutine[Any, Any, T]],
        *,
        timeout_sec: float | None = None,
    ) -> list[T]:
        """Run multiple coroutines with concurrency limiting.

        All coroutines are started concurrently but at most
        ``max_concurrent`` run at the same time.
        """

        async def _limited(coro: Coroutine[Any, Any, T]) -> T:
            return await self.run(coro, timeout_sec=timeout_sec)

        tasks = [asyncio.ensure_future(_limited(coro)) for coro in coros]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            await _cancel_tasks(tasks)
            raise


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

async def _cancel_tasks(tasks: set[asyncio.Task[Any]] | list[asyncio.Task[Any]]) -> None:
    """Cancel all non-done tasks and await their completion."""
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
