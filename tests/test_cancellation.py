"""A check cancelled before it starts leaves no coroutine behind (asyncio warns of one never
awaited: the configuration of the tests makes every warning an error), and what a call started
has ended once it returned, cancelled or not."""

from __future__ import annotations

import asyncio
import gc

import pytest

from fakes.world import World
from sattleme_checkout._engine import Payable
from sattleme_checkout._stored import invoice_to_check
from sattleme_checkout._tasks import cancel


async def test_a_lightning_check_cancelled_before_it_starts() -> None:
    world = await World.start(lot=0)
    try:
        client = world.client()
        engine = client._engine
        invoice = await client.create_invoice(ref="ORD-1", sats=21)
        assert invoice.bolt11 is not None
        assert invoice.payment_hash is not None
        assert invoice.verify_url is not None
        payable = Payable(invoice.bolt11, invoice.payment_hash, invoice.verify_url, 0)
        task = engine.lightning_check(engine.http(), payable)
        task.cancel()
        await asyncio.sleep(0)
        assert task.cancelled()
        del task
        gc.collect()
    finally:
        await world.close()


async def test_a_full_check_cancelled_at_once() -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-2", sats=49_000)
        stored = invoice_to_check(invoice)
        for steps in range(80):
            check = asyncio.ensure_future(client._engine.parts(stored))
            for _ in range(steps):
                await asyncio.sleep(0)
            check.cancel()
            await asyncio.gather(check, return_exceptions=True)
            del check
            gc.collect()
    finally:
        await world.close()


async def test_what_a_call_started_has_ended_when_it_returns_cancelled_twice() -> None:
    """SPEC.md §13: nothing a call started runs on once it returned. `cancel`, with which a call
    ends what it started (its checks, the explorer's WebSocket, requests at once), waits for each
    to end however often the call is cancelled meanwhile, then raises the cancellation: what it
    started has ended when it returns, even when its end takes a while (a pool closing a
    connection) and the call is cancelled again meanwhile."""
    ended: list[str] = []

    async def started() -> None:
        try:
            await asyncio.sleep(3600)
        finally:
            # Its end takes a while: a pool closing a relay's connection.
            await asyncio.sleep(0.3)
            ended.append("started")

    async def call() -> None:
        task = asyncio.ensure_future(started())
        try:
            await asyncio.sleep(3600)
        finally:
            await cancel([task])

    running = asyncio.ensure_future(call())
    await asyncio.sleep(0.05)
    running.cancel()
    await asyncio.sleep(0.05)
    # Again, while it waits for what it started to end.
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert ended == ["started"]
