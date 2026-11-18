"""Waiting for a payment (SPEC.md §23, §31 Waiting for an on-chain payment): the Lightning part
every interval, the on-chain part when mempool.space's WebSocket says something changed."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest

from fakes.chain import tx, txid
from fakes.http import FakeHttp
from fakes.lightning import FakeInvoice
from fakes.wait import until
from fakes.world import World
from sattleme_checkout import HttpResponse, InvalidInvoiceError, InvalidOptionError, PaymentStatus
from sattleme_checkout._constants import MEMPOOL_WEBSOCKET
from sattleme_checkout._explorer import CallExplorer
from sattleme_checkout._http import Http
from sattleme_checkout._waiting import _Warnings, watch_address


async def test_a_lightning_invoice_is_waited_for() -> None:
    world = await World.start(lot=0)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-1", sats=21)
        heard: list[PaymentStatus] = []
        waiting = asyncio.ensure_future(
            client.wait_for_payment(invoice, interval_ms=250, on_status=heard.append)
        )
        await until(lambda: len(heard) >= 2)
        assert {status.state for status in heard} == {"open"}
        assert invoice.payment_hash is not None
        world.lightning.pay(invoice.payment_hash)
        status = await waiting
        assert status.state == "paid"
        assert heard[-1] == status
        # The socket of the explorer is never opened for an invoice without an address.
        assert world.socket.opened == 0
    finally:
        await world.close()


async def test_network_errors_are_retried_and_others_end_the_wait() -> None:
    world = await World.start(lot=0)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-2", sats=21)
        failing: dict[str, Any] = {"answer": (503, {"error": "busy"})}

        def verify(_invoice: FakeInvoice | None) -> tuple[int, Any]:
            return failing["answer"]  # type: ignore[no-any-return]

        world.lightning.verify_answer = verify
        heard: list[PaymentStatus] = []
        waiting = asyncio.ensure_future(
            client.wait_for_payment(invoice, interval_ms=250, on_status=heard.append)
        )
        await asyncio.sleep(0.8)
        assert not waiting.done()
        assert heard == []
        failing["answer"] = (200, {"status": "OK", "settled": False, "pr": "lnbc1other"})
        with pytest.raises(InvalidInvoiceError):
            await waiting
    finally:
        await world.close()


def _failed_checks(world: World) -> int:
    return sum("A check of the payment failed" in warning for warning in world.warnings)


async def test_a_failure_each_check_meets_is_logged_once_until_it_stops() -> None:
    """SPEC.md §23: a Lightning address that does not answer, check after check, is logged
    once; again only once a check worked and it failed anew."""
    world = await World.start(lot=0)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-9", sats=21)
        verify = invoice.verify_url or ""
        world.http.down.add(verify)
        heard: list[PaymentStatus] = []
        waiting = asyncio.ensure_future(
            client.wait_for_payment(invoice, interval_ms=250, on_status=heard.append)
        )
        await until(lambda: world.http.urls(verify)[3:])
        assert _failed_checks(world) == 1
        world.http.down.clear()
        await until(lambda: heard)
        world.http.down.add(verify)
        asked = len(world.http.urls(verify))
        await until(lambda: world.http.urls(verify)[asked + 3 :])
        assert _failed_checks(world) == 2
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    finally:
        await world.close()


def test_a_warning_of_full_checks_is_logged_again_once_a_check_went_without_it() -> None:
    logged: list[str] = []

    class Logger:
        def warning(self, msg: str, /, *args: object) -> None:
            logged.append(msg % args)

    warnings = _Warnings(Logger())
    for check in ("a", "b"):
        # Met twice in one check, then in the next one: logged once.
        warnings.warning("No rest (%s)", check)
        warnings.warning("No rest (%s)", check)
        warnings.check_ended()
    assert logged == ["No rest (a)"]
    # A check without it: it stopped; met again, logged again.
    warnings.check_ended()
    warnings.warning("No rest (%s)", "c")
    warnings.warning("No witness")
    assert logged == ["No rest (a)", "No rest (c)", "No witness"]


class _ExplorerSocket:
    """The explorer's WebSocket in memory: it answers with `messages`, then is dropped."""

    def __init__(self, messages: list[str]) -> None:
        self.sent: list[Any] = []
        self._messages = messages

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        raise ConnectionError("dropped")

    async def close(self) -> None:
        return None


