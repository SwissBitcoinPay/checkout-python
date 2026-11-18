"""Integration tests of on-chain payments (PORTING.md, Steps, 5): the methods of an order, a lot
read and an address leased, two servers leasing one address, a paid address skipped, an empty
lot, the witnesses, the on-chain statuses, BlockCypher, an order paid on-chain found again, the
rest of an underpayment, a mixed payment, and the merchant's tolerance."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import datetime
import secrets
import urllib.parse
from typing import Any

import pytest

from fakes.chain import deposit_address, tx, txid
from fakes.lightning import bolt11
from fakes.registry import leases_of, taken_records, witnesses_of
from fakes.world import START, World
from sattleme_checkout import (
    InvalidInvoiceError,
    InvoicePendingError,
    NetworkError,
    OnchainUnavailableError,
    OrderPaidError,
    PaymentStatus,
    _json,
)
from sattleme_checkout._channel import ActiveChannel
from sattleme_checkout._checkout import Fragment
from sattleme_checkout._constants import (
    ACCEPTANCE_TYPE,
    CHECKOUT_LIFE,
    REST_TYPE,
    SATS_CURRENCY,
    WITNESS_TYPE,
)
from sattleme_checkout._crypto import Secret, xonly_public_key
from sattleme_checkout._declaration import DeclaredPrice, Registry, open_declaration
from sattleme_checkout._derive import (
    address_tag,
    channel_encryption_key,
    lot_address,
    onchain_registry_address,
    order_tag,
    rest_keys,
    sale_keys,
    seen_keys,
    witness_address,
)
from sattleme_checkout._lease import (
    SIZED_ADDRESS,
    LeaseTerms,
    lease_event,
    lease_fits,
    lease_inside,
)
from sattleme_checkout._nostr import Event
from sattleme_checkout._records import Opened, seal
from sattleme_checkout._rest import open_acceptance, open_rest_record
from sattleme_checkout.models import Invoice


def rfc3339(seconds: float) -> str:
    moment = datetime.datetime.fromtimestamp(seconds, datetime.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def pay_onchain(world: World, invoice: Invoice, sats: int, **options: Any) -> str:
    assert invoice.onchain_address is not None
    paying = txid()
    transaction = tx(paying, {invoice.onchain_address: sats}, **options)
    world.explorer.add(invoice.onchain_address, transaction)
    return paying


def confirm(world: World, paying: str, depth: int = 3) -> None:
    world.explorer.confirm(paying, world.explorer.height - depth + 1)


async def test_a_lot_read_and_an_address_leased() -> None:
    world = await World.start(lot=3, tolerance=250)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-1", sats=49_000, note="Order 1")
        address = invoice.onchain_address
        assert address in world.lot
        assert invoice.payment_uri == f"bitcoin:{address}?amount=0.00049&lightning={invoice.bolt11}"
        # Published unconfirmed, confirmed, then marked a second later: the mark replaced it.
        [lease, mark] = taken_records(world, "ORD-1", address)
        assert lease.value.terms is not None
        assert lease.value.terms.confirmed is False
        [held] = leases_of(world, "ORD-1", address)
        assert held.event.id == mark.event.id
        assert mark.event.created_at == lease.event.created_at + 1
        terms = mark.value.terms
        assert terms == LeaseTerms(
            address=address,
            sats=49_000,
            invoice=invoice.bolt11,
            verify_url=invoice.verify_url,
            created_at=invoice.created_at,
            expires_at=invoice.expires_at,
            price=terms.price,
            note="Order 1",
            tolerance=250,
            confirmed=True,
        )
        assert dataclasses.replace(lease.value.terms, confirmed=True) == terms
        assert terms.end == invoice.expires_at + 120
        # The tolerance is in the lease, never in what the shop or the customer gets: the
        # Invoice holds the fields of SPEC.md §3 alone, none of them the tolerance.
        stored = invoice.to_json()
        assert set(stored) == {
            "ref", "bolt11", "paymentHash", "verifyUrl", "onchainAddress", "paymentUri", "sats",
            "fiat", "rate", "note", "createdAt", "expiresAt", "checkoutUrl", "redirectUrl",
        }  # fmt: skip
        assert all(value not in (250, "250") for value in stored.values())
        # On a majority, before the declaration: the declared invoice has its address.
        [declared] = await client.find_invoices("ORD-1")
        assert declared.onchain_address == address
    finally:
        await world.close()


@pytest.mark.parametrize(
    ("sats", "lot", "expected"),
    [
        (999, 3, ["lightning"]),
        (1000, 3, ["lightning", "onchain"]),
        (1000, 0, ["lightning"]),
        (5_000_000, 3, ["onchain"]),
        (5_000_000, 0, OnchainUnavailableError),
    ],
)
async def test_the_methods_of_an_order(sats: int, lot: int, expected: Any) -> None:
    world = await World.start(lot=lot)
    try:
        client = world.client()
        if not isinstance(expected, list):
            with pytest.raises(expected):
                await client.create_invoice(ref="ORD-2", sats=sats)
            assert world.lightning.asked == []
            return
        invoice = await client.create_invoice(ref="ORD-2", sats=sats)
        methods = [
            m for m, v in (("lightning", invoice.bolt11), ("onchain", invoice.onchain_address)) if v
        ]
        assert methods == expected
        if invoice.bolt11 is None:
            assert invoice.payment_hash is None
            assert invoice.verify_url is None
            assert invoice.created_at == int(world.now)
            assert invoice.expires_at == int(world.now) + 900
            assert invoice.payment_uri == f"bitcoin:{invoice.onchain_address}?amount=0.05"
            [declared] = await client.find_invoices("ORD-2")
            assert declared.bolt11 is None
            assert declared.onchain_address == invoice.onchain_address
            # Its lease's date as first published is the Invoice's; its mark's, a second
            # later, the DeclaredInvoice's.
            assert declared.created_at == invoice.created_at
            assert declared.declared_at == invoice.created_at + 1
    finally:
        await world.close()


async def test_the_explorer_failing() -> None:
    world = await World.start(lot=3)
    try:
        world.explorer.failing = True
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-3", sats=49_000)
        assert invoice.onchain_address is None
        assert any("the explorer did not answer" in w for w in world.warnings)
        with pytest.raises(NetworkError):
            await client.create_invoice(ref="ORD-4", sats=5_000_000)
    finally:
        await world.close()


async def test_a_paid_address_is_skipped_and_witnessed() -> None:
    world = await World.start(lot=2)
    try:
        paid, free = world.lot
        earlier = txid()
        world.explorer.add(paid, tx(earlier, {paid: 5000}))
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-5", sats=49_000)
        assert invoice.onchain_address == free
        if any(url.endswith(f"/address/{paid}/txs") for url in world.http.urls()):
            # Tried first: its payment is witnessed `none`, for the merchant's app.
            [witness] = witnesses_of(world)
            assert (witness.address, witness.txid, witness.sale) == (paid, earlier, None)
            assert witness.sats == 5000
        world.publish_lot([paid])
        with_none = await client.create_invoice(ref="ORD-6", sats=49_000)
        assert with_none.onchain_address is None
        assert any(w.txid == earlier and w.sale is None for w in witnesses_of(world))
    finally:
        await world.close()


async def test_another_servers_lease_at_once() -> None:
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        other = world.sale("ORD-OTHER")
        registry_address = onchain_registry_address(world.merchant.registry_key, address)
        injected: list[Event] = []

        def at_once(event: Event) -> bool:
            # As our lease arrives, another server's lease of the same address arrives too.
            if event.tag("d") == registry_address and not injected and event.pubkey != other.author:
                terms = LeaseTerms(
                    address=address,
                    sats=1000,
                    invoice=None,
                    verify_url=None,
                    created_at=int(world.now),
                    expires_at=int(world.now) + 900,
                    price=leases_price(),
                    note="ORD-OTHER",
                    tolerance=0,
                )
                rival = lease_event(
                    registry=world.merchant.registry,
                    sale=other,
                    terms=terms,
                    created_at=int(world.now),
                    random=secrets.token_bytes,
                )
                assert rival is not None
                injected.append(rival)
                for url in world.relay_urls:
                    world.relays[url].store(rival)
            return True

        for url in world.relay_urls:
            world.relays[url].on_event = at_once
        invoice = await world.client().create_invoice(ref="ORD-7", sats=49_000)
        # The later reader sees both, and withdraws (strictly after its lease, which the
        # relays then forget): the other keeps it.
        assert invoice.onchain_address is None
        [withdrawal] = leases_of(world, "ORD-7", address)
        assert withdrawal.value.terms is None
        assert withdrawal.event.created_at == int(world.now) + 1
        [kept] = leases_of(world, "ORD-OTHER", address)
        assert kept.value.terms is not None
        assert kept.event.id == injected[0].id
    finally:
        await world.close()


def leases_price() -> Any:
    from sattleme_checkout._declaration import DeclaredPrice

    return DeclaredPrice(minor=1000, digits=0, currency="SAT")


async def test_two_servers_at_once_never_both_keep_an_address() -> None:
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        first, second = world.client(), world.client()
        invoices = await asyncio.gather(
            first.create_invoice(ref="ORD-8", sats=49_000),
            second.create_invoice(ref="ORD-9", sats=49_000),
        )
        kept = [i for i in invoices if i.onchain_address == address]
        assert len(kept) <= 1
        for invoice in invoices:
            # A server that read the other's lease first never leased it; one that leased it and
            # then saw the other's withdrew; the one that keeps it holds its lease.
            records = leases_of(world, invoice.ref, address)
            holds = bool(records) and records[0].value.terms is not None
            assert holds == (invoice.onchain_address == address)
    finally:
        await world.close()


@pytest.mark.parametrize("failing", ["mark", "link", "declaration"])
async def test_a_confirmed_lease_is_never_withdrawn_whatever_fails_after(failing: str) -> None:
    """SPEC.md §17, §31: a failure after the lease's confirmation (its mark, the checkout link,
    the declaration) leaves it as it is: it holds its address until its end."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        registry_address = onchain_registry_address(world.merchant.registry_key, address)
        sale = world.sale("ORD-10")
        leases: list[Event] = []

        def take(event: Event) -> bool:
            # Two relays of three refuse what fails.
            if event.tag("d") == registry_address:
                leases.append(event)
                return failing != "mark" or len({e.id for e in leases}) == 1
            if failing == "link":
                return event.pubkey == sale.author
            return failing != "declaration"

        for name in ("relay-b.example", "relay-c.example"):
            world.relays[name].on_event = take
        with pytest.raises(NetworkError):
            await world.client().create_invoice(ref="ORD-10", sats=49_000, checkout=True)
        records = taken_records(world, "ORD-10", address)
        # The lease, its mark when it was taken, and never a withdrawal.
        assert all(record.value.terms is not None for record in records)
        assert records[0].value.terms.confirmed is False
        # What each relay holds of it: the lease, or its mark.
        held = leases_of(world, "ORD-10", address)
        assert held
        assert all(record.value.terms is not None for record in held)
    finally:
        await world.close()


