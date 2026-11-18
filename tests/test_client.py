"""The client's options (SPEC.md §4), the synchronous client, and the key as a secret (§27)."""

from __future__ import annotations

import asyncio
import logging
import pickle
import sys
import threading
import time
import traceback
from typing import Any

import pytest

from fakes.chain import tx, txid
from fakes.world import World
from sattleme_checkout import (
    DEFAULT_RATE_SOURCES,
    SPEC_VERSION,
    AsyncClient,
    Client,
    InvalidInvoiceError,
    InvalidKeyError,
    InvalidOptionError,
    InvalidOrderError,
    InvoicePendingError,
    NetworkError,
    Price,
    SattlemeError,
    __version__,
)
from spec_vectors import vector

RATES: Any = vector("rate-sources.json")
APP: Any = vector("app/app.json")
KEY = APP["secretKey"]


def test_versions() -> None:
    import json
    import pathlib
    import tomllib

    root = pathlib.Path(__file__).parent.parent
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    assert __version__ == project["version"]
    assert json.loads((root / "spec" / "PIN.json").read_text())["version"] == SPEC_VERSION
    assert tuple(RATES["default"]) == DEFAULT_RATE_SOURCES


@pytest.mark.parametrize("case", RATES["values"], ids=lambda c: c["name"])
def test_a_clients_rate_source(case: dict[str, Any]) -> None:
    options = {} if "rateSource" not in case else {"rate_source": case["rateSource"]}
    expected = case["expected"]
    if "error" in expected:
        with pytest.raises(InvalidOptionError) as raised:
            AsyncClient(KEY, **options)
        assert raised.value.param == "rate_source"
        with pytest.raises(InvalidOptionError):
            Client(KEY, **options)
    else:
        client = AsyncClient(KEY, **options)
        assert list(client._engine.settings.rate_sources) == expected["sources"]


@pytest.mark.parametrize(
    "case",
    [case for case in RATES["values"] if "rateSource" in case and "error" in case["expected"]],
    ids=lambda c: c["name"],
)
async def test_an_orders_rate_source(case: dict[str, Any]) -> None:
    """rate-sources.json, `values`, each one refused: refused as an order's `rate_source` too,
    before any request (the valid ones: `test_a_clients_rate_source`)."""
    client = AsyncClient(KEY)
    with pytest.raises(InvalidOrderError) as raised:
        await client.create_invoice(
            ref="ORD-1", fiat=Price("1", "CHF"), rate_source=case["rateSource"]
        )
    assert raised.value.param == "rate_source"


@pytest.mark.parametrize(
    ("options", "param"),
    [
        ({"coingecko_api_key": 1}, "coingecko_api_key"),
        ({"coingecko_api_key": ""}, "coingecko_api_key"),
        ({"coingecko_api_key": "CG key"}, "coingecko_api_key"),
        ({"coingecko_api_key": "CG\x7f"}, "coingecko_api_key"),
        ({"coingecko_api_key": "CG-\u00e9"}, "coingecko_api_key"),
        ({"coingecko_api_key": "CG-k\r\nX-Other: 1"}, "coingecko_api_key"),
        ({"relay_timeout_ms": 99}, "relay_timeout_ms"),
        ({"relay_timeout_ms": 60_001}, "relay_timeout_ms"),
        ({"relay_timeout_ms": 1000.0}, "relay_timeout_ms"),
        ({"relay_timeout_ms": "1000"}, "relay_timeout_ms"),
        ({"relay_timeout_ms": False}, "relay_timeout_ms"),
        ({"http_timeout_ms": 0}, "http_timeout_ms"),
        ({"http_timeout_ms": None}, "http_timeout_ms"),
        ({"http": "https://proxy.example"}, "http"),
        ({"websocket": 1}, "websocket"),
        ({"logger": print}, "logger"),
        ({"now": 1791378000}, "now"),
        ({"random": b"0" * 32}, "random"),
    ],
)
def test_invalid_options(options: dict[str, Any], param: str) -> None:
    with pytest.raises(InvalidOptionError) as raised:
        AsyncClient(KEY, **options)
    assert raised.value.param == param
    assert raised.value.code == "invalid_option"
    assert raised.value.retryable is False


def test_valid_options() -> None:
    AsyncClient(
        KEY,
        rate_source=["kraken", lambda currency: "1"],
        coingecko_api_key="CG-abc123",
        relay_timeout_ms=100,
        http_timeout_ms=60_000,
        logger=logging.LoggerAdapter(logging.getLogger("x"), {}),
    )


