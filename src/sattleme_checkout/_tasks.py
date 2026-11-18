"""Running requests at once, without leaking tasks: whatever a call starts ends with it."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Any, TypeVar

from .errors import SattlemeError

T = TypeVar("T")
U = TypeVar("U")


async def settle(awaitable: Awaitable[T]) -> T | SattlemeError:
    """Its result, or the error of SPEC.md §5 it raised. Anything else goes through (a bug, a
    cancellation)."""
    try:
        return await awaitable
    except SattlemeError as error:
        return error


async def cancel(tasks: Sequence[asyncio.Future[Any]]) -> None:
    """Cancel the tasks still running and wait until they end (their outcomes are dropped),
    however often the caller is cancelled meanwhile: its cancellation is raised once they ended,
    so that nothing a call started runs on once it returned (SPEC.md §13). Their ends are
    bounded (a pool's close, a second a connection)."""
    running = [task for task in tasks if not task.done()]
    for task in running:
        task.cancel()
    cancelled: asyncio.CancelledError | None = None
    while any(not task.done() for task in running):
        try:
            # `wait` never raises what the tasks raise.
            await asyncio.wait(running)
        except asyncio.CancelledError as error:
            cancelled = error
    for task in tasks:
        if not task.cancelled():
            task.exception()
    if cancelled is not None:
        raise cancelled


async def both(first: Coroutine[Any, Any, T], second: Coroutine[Any, Any, U]) -> tuple[T, U]:
    """Both at once; the first error ends the other."""
    one, two = asyncio.ensure_future(first), asyncio.ensure_future(second)
    try:
        return await asyncio.gather(one, two)
    finally:
        await cancel([one, two])


async def bounded(
    items: Sequence[T], limit: int, run: Callable[[T], Awaitable[U]]
) -> list[U | SattlemeError]:
    """`run` on every item, `limit` at a time: each outcome (`settle`), in the items' order."""
    results: dict[int, U | SattlemeError] = {}
    next_index = 0

    async def worker() -> None:
        nonlocal next_index
        while next_index < len(items):
            index = next_index
            next_index += 1
            results[index] = await settle(run(items[index]))

    workers = [asyncio.ensure_future(worker()) for _ in range(min(limit, len(items)))]
    try:
        await asyncio.gather(*workers)
    finally:
        await cancel(workers)
    return [results[index] for index in range(len(items))]