async def _watched(connections: list[list[str]]) -> tuple[list[float], int, list[Any]]:
    """`watch_address` over connections each answering its messages then dropped: the waits
    before each new connection, how many checks it started, and what the first one was sent.
    Once they are over, the next connection never opens, and the watch is cancelled."""
    sockets = [_ExplorerSocket(list(messages)) for messages in connections]
    opened: list[_ExplorerSocket] = []
    waits: list[float] = []
    checks = 0
    over = asyncio.Event()

    async def connect(url: str) -> Any:
        assert url == MEMPOOL_WEBSOCKET
        if len(opened) == len(sockets):
            over.set()
            await asyncio.Event().wait()
        opened.append(sockets[len(opened)])
        return opened[-1]

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        await asyncio.sleep(0)

    def on_change() -> None:
        nonlocal checks
        checks += 1

    watching = asyncio.ensure_future(
        watch_address(
            connect,
            CallExplorer(Http(FakeHttp(), 1000), None, {}, lambda: 0),
            "bc1p" + "q" * 58,
            on_change,
            open_timeout=60,
            sleep=sleep,
        )
    )
    try:
        async with asyncio.timeout(10):
            await over.wait()
    finally:
        watching.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watching
    return waits, checks, sockets[0].sent


async def test_a_connection_dropped_before_a_message_is_a_failure() -> None:
    """SPEC.md §31, Waiting for an on-chain payment: a lost socket connects again after 1 s,
    doubled at each failure up to 30 s, a connection lost before it received a message being a
    failure. Each connection starts a check: a server that accepts and drops every connection
    has one every second no more, eight in some 91 s."""
    waits, checks, sent = await _watched([[]] * 7)
    assert waits == [1, 2, 4, 8, 16, 30, 30]
    assert checks == 7
    assert sent == [{"action": "want", "data": ["blocks"]}, {"track-address": "bc1p" + "q" * 58}]


async def test_a_connection_that_received_a_message_waits_1_s_again() -> None:
    block = json.dumps({"block": {"height": 900_001}})
    waits, checks, _sent = await _watched([[], [], ['{"address-transactions": []}'], [], [block]])
    assert waits == [1, 2, 1, 2, 1]
    # A check at each connection and at each message about the address or a block.
    assert checks == 5 + 2


async def test_an_async_on_status_is_awaited_and_its_error_ends_the_wait() -> None:
    world = await World.start(lot=0)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-3", sats=21)
        heard: list[str] = []

        async def on_status(status: PaymentStatus) -> None:
            heard.append(status.state)
            if len(heard) == 2:
                raise KeyError("the integrator's own error")

        with pytest.raises(KeyError):
            await client.wait_for_payment(invoice, interval_ms=250, on_status=on_status)
        assert heard == ["open", "open"]
    finally:
        await world.close()


async def test_the_websocket_wakes_the_onchain_check() -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-4", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        heard: list[PaymentStatus] = []
        waiting = asyncio.ensure_future(
            client.wait_for_payment(invoice, interval_ms=60_000, on_status=heard.append)
        )
        await until(lambda: world.socket.tracked() == [address] and heard)
        assert {"action": "want", "data": ["blocks"]} in world.socket.messages
        explorer_asks = len(world.http.urls(f"https://mempool.space/api/address/{address}"))
        # A payment enters the mempool: pushed, it is checked at once.
        paying = txid()
        transaction = tx(paying, {address: 49_000})
        world.explorer.add(address, transaction)
        await world.socket.push({"address-transactions": [transaction]})
        await until(lambda: heard[-1].state == "unconfirmed")
        assert len(world.http.urls(f"https://mempool.space/api/address/{address}")) > explorer_asks
        # A block: one more confirmation, three on the explorer: paid.
        world.explorer.height += 1
        world.explorer.confirm(paying, world.explorer.height - 2)
        await world.socket.push({"block": {"height": world.explorer.height}})
        status = await waiting
        assert status.to_json() == {
            "state": "paid",
            "method": "onchain",
            "received": "49000",
            "txids": [paying],
        }
        await until(lambda: not world.socket.connections)
    finally:
        await world.close()


