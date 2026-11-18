"""Integration tests of an order's invoice (PORTING.md, Steps, 5), against local relays and fake
services: an order in sats and in a currency, the invoice checks, the declaration on a majority,
one order one invoice at a time, relay moves, a removed integration, a mute relay."""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

import pytest

from fakes.lightning import FakeInvoice, bolt11
from fakes.world import World
from sattleme_checkout import (
    ChannelNotFoundError,
    CheckoutUnavailableError,
    DeclaredInvoice,
    InvalidInvoiceError,
    InvalidOrderError,
    Invoice,
    InvoicePendingError,
    NetworkError,
    OrderPaidError,
    PaymentStatus,
    Price,
    RateError,
    RevokedError,
    SattlemeError,
    UnsupportedCurrencyError,
)


async def started(**options: Any) -> World:
    return await World.start(**options)


def lightning_only(world: World) -> None:
    """No address to lease: Lightning only."""
    world.publish_lot([])


async def test_an_order_in_sats() -> None:
    world = await started()
    try:
        lightning_only(world)
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-1", sats="21", note="  Order 1  ")
        assert invoice.sats == 21
        assert invoice.fiat is None
        assert invoice.rate is None
        assert invoice.note == "Order 1"
        assert invoice.onchain_address is None
        assert invoice.payment_uri == f"lightning:{invoice.bolt11}"
        asked = world.lightning.asked[-1]
        assert asked == {"amount": "21000", "expiry": "900", "comment": "Order 1"}
        found = await client.find_invoices("ORD-1")
        assert [i.payment_hash for i in found] == [invoice.payment_hash]
        assert found[0].fiat is None
        assert found[0].declared_at == int(world.now)
        # Stored as SPEC.md §3 says, and read back.
        stored = invoice.to_json()
        assert stored["sats"] == "21"
        assert type(invoice).from_json(stored) == invoice
    finally:
        await world.close()


async def test_an_invoice_read_back_has_every_member_that_may_be_null() -> None:
    """SPEC.md §3: a value that is absent is written null, never left out (§2): an Invoice's JSON
    or a DeclaredInvoice's without `fiat` or `rate` is none, while with them null it reads."""
    world = await started()
    try:
        lightning_only(world)
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-5", sats=21)
        [declared] = await client.find_invoices("ORD-5")
        for read_back, stored, names in (
            (Invoice.from_json, invoice.to_json(), ("fiat", "rate")),
            (DeclaredInvoice.from_json, declared.to_json(), ("fiat",)),
        ):
            assert all(stored[name] is None for name in names)
            assert read_back(stored) in (invoice, declared)
            for name in names:
                without = {key: value for key, value in stored.items() if key != name}
                with pytest.raises(InvalidOrderError) as raised:
                    read_back(without)
                assert raised.value.param == "invoice"
    finally:
        await world.close()


async def test_an_order_in_a_currency_with_the_default_sources() -> None:
    world = await started()
    try:
        lightning_only(world)
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-2", fiat=Price("24.5", "CHF"))
        assert invoice.sats == 35_502
        assert invoice.fiat is not None
        assert invoice.fiat.to_json() == {
            "amount": "24.50",
            "currency": "CHF",
            "minor": "2450",
            "digits": 2,
        }
        assert invoice.rate is not None
        assert invoice.rate.to_json() == {
            "source": "coingecko",
            "currency": "CHF",
            "price": "69012",
            "fetchedAt": int(world.now),
            "time": int(world.now),
        }
        # CoinGecko first, alone, without a key.
        rate_urls = [u for u in world.http.urls() if "coingecko" in u or "prices" in u]
        assert len(rate_urls) == 1
        asked = next(a for a in world.http.asked if "coingecko" in a.url)
        assert "x-cg-demo-api-key" not in {name.lower() for name in asked.headers}
        declared = (await client.find_invoices("ORD-2"))[0]
        assert declared.fiat == invoice.fiat
        # Every request (the rate source, the Lightning address, its callback, the verify URL):
        # one User-Agent, the same in every implementation (SPEC.md §15).
        assert world.http.urls("https://api.coingecko.com/") != []
        assert world.http.agents() == {("sattleme-Checkout/1", "application/json")}
    finally:
        await world.close()