async def test_an_empty_or_unreadable_lot() -> None:
    world = await World.start(lot=0)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-11", sats=49_000)
        assert invoice.onchain_address is None
        assert (await client.merchant()).onchain_addresses == 0
        with pytest.raises(OnchainUnavailableError) as raised:
            await client.create_invoice(ref="ORD-12", sats=5_000_000)
        assert raised.value.retryable is True
    finally:
        await world.close()


async def test_statuses_unconfirmed_then_paid_at_3_confirmations() -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-13", sats=49_000)
        status = await client.get_status(invoice)
        assert status.to_json() == {
            "state": "open",
            "expiresAt": invoice.expires_at,
            "received": "0",
            "rest": None,
        }
        world.now += 60
        paying = pay_onchain(world, invoice, 49_000)
        status = await client.get_status(invoice)
        assert status.to_json() == {
            "state": "unconfirmed",
            "received": "49000",
            "confirmations": 0,
            "txids": [paying],
        }
        # Witnessed in time, for the order's sale.
        [witness] = witnesses_of(world)
        assert (witness.txid, witness.sale, witness.replacement) == (
            paying,
            world.sale("ORD-13").author,
            False,
        )
        assert witness.seen_at == int(world.now)
        confirm(world, paying, 2)
        assert (await client.get_status(invoice)).confirmations == 2
        # 3 confirmations on the explorer read: paid on what it alone shows.
        confirm(world, paying)
        status = await client.get_status(invoice)
        assert status.to_json() == {
            "state": "paid",
            "method": "onchain",
            "received": "49000",
            "txids": [paying],
        }
        # Long after the end, the witness tells it came in time: BlockCypher is never asked.
        world.now = invoice.expires_at + 7200
        assert (await client.get_status(invoice)).state == "paid"
        assert world.http.urls("https://api.blockcypher.com") == []
        # mempool.space alone, with the User-Agent of every request (SPEC.md §15).
        assert {urllib.parse.urlsplit(url).hostname for url in world.http.urls()} == {
            "breez.tips",
            "mempool.space",
        }
        assert world.http.agents() == {("sattleme-Checkout/1", "application/json")}
    finally:
        await world.close()


async def test_a_txid_listed_twice_once_paying_nothing() -> None:
    """SPEC.md §31: the transactions paying the address nothing are left out (Explorers), then a
    txid given twice is taken once (Replacements, step 1): its entry that pays counts."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-26", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        paying = txid()
        coins = [f"{txid()}:0"]
        world.explorer.listed[address] = [
            tx(paying, {deposit_address(): 5_000}, inputs=coins),
            tx(paying, {address: 5_000}, inputs=coins),
        ]
        world.explorer.known[paying] = world.explorer.listed[address][1]
        status = await client.get_status(invoice)
        assert (status.state, status.received) == ("open", 5_000)
    finally:
        await world.close()


@pytest.mark.parametrize("again", ["the same", "another"])
async def test_a_txid_listed_twice_is_taken_once(again: str) -> None:
    """SPEC.md §31, Replacements, step 1: a txid given twice is taken once, its first entry. Listed
    twice, a payment never doubles what it pays."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-14", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        paying = txid()
        coins = [f"{txid()}:0"]
        listed = tx(paying, {address: 49_000}, inputs=coins, height=world.explorer.height - 2)
        second_entry = (
            copy.deepcopy(listed)
            if again == "the same"
            else tx(paying, {address: 1_000}, inputs=coins)
        )
        world.explorer.listed[address] = [listed, second_entry]
        world.explorer.known[paying] = listed
        status = await client.get_status(invoice)
        assert status.to_json() == {
            "state": "paid",
            "method": "onchain",
            "received": "49000",
            "txids": [paying],
        }
    finally:
        await world.close()