async def test_nothing_it_started_runs_on_once_it_returned() -> None:
    """SPEC.md §23: when the wait returns, every check it started was stopped and has ended. A
    full check (a block woke it) waits for the explorer's answer while the Lightning invoice is
    paid: the wait returns paid. Then the explorer answers, a part of the amount listed: the
    check it held would publish its witness, ask an invoice of the rest and declare it. It
    stopped with the wait: nothing published, no invoice asked."""
    world = await World.start()
    try:
        gate: asyncio.Event | None = None
        held: list[str] = []
        stopped: list[str] = []

        async def transport(url: str, **options: Any) -> HttpResponse:
            # The explorer's answers of the address's transactions, held while `gate` is set.
            if gate is not None and url.endswith("/txs"):
                held.append(url)
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    stopped.append(url)
                    raise
            return await world.http(url, **options)

        client = world.client(http=transport)
        invoice = await client.create_invoice(ref="ORD-12", sats=49_000)
        address = invoice.onchain_address
        assert address is not None and invoice.payment_hash is not None
        heard: list[PaymentStatus] = []
        waiting = asyncio.ensure_future(
            client.wait_for_payment(invoice, interval_ms=250, on_status=heard.append)
        )
        await until(lambda: world.socket.tracked() == [address] and heard)
        # A block: a full check starts, and waits for the explorer's answer.
        gate = asyncio.Event()
        await world.socket.push({"block": {"height": world.explorer.height}})
        await until(lambda: held)
        # Meanwhile the Lightning invoice is paid: the wait returns.
        world.lightning.pay(invoice.payment_hash)
        status = await waiting
        assert (status.state, status.method) == ("paid", "lightning")
        # The check it held was stopped before it returned.
        assert stopped == held
        published = len(world.received()) + len(world.received(kind=1))
        made = len(world.lightning.invoices)
        world.explorer.add(address, tx(txid(), {address: 10_000}))
        gate.set()
        await asyncio.sleep(0.5)
        assert len(world.received()) + len(world.received(kind=1)) == published
        assert len(world.lightning.invoices) == made
        await until(lambda: not world.socket.connections)
    finally:
        await world.close()


async def test_a_lost_socket_connects_again() -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-5", sats=49_000)
        heard: list[PaymentStatus] = []
        waiting = asyncio.ensure_future(
            client.wait_for_payment(invoice, interval_ms=60_000, on_status=heard.append)
        )
        await until(lambda: world.socket.opened == 1 and world.socket.connections)
        await world.socket.drop()
        # Connected again after 1 s, and checked at the connection.
        before = len(heard)
        await until(lambda: world.socket.opened == 2 and world.socket.connections)
        await until(lambda: len(heard) > before)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await until(lambda: not world.socket.connections)
    finally:
        await world.close()