async def test_the_coingecko_key_goes_to_coingecko_only() -> None:
    world = await started()
    try:
        lightning_only(world)
        world.rates.status["coingecko"] = 503
        client = world.client(coingecko_api_key="CG-demo", rate_source=["coingecko", "mempool"])
        invoice = await client.create_invoice(ref="ORD-3", fiat=Price("10", "CHF"))
        assert invoice.rate is not None
        assert invoice.rate.source == "mempool"
        for asked in world.http.asked:
            has_key = "x-cg-demo-api-key" in {name.lower() for name in asked.headers}
            assert has_key == asked.url.startswith("https://api.coingecko.com/")
        # The key's header besides, never instead of, the User-Agent.
        assert world.http.agents() == {("sattleme-Checkout/1", "application/json")}
        assert any("Rate source coingecko failed (network)" in w for w in world.warnings)
    finally:
        await world.close()


@pytest.mark.parametrize(
    ("statuses", "prices", "source", "error"),
    [
        ({"coingecko": 503}, None, "mempool", None),
        ({"coingecko": 429, "mempool": 500}, None, "coinbase", None),
        ({"coingecko": 404, "mempool": 503, "coinbase": 503}, None, "kraken", None),
        (
            {"coingecko": 503, "mempool": 503, "coinbase": 503, "kraken": 503},
            None,
            "opennode",
            None,
        ),
        (
            {"coingecko": 503, "mempool": 503, "coinbase": 503, "kraken": 503, "opennode": 503},
            None,
            None,
            NetworkError,
        ),
        (
            {"coingecko": 503, "mempool": 503, "coinbase": 503, "kraken": 503, "opennode": 400},
            None,
            None,
            RateError,
        ),
        (
            {},
            {"coingecko": {}, "mempool": {}, "coinbase": {}, "kraken": {}, "opennode": {}},
            None,
            UnsupportedCurrencyError,
        ),
        ({}, {"coingecko": {"CHF": "0"}}, "mempool", None),
        ({}, {"coingecko": {"CHF": True}}, "mempool", None),
    ],
)
async def test_the_default_list_is_asked_in_order(
    statuses: dict[str, int],
    prices: dict[str, dict[str, Any]] | None,
    source: str | None,
    error: type[Exception] | None,
) -> None:
    world = await started()
    try:
        lightning_only(world)
        world.rates.status.update(statuses)
        if prices is not None:
            world.rates.prices.update(prices)
        client = world.client()
        if error is not None:
            with pytest.raises(error):
                await client.create_invoice(ref="ORD-4", fiat=Price("10", "CHF"))
            assert world.lightning.asked == []
            return
        invoice = await client.create_invoice(ref="ORD-4", fiat=Price("10", "CHF"))
        assert invoice.rate is not None
        assert invoice.rate.source == source
    finally:
        await world.close()


async def test_a_price_too_old_is_not_usable() -> None:
    world = await started()
    try:
        lightning_only(world)
        world.rates.age = 3601
        client = world.client(rate_source=["coingecko", "mempool"])
        with pytest.raises(RateError):
            await client.create_invoice(ref="ORD-5", fiat=Price("10", "CHF"))
        world.rates.age = 3600
        invoice = await client.create_invoice(ref="ORD-5", fiat=Price("10", "CHF"))
        assert invoice.rate is not None
        assert invoice.rate.time == int(world.now) - 3600
    finally:
        await world.close()


async def test_custom_sources() -> None:
    world = await started()
    try:
        lightning_only(world)

        async def asynchronous(currency: str) -> str:
            assert currency == "EUR"
            return "50000"

        def failing(_currency: str) -> str:
            raise OSError("my database is down")

        client = world.client(rate_source=[failing, asynchronous])
        invoice = await client.create_invoice(ref="ORD-6", fiat=Price("1", "EUR"))
        assert invoice.sats == 2000
        assert invoice.rate is not None
        assert invoice.rate.source == "custom"
        assert invoice.rate.time is None
        with pytest.raises(RateError) as raised:
            await client.create_invoice(ref="ORD-7", fiat=Price("1", "EUR"), rate_source=failing)
        assert isinstance(raised.value.__cause__, OSError)
    finally:
        await world.close()


