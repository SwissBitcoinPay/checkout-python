"""The client (SPEC.md §4): `AsyncClient` for asyncio (FastAPI, any ASGI server), `Client` for
synchronous code (Django, Flask, any WSGI server, scripts, task queues).

Both do the same, with the same options and the same errors: `Client` runs each call of an
`AsyncClient` in an event loop of its own, in the calling thread. It never runs inside an event
loop that is already running (it refuses, rather than block that loop): there, use `AsyncClient`.
A client keeps nothing between calls but its options, and may serve every thread of a process.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Final, NoReturn, TypeVar

from ._constants import (
    INTERVAL_MAX_MS,
    INTERVAL_MIN_MS,
    SAFE_INTEGER_MAX,
    TIMEOUT_DEFAULT_MS,
    TIMEOUT_MAX_MS,
    TIMEOUT_MIN_MS,
)
from ._crypto import Secret
from ._engine import Engine, Settings
from ._http import DefaultTransport, HttpTransport
from ._key import parse_key
from ._log import Logger
from ._order import check_order, check_ref
from ._rates import (
    ABSENT,
    DEFAULT_SOURCES,
    Absent,
    RateSource,
    RateSourceOption,
    parse_rate_sources,
)
from ._relay import WebSocketConnect, default_connect
from ._stored import invoice_to_check
from ._waiting import OnStatus, wait_for_payment
from .errors import InvalidOptionError
from .models import DeclaredInvoice, Invoice, Merchant, PaymentStatus, Price

__all__ = ["AsyncClient", "Client", "InvoiceLike"]

T = TypeVar("T")

InvoiceLike = Invoice | DeclaredInvoice | Mapping[str, Any]
"""An invoice to check: an Invoice, a DeclaredInvoice, or the JSON stored of one (SPEC.md §3)."""

_LOGGER: Final = logging.getLogger("sattleme_checkout")
_HEADER_SAFE: Final = frozenset(chr(code) for code in range(0x21, 0x7F))


def _milliseconds(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidOptionError(f"{name}: an integer from {low} to {high}", param=name)
    return value


_INVALID: Final = Secret(b"")
"""What `_coingecko_key` gives for a value that is no key."""


def _coingecko_key(value: object) -> Secret | None:
    """CoinGecko's key (SPEC.md §4: a string of visible ASCII), held as a secret: its ASCII,
    which only its request's headers reveal (SPEC.md §26: sent to CoinGecko only); None without
    one; `_INVALID` for anything else (its error raised in the options' order). No frame holds
    its text once this returns, a traceback's included."""
    if value is None:
        return None
    held = _INVALID
    if isinstance(value, str) and value and set(value) <= _HEADER_SAFE:
        held = Secret(value.encode("ascii"))
    del value
    return held


def _function(value: object, name: str) -> None:
    if not callable(value):
        raise InvalidOptionError(f"{name}: a function", param=name)


def _clock(clock: Callable[[], float]) -> Callable[[], int]:
    def now() -> int:
        value: object = clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or not 0 <= value <= SAFE_INTEGER_MAX
        ):
            raise InvalidOptionError("now: a clock giving Unix seconds", param="now")
        return math.floor(value)

    return now


def _randomness(source: Callable[[int], bytes]) -> Callable[[int], bytes]:
    def random(size: int) -> bytes:
        value: object = source(size)
        if not isinstance(value, bytes) or len(value) != size:
            raise InvalidOptionError(f"random: {size} random bytes", param="random")
        return value

    return random


