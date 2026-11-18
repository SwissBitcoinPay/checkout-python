"""Waiting for a payment (SPEC.md §23, §31 Waiting for an on-chain payment).

Each status is handed to `on_status`; the wait ends once one is `paid` or `expired`. A `network`
error is retried; any other error ends it. With an address, the explorer's WebSocket
(mempool.space's, or the merchant's Bitcoin node's with its fallback) says when the address's
transactions change, or a block comes: a full check of the invoice then (and every 60 s at
least); between them, its Lightning invoice and the rest invoices the last full check found,
every interval.

A failure that each check meets again (an explorer, a Lightning address that does not answer, a
rest invoice that cannot be asked, a witness `none` that cannot be published) is logged once, and
again only after a check went without it, never at each check.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Final, Protocol

from . import _json
from ._constants import (
    EXPLORER_CHECK_SECONDS,
    RECONNECT_FIRST_SECONDS,
    RECONNECT_MAX_SECONDS,
)
from ._engine import (
    Call,
    Engine,
    Part,
    Parts,
    RestCheck,
    payable_of,
    payable_of_rest,
    status_of,
)
from ._explorer import CallExplorer, Source
from ._log import Logger
from ._node import BitcoinNode
from ._onchain import LightningResult, OnchainResult
from ._relay import WebSocketConnect, WebSocketConnection
from ._stored import InvoiceToCheck
from ._tasks import cancel
from .errors import NetworkError, SattlemeError
from .models import PaymentStatus

OnStatus = Callable[[PaymentStatus], object]
"""Hears each status: a function, or an `async` one (awaited)."""

_CHANGES: Final = (
    "address-transactions",
    "block-transactions",
    "address-removed-transactions",
    "block",
)
_CLOSE_SECONDS: Final = 1.0


async def _hand(on_status: OnStatus | None, status: PaymentStatus) -> None:
    if on_status is None:
        return
    heard = on_status(status)
    if inspect.isawaitable(heard):
        await heard


_FAILED = (
    "A check of the payment failed (%s): the wait goes on, and logs it again once a check worked"
)


class _Warnings:
    """Where the engine's warnings go during a wait: each one (its message without its
    arguments) logged once, and again only after a full check went without it."""

    def __init__(self, logger: Logger) -> None:
        self._logger = logger
        self._logged: set[str] = set()
        self._met: set[str] = set()

    def warning(self, msg: str, /, *args: object) -> None:
        self._met.add(msg)
        if msg not in self._logged:
            self._logged.add(msg)
            self._logger.warning(msg, *args)

    def check_ended(self) -> None:
        """A full check ended: what it did not meet stopped, and is logged when it comes back."""
        self._logged &= self._met
        self._met = set()


class _Failing:
    """Whether the last check failed: its `network` error logged once while checks fail."""

    def __init__(self, logger: Logger) -> None:
        self._logger = logger
        self._failing = False

    def failed(self, error: NetworkError) -> None:
        if not self._failing:
            self._logger.warning(_FAILED, error.message)
        self._failing = True

    def worked(self) -> None:
        self._failing = False


async def wait_lightning(
    engine: Engine, stored: InvoiceToCheck, interval: float, on_status: OnStatus | None
) -> PaymentStatus:
    """An invoice without an address: its Lightning part, every interval."""
    failing = _Failing(engine.settings.logger)
    while True:
        try:
            status = await engine.status(stored)
        except NetworkError as error:
            failing.failed(error)
        else:
            failing.worked()
            await _hand(on_status, status)
            if status.final:
                return status
        await asyncio.sleep(interval)


def _is_change(message: str | bytes) -> bool:
    value = _json.parse_text(message) if isinstance(message, str) else _json.parse_bytes(message)
    return isinstance(value, dict) and any(key in value for key in _CHANGES)


class WatchedExplorer(Protocol):
    """Which explorers' WebSockets a connection tries, in order, and what hears that the node
    failed (_explorer.CallExplorer)."""

    def sources(self) -> list[Source]: ...

    def node_failed(self) -> None: ...


async def _connection(
    connect: WebSocketConnect,
    url: str,
    address: str,
    on_change: Callable[[], None],
    *,
    open_timeout: float,
) -> bool:
    """One connection to an explorer's WebSocket, until it ends: whether it received a message
    (one lost before any is a failure). `on_change` once connected, and at each message about the
    address or a new block."""
    socket: WebSocketConnection | None = None
    answered = False
    try:
        async with asyncio.timeout(open_timeout):
            socket = await connect(url)
        await socket.send(_json.dumps({"action": "want", "data": ["blocks"]}))
        await socket.send(_json.dumps({"track-address": address}))
        on_change()
        while True:
            message = await socket.recv()
            answered = True
            if _is_change(message):
                on_change()
    except Exception:  # noqa: BLE001, S110 - any end of the socket: connect again
        pass
    finally:
        if socket is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(_CLOSE_SECONDS):
                    await socket.close()
    return answered


async def watch_address(
    connect: WebSocketConnect,
    explorer: WatchedExplorer,
    address: str,
    on_change: Callable[[], None],
    *,
    open_timeout: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Watch `address` on the explorer's WebSocket until cancelled: `on_change` at each
    connection (what happened before was not pushed) and at each message about the address or
    a new block. Each connection tries the WebSockets of `explorer`'s sources in order: one that
    fails, or is lost before any message, goes to the next at once (the merchant's Bitcoin
    node's to mempool.space's with `fallback` "mempool", SPEC.md §31), a node's failure
    remembered for the call. A lost socket connects again after 1 s, doubled at each failure (of
    the last one tried) up to 30 s: each connection starts a check, so a server that accepts and
    drops every connection is connected to less and less often; one that received a message sets
    the delay back to 1 s (SPEC.md §31, Waiting for an on-chain payment). What it pushes is never
    trusted: it only says when to check. `sleep`: the wait between two connections, for tests."""
    delay = RECONNECT_FIRST_SECONDS
    while True:
        for source in explorer.sources():
            if await _connection(
                connect, source.web_socket, address, on_change, open_timeout=open_timeout
            ):
                # The explorer answered: the next loss waits 1 s again.
                delay = RECONNECT_FIRST_SECONDS
                break
            if source.name == "node":
                explorer.node_failed()
        await sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX_SECONDS)