def _invoice_answers(world: World) -> dict[str, Any]:
    """An answer of the Lightning address failing each check of SPEC.md §18."""
    metadata_hash = hashlib.sha256(world.lightning.metadata.encode()).digest()
    now = int(world.now)

    def made(**changes: Any) -> Any:
        def answer(invoice: FakeInvoice) -> dict[str, Any]:
            fields: dict[str, Any] = {
                "msat": invoice.msat,
                "payment_hash": bytes.fromhex(invoice.payment_hash),
                "description_hash": metadata_hash,
                "timestamp": now,
                "expiry": invoice.expiry,
            }
            fields.update(changes)
            verify = changes.pop("verify", f"https://breez.tips/v/{invoice.payment_hash}")
            fields.pop("verify", None)
            return {"pr": bolt11(**fields), "verify": verify}

        return answer

    return {
        "bolt11": lambda _invoice: {"pr": "lnbc1notaninvoice", "verify": "https://breez.tips/v"},
        "network": made(network="tb"),
        "amount": made(msat=22_000),
        "description_hash": made(description_hash=bytes(32)),
        "verify_url": made(verify="https://elsewhere.example/v"),
        "duration": made(expiry=901),
        "clock": made(timestamp=now - 301),
        "expired": made(timestamp=now - 300, expiry=300),
    }


@pytest.mark.parametrize(
    "check",
    [
        "bolt11",
        "network",
        "amount",
        "description_hash",
        "verify_url",
        "duration",
        "clock",
        "expired",
    ],
)
async def test_every_invoice_check(check: str) -> None:
    world = await started()
    try:
        lightning_only(world)
        world.lightning.answer = _invoice_answers(world)[check]
        client = world.client()
        with pytest.raises(InvalidInvoiceError) as raised:
            await client.create_invoice(ref="ORD-8", sats=21)
        assert raised.value.check == check
        assert world.received() == []
    finally:
        await world.close()


@pytest.mark.parametrize(
    "lone",
    [chr(0xD800), chr(0xDFFF), chr(0xDE00) + chr(0xD83D)],
    ids=["a lone high surrogate", "a lone low surrogate", "a pair reversed"],
)
async def test_a_pay_requests_metadata_with_a_lone_surrogate(lone: str) -> None:
    """SPEC.md §6, §16: metadata holding a lone surrogate (an escape such as `\\ud800` in the
    answer's JSON, as the fake writes it) has no UTF-8 to hash: invalid_invoice, before the
    callback is asked; never the language's own encoding error, never U+FFFD hashed in its
    place."""
    world = await started()
    try:
        lightning_only(world)
        world.lightning.metadata = f'[["text/plain","Pay {lone}"]]'
        with pytest.raises(InvalidInvoiceError) as raised:
            await world.client().create_invoice(ref="ORD-30", sats=21)
        assert "metadata" in raised.value.message
        assert world.lightning.asked == []
        assert world.received() == []
    finally:
        await world.close()


async def test_no_majority_no_invoice() -> None:
    world = await started()
    try:
        lightning_only(world)
        client = world.client()
        world.relays["relay-b.example"].refuse = True
        invoice = await client.create_invoice(ref="ORD-9", sats=21)
        assert invoice.bolt11 is not None
        world.relays["relay-c.example"].refuse = True
        with pytest.raises(NetworkError) as raised:
            await client.create_invoice(ref="ORD-10", sats=21)
        assert "do not show this invoice" in raised.value.message
    finally:
        await world.close()


async def test_a_pending_invoice_is_declared_again() -> None:
    world = await started()
    try:
        lightning_only(world)
        client = world.client()
        for name in ("relay-b.example", "relay-c.example"):
            world.relays[name].refuse = True
        with pytest.raises(NetworkError):
            await client.create_invoice(ref="ORD-11", sats=21)
        sale = world.sale("ORD-11")
        assert [len(world.held(url, sale.author)) for url in world.relay_urls] == [1, 0, 0]
        # A read goes on at a majority: the relay holding it must be among those that answer
        # (relay-b stays mute) for the next call to find it, and publish it again.
        world.relays["relay-b.example"].mute = True
        world.relays["relay-c.example"].refuse = False
        with pytest.raises(InvoicePendingError) as raised:
            await client.create_invoice(ref="ORD-11", sats=21)
        assert raised.value.checkout_url is None
        assert raised.value.invoice.sats == 21
        assert [len(world.held(url, sale.author)) for url in world.relay_urls] == [1, 0, 1]
        assert len(world.lightning.invoices) == 1
    finally:
        await world.close()


