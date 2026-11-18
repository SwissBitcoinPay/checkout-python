"""One call at a time per key (an order's `ref`, an invoice), within one client.

A client may serve several threads, each with its own event loop (the synchronous `Client` runs
each call in a loop of its own), so the lock is a thread lock, waited for without blocking the
event loop: a cancelled or timed-out waiter leaves nothing behind.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_FIRST_WAIT = 0.005
_LONGEST_WAIT = 0.05


class KeyedLock:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, tuple[threading.Lock, int]] = {}

    def _enter(self, key: str) -> threading.Lock:
        with self._guard:
            lock, users = self._locks.get(key, (threading.Lock(), 0))
            self._locks[key] = (lock, users + 1)
            return lock

    def _leave(self, key: str) -> None:
        with self._guard:
            lock, users = self._locks[key]
            if users == 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        """Hold `key`: the calls holding the same key run one after the other."""
        lock = self._enter(key)
        try:
            wait = _FIRST_WAIT
            while not lock.acquire(blocking=False):
                await asyncio.sleep(wait)
                wait = min(wait * 2, _LONGEST_WAIT)
            try:
                yield
            finally:
                lock.release()
        finally:
            self._leave(key)