def test_an_invalid_key() -> None:
    text = KEY[:-1]
    with pytest.raises(InvalidKeyError) as raised:
        AsyncClient(text)
    assert text not in str(raised.value)
    with pytest.raises(InvalidKeyError):
        Client(" " + KEY)
    with pytest.raises(InvalidKeyError):
        AsyncClient(None)  # type: ignore[arg-type]


def _frames_hold(error: BaseException, secret: str) -> bool:
    """Whether a frame of the package in the traceback (as an error tracker captures it, with
    its local variables) holds `secret`."""
    seen: BaseException | None = error
    while seen is not None:
        for frame, _line in traceback.walk_tb(seen.__traceback__):
            if "sattleme_checkout" not in frame.f_code.co_filename:
                continue
            for value in frame.f_locals.values():
                if isinstance(value, str) and secret in value:
                    return True
        seen = seen.__cause__ or seen.__context__
    return False


def test_the_key_never_shows() -> None:
    secret_part = KEY.split(".")[1]
    for client in (AsyncClient(KEY), Client(KEY)):
        assert secret_part not in repr(client)
        assert secret_part not in str(client)
        with pytest.raises(TypeError):
            pickle.dumps(client)
    # An invalid key, or an invalid option, leaves no frame holding the key's text.
    for make in (
        lambda: AsyncClient(KEY + "x"),
        lambda: Client(KEY + "x"),
        lambda: AsyncClient(KEY, relay_timeout_ms=0),
        lambda: Client(KEY, relay_timeout_ms=0),
    ):
        with pytest.raises(SattlemeError) as raised:
            make()
        assert not _frames_hold(raised.value, secret_part)


def _frames_show(error: BaseException, needles: tuple[str, ...]) -> list[str]:
    """The locals of the package's frames, in `error`'s traceback, its cause's and its context's,
    whose representation shows one of `needles`."""
    found: list[str] = []
    seen: set[int] = set()
    todo: list[BaseException | None] = [error]
    while todo:
        current = todo.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for frame, _line in traceback.walk_tb(current.__traceback__):
            if "sattleme_checkout" not in frame.f_code.co_filename:
                continue
            for name, value in frame.f_locals.items():
                if any(needle in repr(value) for needle in needles):
                    found.append(f"{frame.f_code.co_name}: {name}")
        todo += [current.__cause__, current.__context__]
    return found


@pytest.mark.parametrize(
    ("words", "lot"), [(50_000, 0), (50_000, 3), (70_000, 0), (70_000, 3), (40_186, 3)]
)
async def test_an_invoice_too_large_shows_no_key_in_frames(words: int, lot: int) -> None:
    """SPEC.md §27: no key derived from the integration key in an exception's data, nor the
    tolerance in an error. An invoice too large for its declaration (its outer record at 50 000,
    its inner one at 70 000), or of 40 550 characters with on-chain, too large for its lease
    alone, raises with no frame holding the sale key or the tolerance."""
    world = await World.start(lot=lot, tolerance=500)
    try:
        world.lightning.description_words = words
        key = world.sale("ORD-1").key.reveal().hex()
        with pytest.raises(InvalidInvoiceError, match="too large") as raised:
            await world.client().create_invoice(ref="ORD-1", sats=49_000)
        assert _frames_show(raised.value, (key, "tolerance")) == []
        assert world.received() == []
    finally:
        await world.close()


COINGECKO_KEY = "CG-a-demo-key-of-this-test"


def _shows(error: BaseException, needle: str) -> list[str]:
    """Where `error` shows `needle`: its message, its representation, or a local of one of the
    package's frames along its traceback, its cause's and its context's."""
    found = [text for text in (str(error), repr(error)) if needle in text]
    return found + _frames_show(error, (needle,))