async def test_one_order_one_invoice_at_a_time() -> None:
    world = await started()
    try:
        lightning_only(world)
        client = world.client()
        first = await client.create_invoice(ref="ORD-12", sats=21)
        with pytest.raises(InvoicePendingError) as pending:
            await client.create_invoice(ref="ORD-12", sats=21)
        assert pending.value.invoice.payment_hash == first.payment_hash
        assert pending.value.retryable is False
        # Ended and unpaid: a new invoice.
        world.now = first.expires_at + 120
        second = await client.create_invoice(ref="ORD-12", sats=21)
        assert second.payment_hash != first.payment_hash
        # Paid: never another one.
        assert second.payment_hash is not None
        world.lightning.pay(second.payment_hash)
        world.now = second.expires_at + 120
        with pytest.raises(OrderPaidError) as paid:
            await client.create_invoice(ref="ORD-12", sats=21)
        assert paid.value.invoice.payment_hash == second.payment_hash
        assert paid.value.status.method == "lightning"
        assert paid.value.preimage == world.lightning.invoices[second.payment_hash].preimage
        found = await client.find_invoices("ORD-12")
        assert [i.payment_hash for i in found] == [first.payment_hash, second.payment_hash]
    finally:
        await world.close()


async def test_a_pending_invoice_is_checked_before_it_is_handed_out() -> None:
    """SPEC.md §21, step 1: the payment of an invoice that may still be paid is checked, even of
    one alone, so that an invoice already paid is never handed out again; a check that fails is
    the error (it may have been paid)."""
    world = await started()
    try:
        lightning_only(world)
        client = world.client()
        first = await client.create_invoice(ref="ORD-14", sats=21)
        assert first.payment_hash is not None
        asked = len(world.http.urls())
        with pytest.raises(InvoicePendingError):
            await client.create_invoice(ref="ORD-14", sats=21)
        assert [u for u in world.http.urls()[asked:] if "/verify/" in u] == [first.verify_url]
        # Its check fails: that error, nothing handed out.
        world.http.down.add(first.verify_url or "")
        with pytest.raises(NetworkError):
            await client.create_invoice(ref="ORD-14", sats=21)
        world.http.down.clear()
        # Paid within its time: order_paid, never handed out again.
        world.lightning.pay(first.payment_hash)
        with pytest.raises(OrderPaidError) as paid:
            await client.create_invoice(ref="ORD-14", sats=21)
        assert paid.value.invoice.payment_hash == first.payment_hash
        assert len(world.lightning.invoices) == 1
    finally:
        await world.close()


async def test_two_calls_for_one_order_run_one_after_the_other() -> None:
    world = await started()
    try:
        lightning_only(world)
        client = world.client()
        results = await asyncio.gather(
            client.create_invoice(ref="ORD-13", sats=21),
            client.create_invoice(ref="ORD-13", sats=21),
            return_exceptions=True,
        )
        assert sum(isinstance(r, InvoicePendingError) for r in results) == 1
        assert len(world.lightning.invoices) == 1
    finally:
        await world.close()


async def _two_pending(world: World, ref: str) -> tuple[Any, Any]:
    """An order with two invoices that may still be paid (SPEC.md §21, step 1): one shown, on
    relay-b and relay-c; then a later call, whose read missed it (both relays hid it), made
    another one and failed once relay-a alone took its declaration."""
    sale = world.sale(ref)
    client = world.client()
    world.relays["relay-a.example"].refuse = True
    shown = await client.create_invoice(ref=ref, sats=21)
    world.relays["relay-a.example"].refuse = False
    for name in ("relay-b.example", "relay-c.example"):
        world.relays[name].hides = lambda event: event.pubkey == sale.author
        world.relays[name].refuse = True
    world.now += 60
    with pytest.raises(NetworkError):
        await client.create_invoice(ref=ref, sats=21)
    for name in ("relay-b.example", "relay-c.example"):
        world.relays[name].hides = None
        world.relays[name].refuse = False
    [failed] = world.held("wss://relay-a.example", sale.author)
    return failed, shown


async def test_of_several_invoices_the_one_a_majority_holds_is_handed_out() -> None:
    world = await started()
    try:
        lightning_only(world)
        failed, shown = await _two_pending(world, "ORD-70")
        sale = world.sale("ORD-70")
        # relay-c late: the first read, at a majority, sees both; the read on every relay
        # waits for it.
        late = world.relays["relay-c.example"]
        late.late = lambda filter_: filter_.get("authors") == [sale.author]
        late.late_by = 0.4
        asked = len(world.http.urls())
        with pytest.raises(InvoicePendingError) as pending:
            await world.client().create_invoice(ref="ORD-70", sats=21)
        assert pending.value.invoice.payment_hash == shown.payment_hash
        # Both payments checked first.
        verified = [url for url in world.http.urls()[asked:] if "/verify/" in url]
        assert {url.rsplit("/", 1)[1] for url in verified} == set(world.lightning.invoices)
        # The one never shown is not published again.
        assert failed.id not in [e.id for e in world.held("wss://relay-b.example", sale.author)]
    finally:
        await world.close()