async def test_a_late_payment_and_blockcypher() -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-14", sats=49_000)
        end = invoice.expires_at + 120
        world.now = end + 30
        # Seen by no check while the lease ran: BlockCypher tells when it was received.
        late = pay_onchain(world, invoice, 49_000)
        world.blockcypher.received[late] = rfc3339(end + 5)
        status = await client.get_status(invoice)
        assert status.to_json() == {"state": "expired", "received": "0"}
        assert world.http.urls("https://api.blockcypher.com") != []
        assert [(w.txid, w.sale) for w in witnesses_of(world)] == [(late, None)]
        # Unknown to BlockCypher (HTTP 404): it will be received later, so after the end.
        world.explorer.drop(invoice.onchain_address or "", late)
        unknown = pay_onchain(world, invoice, 49_000)
        assert (await client.get_status(invoice)).state == "expired"
        # Received in time, by BlockCypher's time: it counts, and is witnessed for the sale.
        world.blockcypher.received[unknown] = rfc3339(end)
        status = await client.get_status(invoice)
        assert (status.state, status.received) == ("unconfirmed", 49_000)
        assert (unknown, world.sale("ORD-14").author) in [
            (w.txid, w.sale) for w in witnesses_of(world)
        ]
        # BlockCypher failing: the check fails (network), never a status.
        world.blockcypher.failing = True
        world.explorer.drop(invoice.onchain_address or "", unknown)
        pay_onchain(world, invoice, 49_000)
        with pytest.raises(NetworkError):
            await client.get_status(invoice)
        # The explorer and BlockCypher too: one User-Agent (SPEC.md §15).
        assert world.http.urls("https://mempool.space/api/") != []
        assert world.http.agents() == {("sattleme-Checkout/1", "application/json")}
    finally:
        await world.close()


def _held_by_every_relay(world: World, event: Event) -> bool:
    return all(
        any(isinstance(e, Event) and e.id == event.id for e in world.relays[url].events.values())
        for url in world.relay_urls
    )


@pytest.mark.parametrize("place", ["none", "the sale"])
async def test_a_witness_of_a_newer_format_is_never_written_over_and_warns(place: str) -> None:
    """SPEC.md §12, §31: a witness of a newer format is never used, never written over, and
    warned of. At the witness address of (A, T, none), a late T gets no witness `none` over it
    (step 6); at that of (A, T, the order's sale), T is undecided, nothing published."""
    world = await World.start(lot=1)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-32", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        paying = pay_onchain(world, invoice, 49_000)
        seen = seen_keys(world.merchant.key.secret)
        sale = None if place == "none" else world.sale("ORD-32").author
        newer = seal(
            record_type=WITNESS_TYPE,
            value={"version": 2, "future": True},
            signer=seen.secret,
            key=channel_encryption_key(world.merchant.key.secret),
            address=witness_address(seen, address, paying, sale),
            created_at=int(world.now) - 5,
            random=secrets.token_bytes,
            tags=[["o", order_tag(seen, address, sale)], ["o", address_tag(seen, address)]],
        )
        for url in world.relay_urls:
            world.relays[url].store(newer)
        if place == "none":
            # After the lease's end: no check saw T in time, BlockCypher does not know it.
            world.now = invoice.expires_at + 120 + 60
        before = {value["id"] for value in world.received()}
        status = await client.get_status(invoice)
        if place == "none":
            assert (status.state, status.received) == ("expired", 0)
            assert world.http.urls("https://api.blockcypher.com") != []
        else:
            assert (status.state, status.received) == ("open", 0)
        published = [v for v in world.received() if v["id"] not in before]
        assert [v for v in published if v["pubkey"] == seen.author] == []
        assert _held_by_every_relay(world, newer)
        assert any("witness of this address has a newer format" in w for w in world.warnings)
    finally:
        await world.close()


async def test_a_transaction_is_judged_by_the_clock_once_the_explorer_answered() -> None:
    """SPEC.md §31, The on-chain payment: `now` is read once the explorer answered step 1, never
    before (what it lists was broadcast by then). The check starts 5 s before the lease's end;
    the explorer answers 10 s later, listing a payment BlockCypher received 3 s after the end:
    late, never in time."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-30", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        end = invoice.expires_at + 120
        world.now = end - 5
        paying = pay_onchain(world, invoice, 49_000)
        world.blockcypher.received[paying] = rfc3339(end + 3)
        listing = f"https://mempool.space/api/address/{address}/txs"

        def explorer(url: str) -> tuple[int, Any]:
            if url == listing:
                world.now += 10
            return world.explorer.handle(url)

        world.http.route("https://mempool.space/api/", explorer)
        status = await client.get_status(invoice)
        # Its Lightning part, asked before, not final yet: open, nothing received on-chain.
        assert (status.state, status.received) == ("open", 0)
        assert world.http.urls("https://api.blockcypher.com") != []
        assert [(w.txid, w.sale) for w in witnesses_of(world)] == [(paying, None)]
    finally:
        await world.close()


@pytest.mark.parametrize(("left", "expiry"), [(200, 150), (100, None)])
async def test_a_rest_invoice_is_asked_by_the_clock_of_the_decision(
    left: int, expiry: int | None
) -> None:
    """SPEC.md §31, Asking for a rest invoice: `now` is read when the check decides, once its turn
    to ask came, never earlier. Its turn here takes 50 s (the rest invoices read again, the
    Lightning address's pay request asked): with 200 s left when the check began, its rest invoice
    is asked for the 150 s left; with 100 s, none is asked (fewer than 60 s left)."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-31", sats=49_000)
        world.now = invoice.expires_at - left
        pay_onchain(world, invoice, 20_000)
        pay_request = "https://breez.tips/.well-known/lnurlp/shop"
        handle = world.lightning.handle

        def lightning(url: str) -> tuple[int, Any]:
            if url == pay_request:
                world.now += 50
            return handle(url)

        world.http.route("https://breez.tips/", lightning)
        asked = len(world.lightning.asked)
        status = await client.get_status(invoice)
        assert status.state == "open"
        assert status.rest is not None
        assert world.http.urls(pay_request) != []
        if expiry is None:
            assert status.rest.bolt11 is None
            assert len(world.lightning.asked) == asked
        else:
            assert status.rest.bolt11 is not None
            [query] = world.lightning.asked[asked:]
            assert query["expiry"] == str(expiry)
    finally:
        await world.close()