_UNSET: Final[Any] = object()


def _final(result: Part) -> bool:
    return isinstance(result, LightningResult) and result.state != "open"


def _paid(result: Part) -> bool:
    return isinstance(result, LightningResult) and result.state == "paid"


class _AddressWait:
    """`wait_for_payment` of an invoice with an address: each status combines the last result
    of each part."""

    def __init__(
        self,
        engine: Engine,
        stored: InvoiceToCheck,
        interval: float,
        on_status: OnStatus | None,
    ) -> None:
        self._warnings = _Warnings(engine.settings.logger)
        self._failing = _Failing(engine.settings.logger)
        self._engine = engine.logging_to(self._warnings)
        self._stored = stored
        self._interval = interval
        self._on_status = on_status
        own = payable_of(stored)
        self._own = own
        # The last result of each part; _UNSET until it was checked once.
        self._lightning: Part | None = _UNSET if own is not None else None
        self._onchain: OnchainResult | SattlemeError = _UNSET
        self._rests: RestCheck | None = None
        # Full checks started, the last one that ended, and the first one a status waits for.
        self._started = 0
        self._ended = 0
        self._wait_for = 0
        # The wait is one call (SPEC.md §31, "in the same call"): its checks and its WebSocket
        # share when the merchant's Bitcoin node failed, and each check that read the channel
        # says which node it names (`_on_node`).
        self._call = Call(on_node=self._on_node)
        # The explorer's WebSocket: of the node the last channel read names, opened once a check
        # read the channel (before, whether mempool.space may be asked is not known), opened
        # again when the channel names another node; every watch started, ended with the wait.
        self._watching: str | None = None
        self._watch: asyncio.Task[None] | None = None
        self._watches: list[asyncio.Task[None]] = []
        self._connect: WebSocketConnect | None = None
        self._open_timeout = 0.0
        self._wake = asyncio.Event()
        self._reporting = asyncio.Lock()
        self._result: asyncio.Future[PaymentStatus] = asyncio.get_running_loop().create_future()

    async def run(self, connect: WebSocketConnect, open_timeout: float) -> PaymentStatus:
        address = self._stored.onchain_address
        if address is None:  # pragma: no cover - the caller's choice
            raise ValueError("an invoice with an address")
        self._connect, self._open_timeout = connect, open_timeout
        tasks = [
            asyncio.ensure_future(self._full_checks()),
            asyncio.ensure_future(self._lightning_checks()),
        ]
        for task in tasks:
            task.add_done_callback(self._stopped)
        try:
            return await self._result
        finally:
            await cancel([*tasks, *self._watches])

    def _on_node(self, node: BitcoinNode | None) -> None:
        """A check read the channel, which names `node` (None: mempool.space): the WebSocket
        watched is that explorer's, with its fallback."""
        key = "mempool" if node is None else f"{node.fallback} {node.url}"
        address = self._stored.onchain_address
        if key == self._watching or self._result.done() or self._connect is None or not address:
            return
        if self._watch is not None:
            # Ended with the wait at the latest (`run`).
            self._watch.cancel()
        self._watching = key
        explorer = CallExplorer(self._engine.http(), node, self._call.failures, self._engine.now)
        watch = asyncio.ensure_future(
            watch_address(
                self._connect,
                explorer,
                address,
                self._wake.set,
                open_timeout=self._open_timeout,
            )
        )
        watch.add_done_callback(self._stopped)
        self._watch = watch
        self._watches = [*(w for w in self._watches if not w.done()), watch]

    def _stopped(self, task: asyncio.Task[Any]) -> None:
        """A loop that ends on its own ends the wait with its error (a bug, `on_status`'s)."""
        if task.cancelled():
            return
        error = task.exception()
        if not self._result.done():
            if error is None:  # pragma: no cover - the loops never return
                error = RuntimeError("a loop of wait_for_payment ended")
            self._result.set_exception(error)

    async def _full_checks(self) -> None:
        while True:
            self._wake.clear()
            self._started += 1
            mine = self._started
            parts = await self._engine.parts(self._stored, call=self._call)
            self._warnings.check_ended()
            self._ended = mine
            if parts.onchain is not None:
                self._onchain = parts.onchain
            self._rests = parts.rests
            if parts.lightning is not None:
                self._lightning = parts.lightning
            await self._report()
            # Every 60 s at least: a socket may go quiet without closing.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(EXPLORER_CHECK_SECONDS):
                    await self._wake.wait()

    def _lightning_pending(self) -> bool:
        """Whether a Lightning invoice (the order's, or one of its rest) may still change."""
        lightning = self._lightning
        if lightning is _UNSET or (lightning is not None and not _final(lightning)):
            return True
        rests = self._rests
        return rests is not None and any(not _final(result) for result in rests.results)

    async def _lightning_checks(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            if not self._lightning_pending():
                continue
            http = self._engine.http()
            known = self._rests
            lightning = self._lightning
            checks: list[asyncio.Task[Part]] = []
            own_check = None
            if self._own is not None and (
                lightning is _UNSET or (lightning is not None and not _final(lightning))
            ):
                own_check = self._engine.lightning_check(http, self._own)
                checks.append(own_check)
            rest_checks: list[asyncio.Task[Part] | None] = []
            if known is not None:
                for invoice, result in zip(known.invoices, known.results, strict=True):
                    check = (
                        None
                        if _final(result)
                        else self._engine.lightning_check(http, payable_of_rest(invoice))
                    )
                    rest_checks.append(check)
                    if check is not None:
                        checks.append(check)
            try:
                own = None if own_check is None else await own_check
                results = [None if check is None else await check for check in rest_checks]
            finally:
                await cancel(checks)
            if own is not None:
                self._lightning = own
            newly_paid = False
            # A full check may have found other rest invoices meanwhile: its results stand.
            if known is not None and self._rests is known:
                merged = tuple(
                    old if new is None else new
                    for old, new in zip(known.results, results, strict=True)
                )
                newly_paid = any(
                    _paid(new) and not _paid(old)
                    for old, new in zip(known.results, merged, strict=True)
                )
                self._rests = dataclasses.replace(known, results=merged)
            if newly_paid:
                # What is due on-chain changed: a full check tells the status, none before it.
                self._wait_for = self._started + 1
                self._wake.set()
            else:
                await self._report()

    async def _report(self) -> None:
        async with self._reporting:
            if self._result.done():
                return
            if self._lightning is _UNSET or self._onchain is _UNSET:
                return
            if self._ended < self._wait_for:
                return
            parts = Parts(lightning=self._lightning, onchain=self._onchain, rests=self._rests)
            try:
                status = status_of(self._stored, parts, self._engine.now())
            except NetworkError as error:
                self._failing.failed(error)
                return
            except SattlemeError as error:
                self._result.set_exception(error)
                return
            self._failing.worked()
            await _hand(self._on_status, status)
            if status.final and not self._result.done():
                self._result.set_result(status)


async def wait_for_payment(
    engine: Engine,
    stored: InvoiceToCheck,
    *,
    interval: float,
    on_status: OnStatus | None,
    connect: WebSocketConnect,
    open_timeout: float,
) -> PaymentStatus:
    """Check the payment until it is `paid` or `expired`."""
    if stored.onchain_address is None:
        return await wait_lightning(engine, stored, interval, on_status)
    return await _AddressWait(engine, stored, interval, on_status).run(connect, open_timeout)