async def test_a_copy_whose_signature_is_damaged_still_tells_a_relay_holds_it() -> None:
    """SPEC.md §21, step 1: the one shown is the one whose last record a majority of the list
    sent, a copy whose id is its hash (SPEC.md §11), whatever its signature: relay-c's copy of
    the shown invoice's declaration, its signature damaged, still counts."""
    world = await started()
    try:
        lightning_only(world)
        _failed, shown = await _two_pending(world, "ORD-73")
        sale = world.sale("ORD-73")
        late = world.relays["relay-c.example"]
        [declaration] = world.held("wss://relay-c.example", sale.author)
        for address, held in list(late.events.items()):
            if getattr(held, "id", None) == declaration.id:
                del late.events[address]
        flipped = ("0" if declaration.sig[0] != "0" else "1") + declaration.sig[1:]
        late.put({**declaration.to_json(), "sig": flipped})
        # relay-c late: the first read, at a majority, sees both; the read on every relay
        # waits for it.
        late.late = lambda filter_: filter_.get("authors") == [sale.author]
        late.late_by = 0.3
        with pytest.raises(InvoicePendingError) as pending:
            await world.client().create_invoice(ref="ORD-73", sats=21)
        assert pending.value.invoice.payment_hash == shown.payment_hash
    finally:
        await world.close()


async def test_of_several_invoices_one_paid_gives_order_paid() -> None:
    world = await started()
    try:
        lightning_only(world)
        _failed, shown = await _two_pending(world, "ORD-71")
        never_shown = next(
            i for i in world.lightning.invoices.values() if i.payment_hash != shown.payment_hash
        )
        world.lightning.pay(never_shown.payment_hash)
        with pytest.raises(OrderPaidError) as paid:
            await world.client().create_invoice(ref="ORD-71", sats=21)
        assert paid.value.invoice.payment_hash == never_shown.payment_hash
    finally:
        await world.close()


@pytest.mark.parametrize("paid", [False, True], ids=["none paid", "one paid"])
async def test_of_several_invoices_fewer_than_a_majority_reading_again(paid: bool) -> None:
    """SPEC.md §21, step 1: of several that may still be paid, the sale's records read again
    on every relay, which fewer than a majority answer: `network`, nothing handed out; but a
    paid one gives order_paid whatever the read (a payment proves itself)."""
    world = await started()
    try:
        lightning_only(world)
        _failed, shown = await _two_pending(world, "ORD-72")
        sale = world.sale("ORD-72")
        if paid:
            assert shown.payment_hash is not None
            world.lightning.pay(shown.payment_hash)
        # relay-b and relay-c answer the first read of the sale's records, close the next one.
        asked: dict[str, list[int]] = {}
        for name in ("relay-b.example", "relay-c.example"):
            firsts = asked.setdefault(name, [])

            def closes(filter_: dict[str, Any], firsts: list[int] = firsts) -> bool:
                if filter_.get("authors") != [sale.author] or "until" in filter_:
                    return False
                firsts.append(1)
                return len(firsts) > 1

            world.relays[name].closes = closes
        published = len(world.received())
        if paid:
            with pytest.raises(OrderPaidError) as raised:
                await world.client().create_invoice(ref="ORD-72", sats=21)
            assert raised.value.invoice.payment_hash == shown.payment_hash
        else:
            with pytest.raises(NetworkError):
                await world.client().create_invoice(ref="ORD-72", sats=21)
        # Nothing published again: no invoice was handed out.
        assert len(world.received()) == published
        # Read twice on each: the first read, then the one on every relay, closed.
        assert {name: len(firsts) for name, firsts in asked.items()} == {
            "relay-b.example": 2,
            "relay-c.example": 2,
        }
    finally:
        await world.close()