async def test_an_order_paid_onchain_is_found_again() -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-15", sats=49_000)
        paying = pay_onchain(world, invoice, 49_000)
        await client.get_status(invoice)
        world.now = invoice.expires_at + 600
        # Being confirmed: pending, nothing published again.
        with pytest.raises(InvoicePendingError) as pending:
            await client.create_invoice(ref="ORD-15", sats=49_000)
        assert pending.value.invoice.onchain_address == invoice.onchain_address
        confirm(world, paying)
        with pytest.raises(OrderPaidError) as paid:
            await client.create_invoice(ref="ORD-15", sats=49_000)
        assert paid.value.status.method == "onchain"
        assert paid.value.preimage is None
    finally:
        await world.close()


async def test_the_rest_of_an_underpayment() -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-16", sats=49_000, note="Order 16")
        world.now += 60
        pay_onchain(world, invoice, 20_000)
        status = await client.get_status(invoice)
        assert status.state == "open"
        assert status.received == 20_000
        assert status.rest is not None
        assert status.rest.sats == 29_000
        rest_bolt11 = status.rest.bolt11
        assert rest_bolt11 is not None
        assert status.rest.payment_uri == (
            f"bitcoin:{invoice.onchain_address}?amount=0.00029&lightning={rest_bolt11}"
        )
        asked = world.lightning.asked[-1]
        assert asked["amount"] == "29000000"
        assert asked["expiry"] == str(invoice.expires_at - int(world.now))
        assert asked["comment"] == "Order 16"
        # Declared for the sale, its rest named; never an invoice of the order.
        assert [i.payment_hash for i in await client.find_invoices("ORD-16")] == [
            invoice.payment_hash
        ]
        sale = world.sale("ORD-16")
        declared = {}
        for url in world.relay_urls:
            for event in world.held(url, sale.author):
                opened = open_declaration(
                    event.to_json(), sale=sale, registry=world.merchant.registry
                )
                if isinstance(opened, Opened):
                    declared[opened.value.invoice] = opened.value
        rest_declaration = declared[rest_bolt11]
        assert rest_declaration.rest is not None
        assert (rest_declaration.rest.address, rest_declaration.rest.expires_at) == (
            invoice.onchain_address,
            invoice.expires_at,
        )
        assert rest_declaration.note == "Order 16"
        assert invoice.bolt11 is not None
        assert declared[invoice.bolt11].rest is None
        # Not asked again.
        again = await client.get_status(invoice)
        assert again.rest is not None
        assert again.rest.bolt11 == rest_bolt11
        assert len(world.lightning.asked) == 2
        # A mixed payment: the rest in Lightning, the on-chain part 3 deep.
        paid_rest = next(i for i in world.lightning.invoices.values() if i.pr == rest_bolt11)
        world.lightning.pay(paid_rest.payment_hash)
        status = await client.get_status(invoice)
        assert (status.state, status.received) == ("unconfirmed", 49_000)
        for paying in [t["txid"] for t in world.explorer.listed[invoice.onchain_address or ""]]:
            confirm(world, paying)
        status = await client.get_status(invoice)
        assert status.state == "paid"
        assert status.method == "mixed"
        assert status.preimages == (paid_rest.preimage,)
    finally:
        await world.close()


async def test_an_underpaid_pending_invoice_is_handed_out_with_its_rest_invoice() -> None:
    """SPEC.md §21, step 1; §31, Asking for a rest invoice: the check of one invoice that may
    still be paid asks for a rest invoice as any payment check, so that an underpaid one is
    handed out with a rest invoice its checkout links' pages show: declared, and its rest record
    on the relays, before `invoice_pending`."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(
            ref="ORD-23", sats=49_000, note="Order 23", checkout=True
        )
        address = invoice.onchain_address
        assert address is not None
        world.now += 60
        pay_onchain(world, invoice, 20_000)
        with pytest.raises(InvoicePendingError) as pending:
            await client.create_invoice(ref="ORD-23", sats=49_000, note="Order 23", checkout=True)
        assert pending.value.invoice.payment_hash == invoice.payment_hash
        assert pending.value.checkout_url is not None
        # Its rest asked in that call: 29 000 sats, until the invoice's end.
        asked = world.lightning.asked[-1]
        assert asked["amount"] == "29000000"
        assert asked["expiry"] == str(invoice.expires_at - int(world.now))
        [rest] = [i for i in world.lightning.invoices.values() if i.msat == 29_000_000]
        # Its rest record on every relay, which the link's page reads.
        keys = rest_keys(world.merchant.key.secret, "ORD-23", address, invoice.expires_at)
        for url in world.relay_urls:
            shown = [
                opened.value.invoice
                for event in world.held(url, keys.author)
                if isinstance(opened := open_rest_record(event.to_json(), keys, None), Opened)
            ]
            assert shown == [rest.pr]
        # The next check shows it, asking for none.
        status = await client.get_status(invoice)
        assert status.rest is not None
        assert status.rest.bolt11 == rest.pr
        assert len(world.lightning.invoices) == 2
    finally:
        await world.close()


async def test_a_pending_check_and_a_status_at_once_ask_one_rest_invoice() -> None:
    """SPEC.md §31: within one client, the asks of the check of an order's earlier invoices and
    those of `get_status` for one invoice run one at a time, each after the rest invoices were
    read again: the second takes the rest invoice the first declared."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-24", sats=49_000)
        world.now += 60
        pay_onchain(world, invoice, 20_000)
        # Every request slow: both checks reach the ask of the rest together.
        world.http.delay = 0.1
        created, status = await asyncio.gather(
            client.create_invoice(ref="ORD-24", sats=49_000),
            client.get_status(invoice),
            return_exceptions=True,
        )
        assert isinstance(created, InvoicePendingError)
        assert isinstance(status, PaymentStatus)
        [rest] = [i for i in world.lightning.invoices.values() if i.msat == 29_000_000]
        assert status.rest is not None
        assert status.rest.bolt11 == rest.pr
        assert len(world.lightning.invoices) == 2
    finally:
        await world.close()


