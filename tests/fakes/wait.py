"""Waiting, in a test, for something another task does."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

LONGEST = 10.0


async def until(condition: Callable[[], object]) -> None:
    """Return once `condition()` holds; TimeoutError past 10 s."""
    async with asyncio.timeout(LONGEST):
        while not condition():  # noqa: ASYNC110 - a condition of another task, polled
            await asyncio.sleep(0.01)