async def test_relay_moves() -> None:
    world = await started(
        relays=("relay-a.example", "relay-b.example", "relay-c.example", "relay-d.example"),
        key_relays=3,
    )
    try:
        old = world.relay_urls[:3]
        new = ["wss://relay-d.example"]
        world.relays["wss://relay-d.example"].events.clear()
        # The channel on the key's relays names relay-d: the merchant moved there. Not copied
        # there yet, the channel read so far stands: declarations go to relay-d.
        world.publish_channel(relays=new, on=old)
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-14", sats=21)
        sale = world.sale("ORD-14")
        assert len(world.held("wss://relay-d.example", sale.author)) == 1
        assert all(world.held(url, sale.author) == [] for url in old)
        assert invoice.bolt11 is not None
        # Found there, the channel there wins: removed there alone, the integration is revoked.
        world.publish_channel(status="removed", on=new)
        with pytest.raises(RevokedError):
            await client.create_invoice(ref="ORD-15", sats=21)
        # Three moves at most: d names a, b, c (the key's), which name d again.
        world.publish_channel(relays=old, on=new)
        world.publish_channel(relays=new, on=old)
        invoice = await client.create_invoice(ref="ORD-16", sats=21)
        assert invoice.bolt11 is not None
    finally:
        await world.close()


async def test_a_removed_integration() -> None:
    world = await started()
    try:
        world.publish_channel(status="removed")
        client = world.client()
        with pytest.raises(RevokedError) as raised:
            await client.create_invoice(ref="ORD-16", sats=21)
        assert raised.value.retryable is False
        with pytest.raises(RevokedError):
            await client.merchant()
        with pytest.raises(RevokedError):
            await client.find_invoices("ORD-16")
        assert world.lightning.asked == []
    finally:
        await world.close()


async def test_a_removed_integration_stops_the_calls_that_read_the_channel() -> None:
    """SPEC.md §14, step 3: a removed channel stops every call that reads it, the on-chain part
    of a check included: the check of an invoice with an address (and the wait for it) is
    `revoked` unless its Lightning invoice is paid. The check of an invoice without an address
    reads no relay and goes on: its verify URL needs nothing of the key."""
    world = await started()
    try:
        client = world.client()
        with_address = await client.create_invoice(ref="ORD-31", sats=49_000)
        assert with_address.onchain_address is not None
        lightning_only(world)
        without = await client.create_invoice(ref="ORD-32", sats=21)
        assert without.onchain_address is None
        assert without.payment_hash is not None and with_address.payment_hash is not None
        world.publish_channel(status="removed")

        def asked() -> int:
            return sum(len(world.relays[url].requests) for url in world.relay_urls)

        before = asked()
        assert (await client.get_status(without)).state == "open"
        world.lightning.pay(without.payment_hash)
        assert (await client.wait_for_payment(without, interval_ms=250)).state == "paid"
        assert asked() == before
        with pytest.raises(RevokedError):
            await client.get_status(with_address)
        with pytest.raises(RevokedError):
            await client.wait_for_payment(with_address, interval_ms=250)
        world.lightning.pay(with_address.payment_hash)
        status = await client.get_status(with_address)
        assert (status.state, status.method) == ("paid", "lightning")
    finally:
        await world.close()


@pytest.mark.parametrize("failure", ["relays", "explorer", "channel"])
async def test_a_paid_lightning_invoice_wins_over_every_failure_of_the_onchain_part(
    failure: str,
) -> None:
    """SPEC.md §14, §31: a paid Lightning invoice proves itself, whatever the on-chain part of
    the check of an invoice with an address meets, as for `revoked` (the test above): `network`,
    the relays failing or the explorer; `channel_not_found`, the channel on none of the relays.
    Unpaid, the check is that failure; once paid, `paid` in Lightning, for `get_status` and for
    `wait_for_payment`."""
    world = await started()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-33", sats=49_000)
        assert invoice.onchain_address is not None
        assert invoice.payment_hash is not None
        expected: type[SattlemeError] = NetworkError
        if failure == "relays":
            for relay in world.relays.relays.values():
                relay.down = True
        elif failure == "explorer":
            world.explorer.failing = True
        else:
            for relay in world.relays.relays.values():
                relay.events.clear()
            expected = ChannelNotFoundError
        with pytest.raises(expected):
            await client.get_status(invoice)
        world.lightning.pay(invoice.payment_hash)
        paid = PaymentStatus.paid_lightning(world.lightning.invoices[invoice.payment_hash].preimage)
        assert await client.get_status(invoice) == paid
        assert await client.wait_for_payment(invoice, interval_ms=250) == paid
    finally:
        await world.close()