class AsyncClient:
    """A shop's server for one sattle.me integration, for asyncio.

    `key`: the integration's secret key (`sattleme_ck_…`), from the environment or a vault; never in
    code, a repository, a log or the browser. Every other option is optional:

    - `rate_source`: where prices in a currency get their rate: `"coingecko"`, `"mempool"`,
      `"opennode"`, `"coinbase"`, `"kraken"`, a function of your own (a custom source), or a
      list of them asked in order. `DEFAULT_RATE_SOURCES` when left out;
    - `coingecko_api_key`: CoinGecko's Demo API key, sent to CoinGecko only;
    - `relay_timeout_ms`, `http_timeout_ms`: per relay request (connection included), per HTTP
      request (body included: the Lightning address, the rate sources, the explorer, which is
      mempool.space or the merchant's Bitcoin node, BlockCypher): 100 to 60 000 ms, 10 000 by
      default;
    - `http`: the HTTP transport (an `HttpTransport`), for tests and proxies; the default one
      asks https URLs, and http ones of a merchant's Bitcoin node on a local network alone;
    - `websocket`: what opens a WebSocket (`async (url) -> connection`): the relays' (`wss://`)
      and the explorer's (mempool.space's, or the merchant's Bitcoin node's, `ws://` or
      `wss://`), for tests and proxies;
    - `logger`: where warnings go (`logging.getLogger("sattleme_checkout")` by default);
    - `now`, `random`: the clock (Unix seconds) and the random bytes, for tests only.

    An invalid option raises `InvalidOptionError` naming it, before any request; an invalid key,
    `InvalidKeyError`.
    """

    __slots__ = ("_engine",)

    def __init__(
        self,
        key: str,
        *,
        rate_source: RateSourceOption | Absent = ABSENT,
        coingecko_api_key: str | None = None,
        relay_timeout_ms: int = TIMEOUT_DEFAULT_MS,
        http_timeout_ms: int = TIMEOUT_DEFAULT_MS,
        http: HttpTransport | None = None,
        websocket: WebSocketConnect | None = None,
        logger: Logger | None = None,
        now: Callable[[], float] | None = None,
        random: Callable[[int], bytes] | None = None,
    ) -> None:
        try:
            integration_key = parse_key(key)
        finally:
            # No frame of a traceback holds the key's text (error trackers capture locals).
            del key
        try:
            # A secret from now on, checked in its turn below: CoinGecko's key.
            coingecko_key = _coingecko_key(coingecko_api_key)
        finally:
            del coingecko_api_key
        rate_sources: tuple[RateSource, ...]
        if rate_source is ABSENT:
            rate_sources = DEFAULT_SOURCES
        else:
            parsed = parse_rate_sources(rate_source)
            if parsed is None:
                raise InvalidOptionError(
                    "rate_source: a source's name (coingecko, mempool, opennode, coinbase, "
                    "kraken), a function, or a list of them, each name once",
                    param="rate_source",
                )
            rate_sources = parsed
        if coingecko_key is _INVALID:
            raise InvalidOptionError(
                "coingecko_api_key: a string of visible ASCII (U+0021 to U+007E)",
                param="coingecko_api_key",
            )
        relay_timeout = _milliseconds(
            relay_timeout_ms, "relay_timeout_ms", TIMEOUT_MIN_MS, TIMEOUT_MAX_MS
        )
        http_timeout = _milliseconds(
            http_timeout_ms, "http_timeout_ms", TIMEOUT_MIN_MS, TIMEOUT_MAX_MS
        )
        if http is not None:
            _function(http, "http")
        if websocket is not None:
            _function(websocket, "websocket")
        if logger is not None and not callable(getattr(logger, "warning", None)):
            raise InvalidOptionError("logger: a logging.Logger", param="logger")
        if now is not None:
            _function(now, "now")
        if random is not None:
            _function(random, "random")
        self._engine = Engine(
            Settings(
                key=integration_key,
                rate_sources=rate_sources,
                coingecko_api_key=coingecko_key,
                transport=DefaultTransport() if http is None else http,
                connect=default_connect if websocket is None else websocket,
                relay_timeout_ms=relay_timeout,
                http_timeout_ms=http_timeout,
                logger=_LOGGER if logger is None else logger,
                now=_clock(time.time if now is None else now),
                random=_randomness(os.urandom if random is None else random),
            )
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    def __reduce__(self) -> NoReturn:
        raise TypeError("a client holds the integration key: it is never serialized")

    async def merchant(self) -> Merchant:
        """The merchant: its business's name, the bounds of its Lightning address in sats, and
        how many deposit addresses the integration's lot holds. Never its Lightning address.
        `RevokedError` when the merchant removed the integration."""
        return await self._engine.merchant()

    async def create_invoice(
        self,
        *,
        ref: str,
        sats: int | str | None = None,
        fiat: Price | None = None,
        rate_source: RateSourceOption | Absent = ABSENT,
        duration_seconds: int = 900,
        note: str | None = None,
        checkout: bool = False,
        redirect_url: str | None = None,
    ) -> Invoice:
        """The invoice of an order, declared in the merchant's registry: store it with the
        order (`invoice.to_json()`), then show it. Exactly one of `sats` and `fiat`.

        A Lightning invoice when the amount is within the merchant's Lightning address's bounds,
        a deposit address of the merchant's wallet from 1 000 sats, or both. One order, one
        invoice at a time: `InvoicePendingError` while an earlier invoice of `ref` may still be
        paid, `OrderPaidError` when one was paid. With `checkout=True`, a checkout link too
        (`invoice.checkout_url`), which sends the customer to `redirect_url` once paid. Lock the
        order in your database around this call when several processes may make its invoice.
        """
        order = check_order(
            ref=ref,
            sats=sats,
            fiat=fiat,
            rate_source=rate_source,
            duration_seconds=duration_seconds,
            note=note,
            checkout=checkout,
            redirect_url=redirect_url,
            client_rate_sources=self._engine.settings.rate_sources,
        )
        return await self._engine.create_invoice(order)

    async def get_status(self, invoice: InvoiceLike) -> PaymentStatus:
        """Where the payment of `invoice` is, checked now: `open`, `unconfirmed`, `paid` or
        `expired`. Mark the order paid only on a `paid` status of the invoice it holds. Its
        on-chain part is read from the explorer (mempool.space, or the merchant's Bitcoin node
        with its fallback): `NetworkError` (retry) when it does not answer, never `expired` for
        want of an answer, the merchant's node with `fallback` "none" included."""
        stored = invoice_to_check(invoice)
        return await self._engine.status(stored)

    async def wait_for_payment(
        self,
        invoice: InvoiceLike,
        *,
        interval_ms: int = 2000,
        on_status: OnStatus | None = None,
    ) -> PaymentStatus:
        """Check the payment until it is `paid` or `expired`, every `interval_ms` (250 to
        60 000), handing each status to `on_status` (a function, or an async one). `network`
        errors are retried; any other error ends it. With an address, the explorer's WebSocket
        (mempool.space's, or the merchant's Bitcoin node's with its fallback) says when to check
        its on-chain part. An on-chain payment takes about 30 minutes to be
        `paid` once `unconfirmed`: bound the wait (`asyncio.timeout`), then check again later
        with `get_status`."""
        interval = _milliseconds(interval_ms, "interval_ms", INTERVAL_MIN_MS, INTERVAL_MAX_MS)
        if on_status is not None:
            _function(on_status, "on_status")
        stored = invoice_to_check(invoice)
        settings = self._engine.settings
        return await wait_for_payment(
            self._engine,
            stored,
            interval=interval / 1000,
            on_status=on_status,
            connect=settings.connect,
            open_timeout=settings.relay_timeout_ms / 1000,
        )

    async def find_invoices(self, ref: str) -> list[DeclaredInvoice]:
        """The invoices declared for the order `ref` in the merchant's registry, oldest first:
        to find an order's invoice again after a failure."""
        return await self._engine.find_invoices(check_ref(ref))


class _NoWaitExecutor(ThreadPoolExecutor):
    """The worker threads of one synchronous call: the call's end never waits for a thread
    still blocked (a name resolution, a custom rate source), which ends on its own."""

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:  # noqa: FBT001, FBT002
        super().shutdown(wait=False, cancel_futures=True)


def _seconds(timeout: object) -> float | None:
    if timeout is None:
        return None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise InvalidOptionError("timeout: seconds, above 0", param="timeout")
    return float(timeout)


def _run(call: Callable[[], Coroutine[Any, Any, T]], timeout: object) -> T:
    """Run one call of the asynchronous client in an event loop of its own, in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "sattleme_checkout.Client is synchronous and this thread runs an event loop: "
            "use AsyncClient (await its methods), or call Client from a worker thread "
            "(asyncio.to_thread)"
        )
    seconds = _seconds(timeout)

    async def bounded() -> T:
        if seconds is None:
            return await call()
        async with asyncio.timeout(seconds):
            return await call()

    # A loop factory: the Runner then leaves the thread's current event loop as it was (without
    # one, it sets its own and clears it when done, as `asyncio.run` does). The package only
    # asks for the running loop.
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        runner.get_loop().set_default_executor(
            _NoWaitExecutor(thread_name_prefix="sattleme-checkout")
        )
        return runner.run(bounded())


class Client:
    """A shop's server for one sattle.me integration, for synchronous code.

    The options of `AsyncClient`, and the same methods, which block until they are done. Each
    takes a `timeout` (seconds; none by default): past it, the call stops its requests and
    raises `TimeoutError`.
    """

    __slots__ = ("_client",)

    def __init__(
        self,
        key: str,
        *,
        rate_source: RateSourceOption | Absent = ABSENT,
        coingecko_api_key: str | None = None,
        relay_timeout_ms: int = TIMEOUT_DEFAULT_MS,
        http_timeout_ms: int = TIMEOUT_DEFAULT_MS,
        http: HttpTransport | None = None,
        websocket: WebSocketConnect | None = None,
        logger: Logger | None = None,
        now: Callable[[], float] | None = None,
        random: Callable[[int], bytes] | None = None,
    ) -> None:
        try:
            self._client = AsyncClient(
                key,
                rate_source=rate_source,
                coingecko_api_key=coingecko_api_key,
                relay_timeout_ms=relay_timeout_ms,
                http_timeout_ms=http_timeout_ms,
                http=http,
                websocket=websocket,
                logger=logger,
                now=now,
                random=random,
            )
        finally:
            # No frame of a traceback holds either key's text.
            del key, coingecko_api_key

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    def __reduce__(self) -> NoReturn:
        raise TypeError("a client holds the integration key: it is never serialized")

    def merchant(self, *, timeout: float | None = None) -> Merchant:
        """As `AsyncClient.merchant`."""
        return _run(self._client.merchant, timeout)

    def create_invoice(
        self,
        *,
        ref: str,
        sats: int | str | None = None,
        fiat: Price | None = None,
        rate_source: RateSourceOption | Absent = ABSENT,
        duration_seconds: int = 900,
        note: str | None = None,
        checkout: bool = False,
        redirect_url: str | None = None,
        timeout: float | None = None,
    ) -> Invoice:
        """As `AsyncClient.create_invoice`."""
        return _run(
            lambda: self._client.create_invoice(
                ref=ref,
                sats=sats,
                fiat=fiat,
                rate_source=rate_source,
                duration_seconds=duration_seconds,
                note=note,
                checkout=checkout,
                redirect_url=redirect_url,
            ),
            timeout,
        )

    def get_status(self, invoice: InvoiceLike, *, timeout: float | None = None) -> PaymentStatus:
        """As `AsyncClient.get_status`."""
        return _run(lambda: self._client.get_status(invoice), timeout)

    def wait_for_payment(
        self,
        invoice: InvoiceLike,
        *,
        interval_ms: int = 2000,
        on_status: OnStatus | None = None,
        timeout: float | None = None,
    ) -> PaymentStatus:
        """As `AsyncClient.wait_for_payment`; `on_status` is called in this thread."""
        return _run(
            lambda: self._client.wait_for_payment(
                invoice, interval_ms=interval_ms, on_status=on_status
            ),
            timeout,
        )

    def find_invoices(self, ref: str, *, timeout: float | None = None) -> list[DeclaredInvoice]:
        """As `AsyncClient.find_invoices`."""
        return _run(lambda: self._client.find_invoices(ref), timeout)