class _Opaque:
    """A transport with the default representation, as `DefaultTransport` has: the fake it
    wraps records the headers it was given (a test's own frames hold what they hold)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def __call__(self, url: str, **options: Any) -> Any:
        return await self._inner(url, **options)


def test_the_coingecko_key_never_shows_in_an_option_error() -> None:
    """CoinGecko's key is held as a secret from the client's first line on: an invalid option
    after it, or the key itself invalid, leaves no frame of a traceback holding its text."""
    for make in (
        lambda: AsyncClient(KEY, coingecko_api_key=COINGECKO_KEY, relay_timeout_ms=0),
        lambda: Client(KEY, coingecko_api_key=COINGECKO_KEY, relay_timeout_ms=0),
        lambda: AsyncClient(KEY, rate_source="nowhere", coingecko_api_key=COINGECKO_KEY),
        lambda: AsyncClient(KEY, coingecko_api_key=COINGECKO_KEY + " "),
        lambda: Client(KEY, coingecko_api_key=COINGECKO_KEY + "\n"),
    ):
        with pytest.raises(InvalidOptionError) as raised:
            make()
        assert _shows(raised.value, COINGECKO_KEY) == []
    for client in (
        AsyncClient(KEY, coingecko_api_key=COINGECKO_KEY),
        Client(KEY, coingecko_api_key=COINGECKO_KEY),
    ):
        assert COINGECKO_KEY not in repr(client)


@pytest.mark.parametrize(
    "failure",
    [
        "every source answers 500",
        "coingecko alone answers 500",
        "coingecko alone is down",
        "coingecko alone answers no JSON object",
    ],
)
async def test_the_coingecko_key_never_shows_in_a_rate_error(failure: str) -> None:
    """CoinGecko's key, held as a secret, is revealed only in the headers made at the
    transport's call: whichever source's error the call raises (the last one's, CoinGecko's own
    status, a transport that fails, an answer that is no JSON object), no frame of the package
    along its traceback holds it."""
    world = await World.start(lot=0)
    try:
        sources: Any = DEFAULT_RATE_SOURCES if failure.startswith("every") else "coingecko"
        if failure.startswith("every"):
            world.rates.status = dict.fromkeys(DEFAULT_RATE_SOURCES, 500)
        elif failure.endswith("500"):
            world.rates.status = {"coingecko": 500}
        elif failure.endswith("down"):
            world.http.down = {"https://api.coingecko.com/"}
        else:
            world.http.route("https://api.coingecko.com/", lambda _url: (200, []))
        client = world.client(
            coingecko_api_key=COINGECKO_KEY, rate_source=sources, http=_Opaque(world.http)
        )
        with pytest.raises(SattlemeError) as raised:
            await client.create_invoice(ref="ORD-1", fiat=Price("10", "CHF"))
        assert raised.value.code in ("network", "rate")
        assert _shows(raised.value, COINGECKO_KEY) == []
        # It went to CoinGecko, and to CoinGecko only.
        for asked in world.http.asked:
            sent = asked.headers.get("x-cg-demo-api-key")
            coingecko = asked.url.startswith("https://api.coingecko.com/")
            assert sent == (COINGECKO_KEY if coingecko else None)
        assert world.http.urls("https://api.coingecko.com/") != []
    finally:
        await world.close()


async def test_a_check_that_fails_shows_no_tolerance_in_frames() -> None:
    """SPEC.md §27: the tolerance is never in an error. A check whose explorer fails raises with
    no frame holding it: neither as a number nor in a representation (a task's result)."""
    world = await World.start(lot=3, tolerance=731)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-1", sats=49_000)
        world.explorer.failing = True
        with pytest.raises(NetworkError) as raised:
            await client.get_status(invoice)
        assert _frames_show(raised.value, ("=731",)) == []
        assert _frames_hold_number(raised.value, 731) == []
    finally:
        await world.close()


async def test_a_check_cut_short_shows_neither_the_tolerance_nor_what_it_lets_through() -> None:
    """SPEC.md §27. A check cut by the caller's timeout wherever it is (the explorer, the relays,
    the Lightning address asked): the least accepted (49 000 less 737 basis points: 45 389) would
    give the tolerance away."""
    world = await World.start(lot=3, tolerance=737)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-1", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        paying = txid()
        world.explorer.add(address, tx(paying, {address: 46_000}))
        world.explorer.confirm(paying, world.explorer.height - 2)
        world.http.delay = 0.05
        cut: list[TimeoutError] = []
        for timeout in (0.04, 0.06, 0.08, 0.1, 0.12, 0.15, 0.2):
            try:
                async with asyncio.timeout(timeout):
                    await client.get_status(invoice)
            except TimeoutError as error:
                cut.append(error)
        assert cut
        for timed_out in cut:
            assert _frames_show(timed_out, ("=737", "=45389")) == []
            for number in (737, 45_389):
                assert _frames_hold_number(timed_out, number) == []
    finally:
        await world.close()


def _frames_hold_number(error: BaseException, number: int) -> list[str]:
    found: list[str] = []
    seen: set[int] = set()
    todo: list[BaseException | None] = [error]
    while todo:
        current = todo.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for frame, _line in traceback.walk_tb(current.__traceback__):
            if "sattleme_checkout" not in frame.f_code.co_filename:
                continue
            for name, value in frame.f_locals.items():
                if type(value) is int and value == number:
                    found.append(f"{frame.f_code.co_name}: {name}")
        todo += [current.__cause__, current.__context__]
    return found


def test_errors_cross_processes() -> None:
    error = InvalidOrderError("ref: invalid", param="ref")
    copied = pickle.loads(pickle.dumps(error))  # noqa: S301 - our own error
    assert (type(copied), copied.message, copied.param) == (
        InvalidOrderError,
        "ref: invalid",
        "ref",
    )


async def test_the_synchronous_client_refuses_a_running_loop() -> None:
    client = Client(KEY)
    with pytest.raises(RuntimeError, match="AsyncClient"):
        client.find_invoices("ORD-1")


def _sync(world: World, function: Any) -> Any:
    """Run `function` in a thread with no event loop, as a WSGI worker does."""
    found: dict[str, Any] = {}

    def run() -> None:
        try:
            found["value"] = function()
        except BaseException as error:  # noqa: BLE001 - handed to the test
            found["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    return asyncio.to_thread(thread.join), found


async def test_the_synchronous_client() -> None:
    world = await World.start(lot=0)
    try:
        client = world.sync_client()
        joined, found = _sync(world, lambda: client.create_invoice(ref="ORD-2", sats=21))
        await joined
        invoice = found["value"]
        assert invoice.bolt11 is not None
        joined, found = _sync(world, lambda: client.get_status(invoice.to_json(), timeout=30))
        await joined
        assert found["value"].state == "open"
        joined, found = _sync(world, lambda: client.find_invoices("ORD-2"))
        await joined
        assert [i.payment_hash for i in found["value"]] == [invoice.payment_hash]
        assert invoice.payment_hash is not None
        world.lightning.pay(invoice.payment_hash)
        heard: list[str] = []
        threads: list[int] = []

        def on_status(status: Any) -> None:
            heard.append(status.state)
            threads.append(threading.get_ident())

        def wait() -> Any:
            threads.append(threading.get_ident())
            return client.wait_for_payment(invoice, interval_ms=250, on_status=on_status)

        joined, found = _sync(world, wait)
        await joined
        assert found["value"].state == "paid"
        assert heard == ["paid"]
        assert len(set(threads)) == 1
    finally:
        await world.close()


async def test_the_synchronous_client_keeps_the_threads_event_loop() -> None:
    world = await World.start(lot=0)
    try:
        client = world.sync_client()

        def call() -> Any:
            # Code of the integrator's own that set this thread's event loop, and uses it after.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                client.find_invoices("ORD-9")
                return asyncio.get_event_loop() is loop and not loop.is_closed()
            finally:
                asyncio.set_event_loop(None)
                loop.close()

        joined, found = _sync(world, call)
        await joined
        assert found == {"value": True}
    finally:
        await world.close()


async def test_a_synchronous_call_times_out() -> None:
    world = await World.start(lot=0)
    try:
        world.http.delay = 5
        client = world.sync_client(http_timeout_ms=10_000)
        started = time.monotonic()
        joined, found = _sync(
            world, lambda: client.create_invoice(ref="ORD-3", sats=21, timeout=0.5)
        )
        await joined
        assert isinstance(found["error"], TimeoutError)
        assert not isinstance(found["error"], SattlemeError)
        assert time.monotonic() - started < 3
        joined, found = _sync(world, lambda: client.get_status({}, timeout=0))
        await joined
        assert isinstance(found["error"], InvalidOptionError)
        assert found["error"].param == "timeout"
    finally:
        await world.close()


async def test_calls_for_one_order_run_one_after_the_other_across_threads() -> None:
    world = await World.start(lot=0)
    try:
        client = world.sync_client()
        runs = [_sync(world, lambda: client.create_invoice(ref="ORD-4", sats=21)) for _ in range(3)]
        for joined, _found in runs:
            await joined
        outcomes = [found.get("value", found.get("error")) for _joined, found in runs]
        assert sum(isinstance(o, InvoicePendingError) for o in outcomes) == 2
        assert len(world.lightning.invoices) == 1
    finally:
        await world.close()


async def test_a_cancelled_call_raises_the_cancellation() -> None:
    world = await World.start(lot=0)
    try:
        world.http.delay = 5
        client = world.client()
        call = asyncio.ensure_future(client.create_invoice(ref="ORD-5", sats=21))
        await asyncio.sleep(0.3)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        # Its connections closed.
        await asyncio.sleep(0.1)
    finally:
        await world.close()


async def test_an_invalid_stored_invoice() -> None:
    client = AsyncClient(KEY)
    invalid: list[Any] = [{}, {"sats": "1", "expiresAt": 1}, [], "lnbc1", None]
    for stored in invalid:
        with pytest.raises(InvalidOrderError) as raised:
            await client.get_status(stored)
        assert raised.value.param == "invoice"


def test_the_python_versions() -> None:
    assert sys.version_info >= (3, 11)