async def test_no_channel() -> None:
    world = await started()
    try:
        for relay in world.relays.relays.values():
            relay.events.clear()
        with pytest.raises(ChannelNotFoundError) as raised:
            await world.client().merchant()
        assert raised.value.newer_format is False
        assert raised.value.retryable is True
    finally:
        await world.close()


async def test_a_mute_relay_slows_nothing() -> None:
    world = await started()
    try:
        lightning_only(world)
        world.relays["relay-c.example"].mute = True
        client = world.client(relay_timeout_ms=5000)
        started_at = time.monotonic()
        invoice = await client.create_invoice(ref="ORD-17", sats=21)
        await client.find_invoices("ORD-17")
        assert time.monotonic() - started_at < 2.5
        assert invoice.bolt11 is not None
    finally:
        await world.close()


async def test_relays_requiring_authentication() -> None:
    world = await started()
    try:
        lightning_only(world)
        for relay in world.relays.relays.values():
            relay.require_auth = True
            relay.auth_publisher = True
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-18", sats=21, checkout=True)
        assert invoice.checkout_url is not None
        sale = world.sale("ORD-18")
        # Each event is published authenticated as its author: the sale for its declaration,
        # the link's own author for the link; reads with a key of their own.
        for url in world.relay_urls:
            relay = world.relays[url]
            authors = {event["pubkey"] for event in relay.received}
            assert authors <= set(relay.authenticated)
            assert sale.author in authors or relay.received == []
        assert sum(sale.author in world.relays[url].authenticated for url in world.relay_urls) >= 2
        assert all(
            world.merchant.team not in world.relays[u].authenticated for u in world.relay_urls
        )
    finally:
        await world.close()


async def test_the_merchant() -> None:
    world = await started(lot=7)
    try:
        merchant = await world.client().merchant()
        assert merchant.to_json() == {
            "businessName": "Sunrise Bakery",
            "minSats": "1",
            "maxSats": "4000000",
            "onchainAddresses": 7,
        }
        assert "breez.tips" not in repr(merchant)
    finally:
        await world.close()


async def test_checkout_links() -> None:
    world = await started()
    try:
        lightning_only(world)
        client = world.client()
        invoice = await client.create_invoice(
            ref="ORD-19", sats=21, checkout=True, redirect_url="myshop://orders/19"
        )
        assert invoice.checkout_url is not None
        assert invoice.checkout_url.startswith("https://pay.example/pay#c.")
        assert invoice.redirect_url == "myshop://orders/19"
        # A new link to the pending invoice, for a shop that lost the first.
        with pytest.raises(InvoicePendingError) as raised:
            await client.create_invoice(ref="ORD-19", sats=21, checkout=True)
        assert raised.value.checkout_url is not None
        assert raised.value.checkout_url != invoice.checkout_url
        # Without checkout, no link.
        with pytest.raises(InvoicePendingError) as again:
            await client.create_invoice(ref="ORD-19", sats=21)
        assert again.value.checkout_url is None
        with pytest.raises(InvalidOrderError) as refused:
            await client.create_invoice(ref="ORD-20", sats=21, redirect_url="https://shop.example")
        assert refused.value.param == "redirect_url"
    finally:
        await world.close()


async def test_a_link_on_too_few_relays_declares_nothing() -> None:
    world = await started()
    try:
        lightning_only(world)
        client = world.client()

        def refuse_links(event: Any) -> bool:
            return bool(event.tag("expiration") is None)

        for name in ("relay-b.example", "relay-c.example"):
            world.relays[name].on_event = refuse_links
        with pytest.raises(NetworkError):
            await client.create_invoice(ref="ORD-21", sats=21, checkout=True)
        sale = world.sale("ORD-21")
        assert all(world.held(url, sale.author) == [] for url in world.relay_urls)
        # The same order gets a new invoice at once.
        for name in ("relay-b.example", "relay-c.example"):
            world.relays[name].on_event = None
        invoice = await client.create_invoice(ref="ORD-21", sats=21, checkout=True)
        assert invoice.checkout_url is not None
    finally:
        await world.close()


async def test_a_channel_without_a_checkout_origin() -> None:
    world = await started(checkout_origin=None)
    try:
        client = world.client()
        with pytest.raises(CheckoutUnavailableError):
            await client.create_invoice(ref="ORD-22", sats=21, checkout=True)
        assert world.lightning.asked == []
        assert world.received() == []
    finally:
        await world.close()