async def test_a_rest_record_taken_now_is_known_on_a_majority() -> None:
    """SPEC.md §31, Rest records: a record is known on a majority when a majority sent it, or
    took it in this check. Relays a and b take the new rest invoice's record (c refuses it, b
    would refuse it again); the read after it hears a and c (b answers late): still known."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-19", sats=49_000)
        assert invoice.onchain_address is not None
        keys = rest_keys(
            world.merchant.key.secret, "ORD-19", invoice.onchain_address, invoice.expires_at
        )
        _, b, c = (world.relays[url] for url in world.relay_urls)
        records: list[str] = []

        def once(event: Event) -> bool:
            # Relay b takes the first rest record, and no other.
            if event.pubkey != keys.author or event.kind != 30078:
                return True
            records.append(event.id)
            return len(records) == 1

        b.on_event = once
        c.on_event = lambda event: event.pubkey != keys.author
        b.delay = 0.2
        pay_onchain(world, invoice, 20_000)
        status = await client.get_status(invoice)
        assert status.rest is not None
        assert status.rest.bolt11 is not None
        # Not published again: a majority took it.
        assert len(records) == 1
    finally:
        await world.close()


async def test_a_rest_record_of_a_newer_format_holds_its_invoice_and_warns() -> None:
    """SPEC.md §12, §31, Rest records: a record of a newer format that a relay sent at a rest
    invoice's record address is never written over, and the invoice counts as recorded: it is
    the status's `bolt11`. The rest invoice's first record was taken by no relay; then a newer
    version recorded it, on one relay of three (fewer than a majority sent anything there)."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-33", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        keys = rest_keys(world.merchant.key.secret, "ORD-33", address, invoice.expires_at)
        relays = [world.relays[url] for url in world.relay_urls]
        for relay in relays:
            relay.on_event = lambda event: event.pubkey != keys.author
        pay_onchain(world, invoice, 20_000)
        status = await client.get_status(invoice)
        assert status.rest is not None
        assert status.rest.bolt11 is None
        [made] = [i for i in world.lightning.invoices.values() if i.msat == 29_000_000]
        newer = seal(
            record_type=REST_TYPE,
            value={"version": 2, "future": True},
            signer=keys.secret,
            key=keys.encryption_key,
            address=keys.record_address(made.payment_hash),
            created_at=int(world.now) - 1,
            random=secrets.token_bytes,
            tags=[["expiration", str(invoice.expires_at + CHECKOUT_LIFE)]],
        )
        relays[1].store(newer)
        for relay in relays:
            relay.on_event = None
        before = {value["id"] for value in world.received()}
        world.warnings.clear()
        status = await client.get_status(invoice)
        assert status.rest is not None
        assert status.rest.bolt11 == made.pr
        # None asked again, nothing written by the rest author: the newer record is there.
        assert len([i for i in world.lightning.invoices.values() if i.msat == 29_000_000]) == 1
        published = [v for v in world.received() if v["id"] not in before]
        assert [v for v in published if v["pubkey"] == keys.author] == []
        assert any(isinstance(e, Event) and e.id == newer.id for e in relays[1].events.values())
        newer_warned = "record of an invoice of the rest has a newer format"
        assert any(newer_warned in w for w in world.warnings)
    finally:
        await world.close()


async def test_rest_records_unread_are_logged() -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-24", sats=49_000)
        assert invoice.onchain_address is not None
        pay_onchain(world, invoice, 20_000)
        status = await client.get_status(invoice)
        assert status.rest is not None
        assert status.rest.bolt11 is not None
        keys = rest_keys(
            world.merchant.key.secret, "ORD-24", invoice.onchain_address, invoice.expires_at
        )
        # Two relays of three close the reads of the rest author: fewer than a majority answer.
        for url in world.relay_urls[1:]:
            world.relays[url].closed_authors = {keys.author}
        status = await client.get_status(invoice)
        assert status.rest is not None
        assert status.rest.bolt11 is None
        assert any("records of the invoices of the rest were not read" in w for w in world.warnings)
    finally:
        await world.close()


async def test_no_rest_record_published_again_after_a_read_fewer_than_a_majority_answered() -> None:
    """SPEC.md §13, §31, Rest records: when fewer than a majority of the relays answered the read
    of the rest records, none is published again (what the others hold is never written over
    unseen), and every rest invoice but those a majority took in this check counts as not
    recorded: never the status's `bolt11`. The rest invoice's record is on relay a alone (b and c
    refused it); then b and c close the reads of the rest author: nothing is published, though a
    alone sent the record, and no second invoice of the rest is asked (SPEC.md §31, Asking for a
    rest invoice: one of exactly the rest exists, whatever its record); once they answer, it is
    published again."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-36", sats=49_000)
        assert invoice.onchain_address is not None
        keys = rest_keys(
            world.merchant.key.secret, "ORD-36", invoice.onchain_address, invoice.expires_at
        )
        _, b, c = (world.relays[url] for url in world.relay_urls)
        for relay in (b, c):
            relay.on_event = lambda event: event.pubkey != keys.author
        pay_onchain(world, invoice, 20_000)
        status = await client.get_status(invoice)
        assert status.rest is not None
        assert status.rest.bolt11 is None
        [made] = [i for i in world.lightning.invoices.values() if i.msat == 29_000_000]
        # Published, and published again by the read after it: taken by relay a alone.
        assert [url for url in world.relay_urls if world.held(url, keys.author)] == [
            world.relay_urls[0]
        ]
        for relay in (b, c):
            relay.on_event = None
            relay.closed_authors = {keys.author}
        before = len(world.received())
        asked = len(world.lightning.asked)
        world.warnings.clear()
        status = await client.get_status(invoice)
        # No second invoice of the rest asked; no rest record published, nor anything else.
        assert len(world.lightning.asked) == asked
        assert len(world.received()) == before
        assert status.rest is not None
        assert status.rest.bolt11 is None
        warned = (
            "The records of the invoices of the rest were not read (Fewer than a majority of the "
            "relays answered (1 of 3)): read again at the next check"
        )
        assert [w for w in world.warnings if "rest" in w] == [warned]
        for relay in (b, c):
            relay.closed_authors = set()
        status = await client.get_status(invoice)
        assert status.rest is not None
        assert status.rest.bolt11 == made.pr
        for url in world.relay_urls:
            [held] = world.held(url, keys.author)
            opened = open_rest_record(held.to_json(), keys, None)
            assert isinstance(opened, Opened)
            assert opened.value.payment_hash == made.payment_hash
        # One rest invoice asked in all.
        assert [i for i in world.lightning.invoices.values() if i.msat == 29_000_000] == [made]
    finally:
        await world.close()


async def test_a_rest_invoice_just_asked_is_shown_when_the_records_are_unread() -> None:
    """SPEC.md §31, Asking for a rest invoice: its record published on a majority, "It is then
    the status's bolt11", though the read of the rest records after it fails."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-27", sats=49_000)
        assert invoice.onchain_address is not None
        keys = rest_keys(
            world.merchant.key.secret, "ORD-27", invoice.onchain_address, invoice.expires_at
        )
        # Every relay takes the rest record; two of three close the reads of the rest author.
        for url in world.relay_urls[1:]:
            world.relays[url].closed_authors = {keys.author}
        pay_onchain(world, invoice, 20_000)
        status = await client.get_status(invoice)
        assert status.rest is not None
        assert status.rest.bolt11 is not None
        assert any("records of the invoices of the rest were not read" in w for w in world.warnings)
    finally:
        await world.close()