async def test_a_paid_rest_invoice_checks_the_onchain_part_at_once() -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-6", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        paying = txid()
        transaction = tx(paying, {address: 20_000})
        world.explorer.add(address, transaction)
        heard: list[PaymentStatus] = []
        waiting = asyncio.ensure_future(
            client.wait_for_payment(invoice, interval_ms=250, on_status=heard.append)
        )
        await until(lambda: heard and heard[-1].rest is not None and heard[-1].rest.bolt11)
        rest = heard[-1].rest
        assert rest is not None
        rest_invoice = next(i for i in world.lightning.invoices.values() if i.pr == rest.bolt11)
        # The on-chain payment 3 deep (a block says so): still underpaid.
        world.explorer.confirm(paying, world.explorer.height - 2)
        checks = len(world.http.urls(f"https://mempool.space/api/address/{address}"))
        await world.socket.push({"block": {"height": world.explorer.height}})
        await until(
            lambda: len(world.http.urls(f"https://mempool.space/api/address/{address}")) > checks
        )
        await asyncio.sleep(0.5)
        assert heard[-1].state == "open"
        # The rest paid: the on-chain part is checked again at once, without a socket message,
        # against what is now due.
        world.lightning.pay(rest_invoice.payment_hash)
        status = await asyncio.wait_for(waiting, 10)
        assert (status.state, status.method) == ("paid", "mixed")
        assert status.preimages == (rest_invoice.preimage,)
        # No status combined the rest paid with an on-chain part judged before it.
        assert all(s.state in ("open", "paid") for s in heard)
    finally:
        await world.close()


async def test_a_rest_invoice_that_cannot_be_asked_is_logged_once() -> None:
    """SPEC.md §23: a rest invoice the Lightning address refuses, full check after full check
    (each block wakes one), is logged once, not at each check."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-10", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        world.lightning.refuse_invoices = True
        transaction = tx(txid(), {address: 20_000})
        world.explorer.add(address, transaction)
        heard: list[PaymentStatus] = []
        waiting = asyncio.ensure_future(
            client.wait_for_payment(invoice, interval_ms=60_000, on_status=heard.append)
        )
        checked = f"https://mempool.space/api/address/{address}"

        def full_checks() -> int:
            return len(world.http.urls(checked))

        await until(lambda: heard and world.socket.connections)
        for done in range(full_checks(), full_checks() + 3):
            await world.socket.push({"block": {"height": world.explorer.height}})
            await until(lambda: full_checks() > done)  # noqa: B023 - awaited at once
        await until(lambda: len(heard) >= 4)
        refused = [w for w in world.warnings if "No Lightning invoice of the rest" in w]
        assert len(refused) == 1
        assert len(world.lightning.asked) >= 5
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    finally:
        await world.close()


async def test_a_wait_and_a_check_at_once_ask_one_rest_invoice() -> None:
    """One ask of a rest at a time per invoice in a client, a wait's checks included: a check
    at the same time takes the rest invoice the other one asked."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-11", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        transaction = tx(txid(), {address: 20_000})
        world.explorer.add(address, transaction)
        # Every request slow: both checks reach the ask of the rest together.
        world.http.delay = 0.1
        heard: list[PaymentStatus] = []
        waiting = asyncio.ensure_future(
            client.wait_for_payment(invoice, interval_ms=60_000, on_status=heard.append)
        )
        await until(lambda: world.socket.connections)
        status = await client.get_status(invoice)
        await until(lambda: heard)
        assert status.rest is not None and status.rest.bolt11 is not None
        assert heard[0].rest is not None
        assert heard[0].rest.bolt11 == status.rest.bolt11
        assert len(world.lightning.invoices) == 2
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    finally:
        await world.close()


async def test_an_expired_invoice_ends_the_wait() -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-7", sats=49_000)
        world.now = invoice.expires_at + 121
        status = await client.wait_for_payment(invoice, interval_ms=250)
        assert status.to_json() == {"state": "expired", "received": "0"}
    finally:
        await world.close()


@pytest.mark.parametrize(
    ("options", "param"),
    [
        ({"interval_ms": 249}, "interval_ms"),
        ({"interval_ms": 60_001}, "interval_ms"),
        ({"interval_ms": 1000.0}, "interval_ms"),
        ({"interval_ms": True}, "interval_ms"),
        ({"on_status": "print"}, "on_status"),
    ],
)
async def test_wait_options_are_checked_before_any_request(
    options: dict[str, Any], param: str
) -> None:
    world = await World.start(lot=0)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-8", sats=21)
        asked = len(world.http.asked)
        with pytest.raises(InvalidOptionError) as raised:
            await client.wait_for_payment(invoice, **options)
        assert raised.value.param == param
        assert len(world.http.asked) == asked
    finally:
        await world.close()