async def test_a_rest_not_asked_of_metadata_with_a_lone_surrogate_is_only_logged() -> None:
    """SPEC.md §16, §23: when a check asks for the invoice of an underpayment's rest and the
    Lightning address answers metadata holding a lone surrogate (no UTF-8 to hash), the failure
    is logged and the status still given: open, its rest without an invoice; the callback is
    never asked."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-37", sats=49_000)
        pay_onchain(world, invoice, 20_000)
        world.lightning.metadata = '[["text/plain","Pay ' + chr(0xD800) + '"]]'
        asked = len(world.lightning.asked)
        status = await client.get_status(invoice)
        assert (status.state, status.received) == ("open", 20_000)
        assert status.rest is not None
        assert (status.rest.sats, status.rest.bolt11) == (29_000, None)
        assert len(world.lightning.asked) == asked
        warned = (
            "No Lightning invoice of the rest of an underpayment (The pay request's metadata is "
            "not a string of text): asked again at the next check"
        )
        assert [w for w in world.warnings if "rest" in w] == [warned]
    finally:
        await world.close()


async def test_the_rest_alone_pays() -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-17", sats=49_000)
        pay_onchain(world, invoice, 20_000)
        first = await client.get_status(invoice)
        assert first.rest is not None
        assert first.rest.bolt11 is not None
        rest_invoice = next(
            i for i in world.lightning.invoices.values() if i.pr == first.rest.bolt11
        )
        world.lightning.pay(rest_invoice.payment_hash)
        # The on-chain payment replaced away: the rest invoice of 29 000 does not pay 49 000.
        world.explorer.listed[invoice.onchain_address or ""] = []
        status = await client.get_status(invoice)
        assert status.state == "open"
        assert status.received == 29_000
    finally:
        await world.close()


@pytest.mark.parametrize("reason", ["too_late", "outside_sendable", "address_fails"])
async def test_a_rest_not_asked(reason: str) -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-18", sats=49_000)
        if reason == "too_late":
            world.now = invoice.expires_at - 59
        elif reason == "outside_sendable":
            world.lightning.min_sendable = 30_000_000
        else:
            world.lightning.refuse_invoices = True
        pay_onchain(world, invoice, 20_000)
        status = await client.get_status(invoice)
        assert status.state == "open"
        assert status.rest is not None
        assert status.rest.bolt11 is None
        assert status.rest.payment_uri == f"bitcoin:{invoice.onchain_address}?amount=0.00029"
        # No invoice of the rest was made.
        assert len(world.lightning.invoices) == 1
        assert len(world.lightning.asked) == (2 if reason == "address_fails" else 1)
        if reason == "address_fails":
            assert any("No Lightning invoice of the rest" in w for w in world.warnings)
            # Asked again at the next check.
            world.lightning.refuse_invoices = False
            status = await client.get_status(invoice)
            assert status.rest is not None
            assert status.rest.bolt11 is not None
    finally:
        await world.close()


async def test_a_rest_below_the_onchain_floor_is_paid_in_lightning_only() -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-19", sats=49_000)
        pay_onchain(world, invoice, 48_500)
        status = await client.get_status(invoice)
        assert status.rest is not None
        assert status.rest.sats == 500
        assert status.rest.payment_uri == f"lightning:{status.rest.bolt11}"
    finally:
        await world.close()


def acceptances(world: World, ref: str, invoice: Invoice) -> list[int]:
    assert invoice.onchain_address is not None
    keys = rest_keys(world.merchant.key.secret, ref, invoice.onchain_address, invoice.expires_at)
    found = []
    for value in world.received():
        opened = open_acceptance(value, keys, invoice.sats)
        if isinstance(opened, Opened):
            found.append(opened.value)
    return found


async def test_the_merchants_tolerance() -> None:
    world = await World.start(tolerance=500)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-20", sats=49_000)
        # The merchant changes its tolerance later: the order keeps the one of its lease.
        world.publish_channel(tolerance=0)
        world.now += 60
        paying = pay_onchain(world, invoice, 46_600, inputs=[f"{txid()}:0"])
        status = await client.get_status(invoice)
        assert status.to_json() == {
            "state": "unconfirmed",
            "received": "46600",
            "confirmations": 0,
            "txids": [paying],
        }
        assert acceptances(world, "ORD-20", invoice) == [46_600]
        # Published once.
        await client.get_status(invoice)
        assert acceptances(world, "ORD-20", invoice) == [46_600]
        # A replacement that lowers what counts, and leaves it accepted: a newer record, of less.
        coin = world.explorer.known[paying]["vin"][0]
        world.explorer.drop(invoice.onchain_address or "", paying)
        replacing = pay_onchain(world, invoice, 46_560, inputs=[f"{coin['txid']}:{coin['vout']}"])
        status = await client.get_status(invoice)
        assert (status.state, status.received, status.txids) == (
            "unconfirmed",
            46_560,
            (replacing,),
        )
        assert sorted(acceptances(world, "ORD-20", invoice)) == [46_560, 46_600]
        confirm(world, replacing)
        status = await client.get_status(invoice)
        assert (status.state, status.method, status.received) == ("paid", "onchain", 46_560)
    finally:
        await world.close()


async def test_short_by_more_than_the_tolerance_asks_the_whole_rest() -> None:
    world = await World.start(tolerance=500)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-21", sats=49_000)
        pay_onchain(world, invoice, 46_549)
        status = await client.get_status(invoice)
        assert status.state == "open"
        assert status.rest is not None
        assert status.rest.sats == 2451
        assert status.rest.bolt11 is not None
        assert acceptances(world, "ORD-21", invoice) == []
    finally:
        await world.close()


async def test_an_acceptance_record_of_a_newer_format_is_never_written_over_and_warns() -> None:
    """SPEC.md §12, §31, Acceptance records: a newer version accepted the payment, in a format
    this one cannot read, on one relay: nothing is published over it, and it is warned of."""
    world = await World.start(tolerance=500)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-34", sats=49_000)
        assert invoice.onchain_address is not None
        keys = rest_keys(
            world.merchant.key.secret, "ORD-34", invoice.onchain_address, invoice.expires_at
        )
        newer = seal(
            record_type=ACCEPTANCE_TYPE,
            value={"version": 2, "future": True},
            signer=keys.secret,
            key=keys.encryption_key,
            address=keys.acceptance_address,
            created_at=int(world.now) - 5,
            random=secrets.token_bytes,
            tags=[["expiration", str(invoice.expires_at + CHECKOUT_LIFE)]],
        )
        world.relays[world.relay_urls[0]].store(newer)
        pay_onchain(world, invoice, 47_000)
        before = {value["id"] for value in world.received()}
        status = await client.get_status(invoice)
        assert (status.state, status.received) == ("unconfirmed", 47_000)
        published = [v for v in world.received() if v["id"] not in before]
        assert [v for v in published if v["pubkey"] == keys.author] == []
        newer_warned = "acceptance record of this order has a newer format"
        assert any(newer_warned in w for w in world.warnings)
    finally:
        await world.close()


async def test_an_acceptance_record_failing_is_only_logged() -> None:
    world = await World.start(tolerance=500)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-22", sats=49_000)
        assert invoice.onchain_address is not None
        keys = rest_keys(
            world.merchant.key.secret, "ORD-22", invoice.onchain_address, invoice.expires_at
        )
        for url in world.relay_urls:
            world.relays[url].on_event = lambda event: event.pubkey != keys.author
        pay_onchain(world, invoice, 47_000)
        status = await client.get_status(invoice)
        assert status.state == "unconfirmed"
        assert any("acceptance record" in w for w in world.warnings)
    finally:
        await world.close()


async def test_no_acceptance_record_after_a_read_fewer_than_a_majority_answered() -> None:
    """SPEC.md §13, §31, Acceptance records: when fewer than a majority of the relays answered
    the read of the invoice's acceptance records, none is published (what the others hold, of a
    newer format perhaps, is never written over unseen): a warning, never the check's error; the
    next check reads and publishes again."""
    world = await World.start(tolerance=500)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-35", sats=49_000)
        assert invoice.onchain_address is not None
        keys = rest_keys(
            world.merchant.key.secret, "ORD-35", invoice.onchain_address, invoice.expires_at
        )

        def acceptance_read(filter_: dict[str, Any]) -> bool:
            return keys.acceptance_address in filter_.get("#d", [])

        # Two relays of three close the read of the acceptance records alone.
        for url in world.relay_urls[1:]:
            world.relays[url].closes = acceptance_read
        pay_onchain(world, invoice, 47_000)
        status = await client.get_status(invoice)
        assert (status.state, status.received) == ("unconfirmed", 47_000)
        assert acceptances(world, "ORD-35", invoice) == []
        assert [v for v in world.received() if v["pubkey"] == keys.author] == []
        warned = (
            "The acceptance record of a payment was not published (Fewer than a majority of the "
            "relays answered (1 of 3)): published again at the next check"
        )
        assert [w for w in world.warnings if "acceptance record" in w] == [warned]
        for url in world.relay_urls[1:]:
            world.relays[url].closes = None
        status = await client.get_status(invoice)
        assert (status.state, status.received) == ("unconfirmed", 47_000)
        assert acceptances(world, "ORD-35", invoice) == [47_000]
    finally:
        await world.close()


async def test_an_acceptance_record_when_the_rest_part_fails() -> None:
    """SPEC.md §31, Acceptance records: whatever the rest part, a status accepted below `sats`
    publishes one. Here the rest part is `network`: its declaration is on one relay of three, and
    publishing it again fails."""
    world = await World.start(tolerance=500)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-23", sats=49_000)
        pay_onchain(world, invoice, 20_000)
        status = await client.get_status(invoice)
        assert status.rest is not None
        assert status.rest.bolt11 is not None
        sale = world.sale("ORD-23")
        _, b, c = (world.relays[url] for url in world.relay_urls)
        rest = next(
            event
            for event in world.held(world.relay_urls[0], sale.author)
            if isinstance(
                opened := open_declaration(
                    event.to_json(), sale=sale, registry=world.merchant.registry
                ),
                Opened,
            )
            and opened.value.rest is not None
        )
        for relay in (b, c):
            relay.events = {
                address: held
                for address, held in relay.events.items()
                if getattr(held, "id", None) != rest.id
            }
            relay.on_event = lambda event: event.pubkey != sale.author
        # The read's majority is relay a and relay b: the declaration on a alone.
        c.delay = 0.2
        pay_onchain(world, invoice, 26_600)
        status = await client.get_status(invoice)
        assert (status.state, status.received) == ("unconfirmed", 46_600)
        # Not published again: the rest part failed.
        assert [url for url in world.relay_urls if rest in world.held(url, sale.author)] == [
            world.relay_urls[0]
        ]
        assert acceptances(world, "ORD-23", invoice) == [46_600]
    finally:
        await world.close()


async def test_a_tolerance_that_is_not_one_is_none() -> None:
    world = await World.start(tolerance=1001)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-23", sats=49_000)
        [lease] = leases_of(world, "ORD-23", invoice.onchain_address or "")
        assert lease.value.terms.tolerance == 0
        pay_onchain(world, invoice, 48_999)
        assert (await client.get_status(invoice)).state == "open"
    finally:
        await world.close()


async def test_without_the_sales_records_no_tolerance_and_no_rest() -> None:
    world = await World.start(tolerance=500)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-24", sats=49_000)
        pay_onchain(world, invoice, 47_000)
        # Relays down: the address's parts cannot be told; Lightning alone can still pay.
        for url in world.relay_urls:
            world.relays[url].down = True
        with pytest.raises(NetworkError):
            await client.get_status(invoice)
        assert invoice.payment_hash is not None
        world.lightning.pay(invoice.payment_hash)
        status = await client.get_status(invoice)
        assert status == PaymentStatus.paid_lightning(
            world.lightning.invoices[invoice.payment_hash].preimage
        )
    finally:
        await world.close()


def test_rest_keys_never_show_their_secrets() -> None:
    keys = rest_keys(Secret(secrets.token_bytes(32)), "ORD-1", deposit_address(), 1)
    for secret in (keys.secret, keys.key, keys.encryption_key):
        assert secret.reveal().hex() not in repr(keys)
        assert secret.reveal().hex() not in str(keys)


def test_the_tolerance_and_a_link_key_never_show() -> None:
    """SPEC.md §27: the tolerance is never in a log nor an error: never in a representation. Nor
    a link key, which reads the link's record."""
    terms = LeaseTerms(
        address=deposit_address(),
        sats=49_000,
        invoice=None,
        verify_url=None,
        created_at=1,
        expires_at=901,
        price=leases_price(),
        note="ORD-1",
        tolerance=731,
    )
    channel = ActiveChannel(
        business_name="Sunrise Bakery",
        lightning_address="shop@breez.tips",
        registry_key=Secret(secrets.token_bytes(32)),
        registry_reader="0" * 64,
        relays=None,
        checkout_origin=None,
        tolerance=731,
        updated_at=1,
    )
    assert terms.tolerance == channel.tolerance == 731
    for shown in (repr(terms), repr(channel)):
        assert "731" not in shown
        assert "tolerance" not in shown
    fragment = Fragment(author="0" * 64, link_key="7" * 64, relays=("wss://relay.example",))
    assert fragment.link_key not in repr(fragment)


def _description_words(length: int) -> int:
    """The description words that make the fake Lightning address's invoice of 49 000 sats
    (900 s) `length` characters long."""
    words = 0
    while True:
        made = bolt11(
            msat=49_000_000,
            payment_hash=bytes(32),
            description_hash=bytes(32),
            timestamp=START,
            expiry=900,
            description_words=words,
        )
        if len(made) >= length:
            assert len(made) == length
            return words
        words += max(1, (length - len(made)) // 2)


@pytest.mark.parametrize("lot", ["free", "paid"])
async def test_an_invoice_its_lease_cannot_hold_is_refused_before_anything_is_published(
    lot: str,
) -> None:
    """SPEC.md §17, step 6: the lease of an address holds the invoice too, sealed twice as the
    declaration, with some 150 bytes more. An invoice of 40 550 characters fits its declaration
    (its inside within NIP-44's padded 40 960 bytes) and not its lease (padded to 49 152 bytes,
    too large once sealed again): invalid_invoice before anything is published, never NIP-44's
    own error, whether an address could be leased or not (the lot's addresses all paid: a witness
    `none` of each would be published, and the invoice made Lightning only)."""
    world = await World.start(lot=3)
    try:
        world.lightning.description_words = _description_words(40_550)
        if lot == "paid":
            for address in world.lot:
                world.explorer.add(address, tx(txid(), {address: 10_000}))
        with pytest.raises(InvalidInvoiceError, match="too large for the lease of an address"):
            await world.client().create_invoice(ref="ORD-60", sats=49_000)
        [asked] = world.lightning.invoices.values()
        assert len(asked.pr) == 40_550
        # Nothing published, and the explorer never asked of an address.
        assert world.received() == []
        assert world.received(kind=1) == []
        assert world.http.urls("https://mempool.space/api/address/") == []
    finally:
        await world.close()


def _lease_fits_by_definition(terms: LeaseTerms, sale: Any, registry: Registry) -> bool:
    """SPEC.md §31, Leases: its inside fits NIP-44, then its outside, the inside sealed, does."""
    from sattleme_checkout import _json, _nip44
    from sattleme_checkout._constants import LEASE_TYPE
    from sattleme_checkout._derive import inner_key
    from sattleme_checkout._lease import lease_inside
    from sattleme_checkout._records import plaintext_fits

    inside = _json.dumps(lease_inside(terms, sale))
    if not _nip44.fits(inside):
        return False
    sealed = _nip44.encrypt(inside, inner_key(sale.secret, registry.reader), bytes(32))
    return plaintext_fits(LEASE_TYPE, {"version": 1, "end": terms.end, "sealed": sealed})


@pytest.mark.parametrize(
    "length", [100, 40_000, 40_500, 40_520, 40_540, 40_560, 40_580, 40_700, 49_000, 70_000]
)
def test_the_size_of_a_lease_is_told_without_drawing_a_random_byte(length: int) -> None:
    """`lease_fits` says what sealing the lease makes, for every deposit address alike (they have
    one length), and draws no random byte: a lease too large is None before one is drawn."""
    world_key = Secret(secrets.token_bytes(32))
    sale = sale_keys(world_key, "ORD-1")
    registry = Registry(
        key=Secret(secrets.token_bytes(32)), reader=xonly_public_key(world_key).hex()
    )
    terms = LeaseTerms(
        address=deposit_address(),
        sats=49_000,
        invoice="lnbc1" + "q" * (length - 5),
        verify_url="https://ln.example/verify",
        created_at=1,
        expires_at=901,
        price=leases_price(),
        note="ORD-1",
        tolerance=500,
    )
    drawn: list[int] = []

    def random(size: int) -> bytes:
        drawn.append(size)
        return secrets.token_bytes(size)

    fits = lease_fits(terms, sale)
    assert drawn == []
    assert fits is _lease_fits_by_definition(terms, sale, registry)
    assert lease_fits(dataclasses.replace(terms, address=SIZED_ADDRESS), sale) is fits
    event = lease_event(registry=registry, sale=sale, terms=terms, created_at=1, random=random)
    assert (event is not None) is fits
    # Its two nonces and its signature's auxiliary bytes, once it fits; none otherwise.
    assert drawn == ([32] * 3 if fits else [])
    if length in (100, 70_000):
        assert fits is (length == 100)


# The verify URL of the fake Lightning address's invoices, but for their payment hash.
VERIFY_URL = "https://breez.tips/lnurlp/shop/verify/"


def _first_lease(invoice: str, verify_url: str, note: str) -> LeaseTerms:
    """The terms of the lease `create_invoice` makes of an order of 49 000 sats (900 s), as
    first published (SPEC.md §31, Leases: `"confirmed": false`), at a deposit address."""
    return LeaseTerms(
        address=SIZED_ADDRESS,
        sats=49_000,
        invoice=invoice,
        verify_url=verify_url,
        created_at=START,
        expires_at=START + 900,
        price=DeclaredPrice(minor=49_000, digits=0, currency=SATS_CURRENCY),
        note=note,
        tolerance=0,
    )


async def test_a_lease_is_measured_as_first_published_unconfirmed() -> None:
    """SPEC.md §17, step 6: the lease is measured as first published, `"confirmed": false` (the
    longer of its two forms), with a deposit address of 62 characters (every one has that
    length). An invoice whose lease fits with `"confirmed": true` but not with `false` (its inside
    40 960 bytes as marked, NIP-44's padding then 40 960, and 40 961 as first published, padded
    to 49 152: too large once sealed again) is invalid_invoice before the lot, the leases or the
    explorer is asked, nothing published; a note one character shorter, and the order gets its
    address."""
    world = await World.start(lot=3)
    try:
        assert len(SIZED_ADDRESS) == 62
        assert all(len(address) == 62 for address in world.lot)
        world.lightning.description_words = _description_words(40_425)
        inside = _json.dumps(
            lease_inside(
                _first_lease("l" * 40_425, VERIFY_URL + "0" * 64, ""),
                world.sale("ORD-62"),
            )
        )
        note = "N" * (40_961 - len(inside.encode("utf-8")))
        with pytest.raises(InvalidInvoiceError, match="too large for the lease of an address"):
            await world.client().create_invoice(ref="ORD-62", sats=49_000, note=note)
        [asked] = world.lightning.invoices.values()
        made = _first_lease(asked.pr, VERIFY_URL + asked.payment_hash, note)
        sale = world.sale("ORD-62")
        assert lease_fits(dataclasses.replace(made, confirmed=True), sale)
        assert not lease_fits(made, sale)
        # Nothing asked of the lot, the leases or the explorer; nothing published.
        lot = lot_address(world.merchant.key.secret)
        for relay in world.relays.relays.values():
            assert not any(lot in f.get("#d", ()) or "since" in f for f in relay.requests)
        assert world.http.urls("https://mempool.space/api/address/") == []
        assert world.received() == []
        # One character less: it fits as first published.
        invoice = await world.client().create_invoice(ref="ORD-63", sats=49_000, note=note[1:])
        assert invoice.onchain_address in world.lot
    finally:
        await world.close()


def test_a_lease_too_large_is_none_never_an_exception() -> None:
    """The caller raises invalid_invoice from a frame without the sealed terms (SPEC.md §27)."""
    world_key = Secret(secrets.token_bytes(32))
    sale = sale_keys(world_key, "ORD-1")
    registry = Registry(
        key=Secret(secrets.token_bytes(32)), reader=xonly_public_key(world_key).hex()
    )
    terms = LeaseTerms(
        address=deposit_address(),
        sats=49_000,
        invoice="lnbc1" + "q" * 49_000,
        verify_url="https://ln.example/verify",
        created_at=1,
        expires_at=901,
        price=leases_price(),
        note="ORD-1",
        tolerance=500,
    )
    event = lease_event(
        registry=registry, sale=sale, terms=terms, created_at=1, random=secrets.token_bytes
    )
    assert event is None
    small = dataclasses.replace(terms, invoice="lnbc1" + "q" * 100)
    assert lease_event(
        registry=registry, sale=sale, terms=small, created_at=1, random=secrets.token_bytes
    )


async def test_a_leased_address_goes_to_no_other_order_until_its_lease_ends() -> None:
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        first = await world.client().create_invoice(ref="ORD-25", sats=49_000)
        assert first.onchain_address == address
        # Another server, later: the address is leased, it gets none.
        second = await world.client().create_invoice(ref="ORD-26", sats=49_000)
        assert second.onchain_address is None
        assert leases_of(world, "ORD-26", address) == []
        # The lease's end and the clock tolerance past, unpaid: free again.
        world.now = first.expires_at + 120 + 300
        third = await world.client().create_invoice(ref="ORD-27", sats=49_000)
        assert third.onchain_address is None
        world.now += 1
        fourth = await world.client().create_invoice(ref="ORD-28", sats=49_000)
        assert fourth.onchain_address == address
    finally:
        await world.close()
