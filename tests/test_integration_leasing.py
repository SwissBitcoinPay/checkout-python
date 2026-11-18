"""Integration tests of leasing a deposit address (SPEC.md §31, Leasing an address; PORTING.md,
Steps, 5): a lease confirmed then marked, an address a witness names skipped, leases read among
many records anybody wrote, a lease that was not confirmed withdrawn and never giving its invoice
an address, two calls of one order at once, a relay that hides a lease while another one that
holds it answers late, and a mute relay that delays the reads on every relay one timeout at
most."""

from __future__ import annotations

import asyncio
import secrets
import time
import tracemalloc
from typing import Any

import pytest

from fakes.chain import txid
from fakes.registry import leases_of, taken_records
from fakes.relay import Copies
from fakes.world import World
from sattleme_checkout import NetworkError, OnchainUnavailableError
from sattleme_checkout._constants import WITNESS_TYPE
from sattleme_checkout._declaration import DeclaredPrice
from sattleme_checkout._derive import (
    address_tag,
    channel_encryption_key,
    onchain_registry_address,
    outer_key,
    seen_keys,
    witness_address,
)
from sattleme_checkout._lease import LeaseTerms, lease_event, withdrawal_event
from sattleme_checkout._leasing import read_leases
from sattleme_checkout._nostr import Event, sign_event
from sattleme_checkout._records import seal
from sattleme_checkout._relay import RelayPool, fresh_key
from sattleme_checkout._witness import Witness, witness_event


def store(world: World, event: Event, on: list[str] | None = None) -> None:
    for url in world.relay_urls if on is None else on:
        world.relays[url].store(event)


def rival_lease(world: World, ref: str, address: str, created_at: int) -> Event:
    """Another order's lease of `address`: a holder of the registry key wrote it."""
    terms = LeaseTerms(
        address=address,
        sats=1000,
        invoice=None,
        verify_url=None,
        created_at=created_at,
        expires_at=created_at + 900,
        price=DeclaredPrice(minor=1000, digits=0, currency="SAT"),
        note=ref,
        tolerance=0,
        confirmed=True,
    )
    event = lease_event(
        registry=world.merchant.registry,
        sale=world.sale(ref),
        terms=terms,
        created_at=created_at,
        random=secrets.token_bytes,
    )
    assert event is not None
    return event


def anybodys(world: World, address: str, created_at: int, filler: int = 0) -> Event:
    """An event anybody may write at the lease's `d` tag of `address` (it is public), its
    content `filler` characters longer than its own 140."""
    registry_address = onchain_registry_address(world.merchant.registry_key, address)
    return sign_event(
        fresh_key(secrets.token_bytes),
        created_at=created_at,
        kind=30078,
        tags=[["d", registry_address]],
        content=secrets.token_hex(70) + "x" * filler,
        aux=secrets.token_bytes(32),
    )


async def test_a_lease_confirmed_then_marked_gives_its_address() -> None:
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        invoice = await world.client().create_invoice(ref="ORD-40", sats=49_000)
        assert invoice.onchain_address == address
        lease, mark = taken_records(world, "ORD-40", address)
        assert lease.value.terms.confirmed is False
        assert mark.value.terms.confirmed is True
        # Read again on every relay before its mark.
        lease_reads = [
            [request for request in world.relays[url].requests if "since" in request]
            for url in world.relay_urls
        ]
        assert all(len(reads) == 2 for reads in lease_reads)
        assert all(reads[0]["limit"] == 100 for reads in lease_reads)
    finally:
        await world.close()


@pytest.mark.parametrize("named_by", ["none", "a sale", "a newer format", "a forged witness"])
async def test_an_address_a_witness_names_is_skipped(named_by: str) -> None:
    """SPEC.md §31, step 3: an address a witness names is not free (an event of the seen author
    that checks out, with the address's tag as an `o` tag), whatever the explorer shows."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        seen = seen_keys(world.merchant.key.secret)
        channel_key = channel_encryption_key(world.merchant.key.secret)
        paying = txid()
        if named_by == "a newer format":
            event = seal(
                record_type=WITNESS_TYPE,
                value={"version": 2},
                signer=seen.secret,
                key=channel_key,
                address=witness_address(seen, address, paying, None),
                created_at=int(world.now) - 60,
                random=secrets.token_bytes,
                tags=[["o", address_tag(seen, address)]],
            )
        else:
            sale = None if named_by == "none" else world.sale("ORD-OLD").author
            witness = Witness(
                address=address,
                txid=paying,
                sale=sale,
                sats=5000,
                inputs=(f"{'1' * 64}:0",),
                replacement=False,
                seen_at=int(world.now) - 60,
                expires_at=None if sale is None else int(world.now) - 1000,
            )
            signer = fresh_key(secrets.token_bytes) if named_by == "a forged witness" else seen
            if named_by == "a forged witness":
                forged = witness_event(seen, channel_key, witness, secrets.token_bytes)
                event = sign_event(
                    signer,  # type: ignore[arg-type]
                    created_at=forged.created_at,
                    kind=forged.kind,
                    tags=[list(tag) for tag in forged.tags],
                    content=forged.content,
                    aux=secrets.token_bytes(32),
                )
            else:
                event = witness_event(seen, channel_key, witness, secrets.token_bytes)
        store(world, event)
        # The explorer shows nothing paying it (the transaction replaced, or dropped).
        invoice = await world.client().create_invoice(ref="ORD-41", sats=49_000)
        if named_by == "a forged witness":
            assert invoice.onchain_address == address
        else:
            assert invoice.onchain_address is None
            assert not any(url.endswith(f"/address/{address}/txs") for url in world.http.urls())
    finally:
        await world.close()


@pytest.mark.parametrize("onchain_only", [False, True])
@pytest.mark.parametrize("written", [5, -5], ids=["after the lease", "before the lease"])
async def test_leases_read_among_many_records_anybody_wrote(
    onchain_only: bool, written: int
) -> None:
    """A read of leases that cannot be complete (one second holds more events than a page)
    leaves the order without that address, never with one another order holds: written before
    the order's lease, the read shows its lease, not the other order's."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        now = int(world.now)
        rival = rival_lease(world, "ORD-OTHER", address, now - 10)
        store(world, rival)
        for _ in range(101):
            store(world, anybodys(world, address, now + written))
        client = world.client()
        if onchain_only:
            with pytest.raises(OnchainUnavailableError):
                await client.create_invoice(ref="ORD-42", sats=5_000_000)
        else:
            invoice = await client.create_invoice(ref="ORD-42", sats=49_000)
            assert invoice.onchain_address is None
            assert invoice.bolt11 is not None
        # Published unconfirmed, not confirmed (the read is not complete): withdrawn, a second
        # after it.
        lease, withdrawal = taken_records(world, "ORD-42", address)
        assert lease.value.terms.confirmed is False
        assert withdrawal.value.terms is None
        assert withdrawal.event.created_at == lease.event.created_at + 1
        [kept] = leases_of(world, "ORD-OTHER", address)
        assert kept.event.id == rival.id
        found = await client.find_invoices("ORD-42")
        assert [i.onchain_address for i in found] == ([] if onchain_only else [None])
    finally:
        await world.close()


async def test_leases_read_among_many_large_events_end_incomplete_never_failed() -> None:
    """SPEC.md §13, Limits: events anybody wrote at the lease's `d` tag, large ones, on every
    relay: a read of leases keeps only what it needs of each (nothing piles up) and ends
    incomplete, never failed: the order goes without that address, never `network`."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        now = int(world.now)
        for _ in range(101):
            store(world, anybodys(world, address, now + 5, filler=250_000))
        client = world.client(relay_timeout_ms=10_000)
        tracemalloc.start()
        try:
            invoice = await client.create_invoice(ref="ORD-44", sats=49_000)
            _held, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert invoice.onchain_address is None
        assert invoice.bolt11 is not None
        # Some 300 MB of events read (100 a page, two pages a relay, two reads), a few at once.
        assert peak < 32 << 20
        lease, withdrawal = taken_records(world, "ORD-44", address)
        assert lease.value.terms.confirmed is False
        assert withdrawal.value.terms is None
    finally:
        await world.close()


async def test_a_damaged_copy_on_one_relay_never_hides_the_copies_of_the_others() -> None:
    """SPEC.md §11, §31: each relay's copy of a record is one more signature to try. Another
    order's lease was withdrawn: the first relay to answer sends the withdrawal with a damaged
    signature, the others send it as it was signed. The withdrawal ends the lease (its signature
    checks out on a copy): the address is free, and leased for the order."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        now = int(world.now)
        other = world.sale("ORD-OTHER")
        rival = rival_lease(world, "ORD-OTHER", address, now - 20)
        withdrawn = withdrawal_event(
            registry=world.merchant.registry,
            sale=other,
            address=address,
            created_at=now - 10,
            random=secrets.token_bytes,
        )
        first, *others = world.relay_urls
        store(world, rival)
        store(world, withdrawn, on=others)
        flipped = ("0" if withdrawn.sig[0] != "0" else "1") + withdrawn.sig[1:]
        world.relays[first].put({**withdrawn.to_json(), "sig": flipped})
        # The damaged copy comes first in every read of leases.
        for index, url in enumerate(others):
            world.relays[url].late = lambda filter_: "since" in filter_
            world.relays[url].late_by = 0.05 * (index + 1)
        invoice = await world.client().create_invoice(ref="ORD-45", sats=49_000)
        assert invoice.onchain_address == address
    finally:
        await world.close()


async def test_copies_of_a_real_lease_under_other_ids_never_pile_up() -> None:
    """SPEC.md §13, Limits; §31, Opening a lease: anybody can send copies of a real lease under
    ids of their own, which open as it does (its signature is not checked to open it). A read of
    leases keeps a few bytes of each, never its tags nor its content: some 100 MB of them, a few
    MiB in memory, and the read incomplete, never failed."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        now = int(world.now)
        lease = rival_lease(world, "ORD-OTHER", address, now - 10)
        socket = Copies(lease, count=1000, size=100_000)

        async def connect(_url: str) -> Copies:
            return socket

        tracemalloc.start()
        try:
            async with RelayPool(
                connect=connect, timeout_ms=30_000, random=secrets.token_bytes, now=lambda: now
            ) as pool:
                read = await read_leases(
                    pool, world.merchant.registry, [address], ["wss://a.example"], now
                )
            _held, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        records = read.records[address]
        # Every copy opened, kept once per id: more than a page, the read is not complete.
        assert len(records) == 1000
        assert {record.pubkey for record in records} == {lease.pubkey}
        assert read.answered and not read.complete
        assert peak < 16 << 20
    finally:
        await world.close()


async def test_a_relay_that_ignores_the_limit_reads_incompletely_alone() -> None:
    """One relay answers without the limit, oldest first, more than a page: its read is not
    complete, the others' are, a majority: the lease is confirmed."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        now = int(world.now)
        ignoring = world.relays[world.relay_urls[0]]
        ignoring.ignore_limit = True
        for second in range(150):
            store(world, anybodys(world, address, now - 1000 + second), on=[world.relay_urls[0]])
        invoice = await world.client().create_invoice(ref="ORD-43", sats=49_000)
        assert invoice.onchain_address == address
    finally:
        await world.close()


async def test_a_lease_not_confirmed_never_gives_its_invoice_an_address() -> None:
    """Its withdrawal refused: the lease holds its address until its end, never the order's
    (SPEC.md §21: a lease that is not confirmed gives nothing), and no order leases it then."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        now = int(world.now)
        registry_address = onchain_registry_address(world.merchant.registry_key, address)
        for _ in range(101):
            store(world, anybodys(world, address, now + 5))
        sale = world.sale("ORD-44")
        for url in world.relay_urls:
            world.relays[url].on_event = lambda event: (
                not (
                    event.pubkey == sale.author
                    and event.tag("d") == registry_address
                    and event.created_at == now + 1
                )
            )
        client = world.client()
        with pytest.raises(OnchainUnavailableError):
            await client.create_invoice(ref="ORD-44", sats=5_000_000)
        [lease] = leases_of(world, "ORD-44", address)
        assert lease.value.terms.confirmed is False
        assert await client.find_invoices("ORD-44") == []
        assert any("not withdrawn" in warning for warning in world.warnings)
        for url in world.relay_urls:
            world.relays[url].on_event = None
            # The events anybody wrote gone: the address could be read completely again.
            events = world.relays[url].events
            for key in [
                k
                for k, v in events.items()
                if isinstance(v, Event)
                and v.pubkey != sale.author
                and v.tag("d") == registry_address
            ]:
                del events[key]
        # The order does without the addresses its earlier calls tried; another order without
        # an address its lease holds.
        with pytest.raises(OnchainUnavailableError):
            await client.create_invoice(ref="ORD-44", sats=5_000_000)
        with pytest.raises(OnchainUnavailableError):
            await client.create_invoice(ref="ORD-45", sats=5_000_000)
    finally:
        await world.close()


async def test_two_calls_of_one_order_at_once_never_withdraw_each_others_lease() -> None:
    """Two servers of one shop, the order not locked across them (SPEC.md §17): their records of
    the address, of one author, replace each other; neither withdraws the other's."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        invoices = await asyncio.gather(
            world.client().create_invoice(ref="ORD-46", sats=49_000),
            world.client().create_invoice(ref="ORD-46", sats=49_000),
        )
        holding = [invoice for invoice in invoices if invoice.onchain_address == address]
        assert len(holding) <= 1
        records = taken_records(world, "ORD-46", address)
        assert all(record.value.terms is not None for record in records)
        held = leases_of(world, "ORD-46", address)
        if holding:
            [invoice] = holding
            assert any(
                record.value.terms.confirmed and record.value.terms.invoice == invoice.bolt11
                for record in held
            )
    finally:
        await world.close()


async def test_a_relay_that_hides_a_lease_while_another_holds_it_late() -> None:
    """The lease's confirmation reads on every relay (SPEC.md §13): another order's lease, which
    only a late relay sends, keeps the address from this order."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        late = world.relay_urls[2]
        rival = rival_lease(world, "ORD-OTHER", address, int(world.now) - 10)
        store(world, rival, on=[late])
        world.relays[late].late = lambda filter_: "since" in filter_
        world.relays[late].late_by = 0.5
        started = time.monotonic()
        invoice = await world.client().create_invoice(ref="ORD-47", sats=49_000)
        assert time.monotonic() - started < 1.9
        assert invoice.onchain_address is None
        lease, withdrawal = taken_records(world, "ORD-47", address)
        assert lease.value.terms.confirmed is False
        assert withdrawal.value.terms is None
    finally:
        await world.close()


def _dummies(registry_address: str, count: int, created_at: int) -> list[dict[str, Any]]:
    """Events a relay sends at a lease's `d` tag (anybody may write there): none checks out."""
    return [
        {
            "id": secrets.token_hex(32),
            "pubkey": secrets.token_hex(32),
            "created_at": created_at,
            "kind": 30078,
            "tags": [["d", registry_address]],
            "content": "x",
            "sig": "00" * 64,
        }
        for _ in range(count)
    ]


async def test_a_relay_still_reading_the_confirmation_has_not_answered() -> None:
    """SPEC.md §13, Reads on every relay; §31, Leasing an address, step 3.3: b is down; a's read
    of the lease's confirmation ends incomplete (101 events more, the limit ignored); c sent a
    full first page and is still reading its second when the read goes on (a majority can no
    longer read completely). Only a answered: `network`, never an order without its address
    (counting c as an answer would withdraw the lease and make a Lightning-only invoice)."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        registry_address = onchain_registry_address(world.merchant.registry_key, address)
        a, b, c = (world.relays[url] for url in world.relay_urls)
        b.down = True

        def confirmation(relay: Any, filter_: dict[str, Any]) -> bool:
            """The confirmation's read: the address's registry address alone, once the relay
            holds the order's lease (the read of step 2 asks the same before it is published)."""
            return filter_.get("#d") == [registry_address] and any(
                isinstance(e, Event) and e.tag("d") == registry_address
                for e in relay.events.values()
            )

        now = int(world.now)
        a.ignore_limit = True
        a.forge = lambda f: _dummies(registry_address, 101, now - 5) if confirmation(a, f) else []
        # Its read ends once c's first page has come.
        a.late, a.late_by = (lambda f: confirmation(a, f)), 0.5
        c.forge = lambda f: (
            _dummies(registry_address, 99, now - 5)
            if confirmation(c, f) and "until" not in f
            else []
        )
        c.stalls = lambda f: confirmation(c, f) and "until" in f
        # c's timeout far beyond what the call takes, however busy the machine.
        client = world.client(relay_timeout_ms=60_000)
        started = time.monotonic()
        with pytest.raises(NetworkError):
            await client.create_invoice(ref="ORD-60", sats=49_000)
        # At a's end, not at c's timeout.
        assert time.monotonic() - started < 50
        assert any("until" in f for f in c.requests)
        # The lease was not confirmed: withdrawn.
        lease, withdrawal = taken_records(world, "ORD-60", address)
        assert lease.value.terms.confirmed is False
        assert withdrawal.value.terms is None
    finally:
        await world.close()


async def test_a_mute_relay_delays_the_confirmation_one_timeout_and_once_per_call() -> None:
    """A mute relay: each read on every relay waits for it one timeout at most, and not again
    once a request to it ran out of time in the call (SPEC.md §13)."""
    world = await World.start(lot=2)
    try:
        first, second = world.lot
        world.relays[world.relay_urls[2]].mute = True
        other = world.sale("ORD-OTHER")
        injected: list[Event] = []

        def at_once(event: Event, *_: Any) -> bool:
            # As the first lease arrives, another server's lease of the same address too.
            d = event.tag("d")
            if not injected and event.pubkey != other.author and d is not None:
                for address in (first, second):
                    if d == onchain_registry_address(world.merchant.registry_key, address):
                        rival = rival_lease(world, "ORD-OTHER", address, int(world.now))
                        injected.append(rival)
                        store(world, rival, on=world.relay_urls[:2])
            return True

        for url in world.relay_urls[:2]:
            world.relays[url].on_event = at_once
        client = world.client(relay_timeout_ms=1000)
        started = time.monotonic()
        invoice = await client.create_invoice(ref="ORD-48", sats=49_000)
        elapsed = time.monotonic() - started
        # Two confirmations, the mute relay waited for once: one timeout, not two.
        assert invoice.onchain_address is not None
        assert 0.9 < elapsed < 1.7
    finally:
        await world.close()


async def test_find_invoices_publishes_nothing() -> None:
    """SPEC.md §24: to show an order's invoice again, createInvoice hands it out."""
    world = await World.start(lot=1)
    try:
        client = world.client()
        await client.create_invoice(ref="ORD-49", sats=49_000)
        taken = {url: len(world.relays[url].received) for url in world.relay_urls}
        [found] = await client.find_invoices("ORD-49")
        assert found.onchain_address is not None
        assert {url: len(world.relays[url].received) for url in world.relay_urls} == taken
    finally:
        await world.close()


async def test_an_order_does_without_the_addresses_its_earlier_calls_tried() -> None:
    """SPEC.md §31, step 3: where a record of the sale is (its signature checked), an earlier
    call of the order tried the address: a new lease there could be of the second of its
    withdrawal, and either replace the other. Another order may lease it."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        registry_address = onchain_registry_address(world.merchant.registry_key, address)
        spam = [anybodys(world, address, int(world.now) + 5) for _ in range(101)]
        for event in spam:
            store(world, event)
        client = world.client()
        first = await client.create_invoice(ref="ORD-50", sats=500_000, duration_seconds=60)
        assert first.onchain_address is None
        [withdrawal] = leases_of(world, "ORD-50", address)
        assert withdrawal.value.terms is None
        for url in world.relay_urls:
            relay = world.relays[url]
            for key in [k for k, v in relay.events.items() if v in spam]:
                del relay.events[key]
        # The first invoice ended: the order may have another.
        world.now += 60 + 121
        again = await client.create_invoice(ref="ORD-50", sats=500_000)
        assert again.onchain_address is None
        other = await client.create_invoice(ref="ORD-51", sats=500_000)
        assert other.onchain_address == address
        assert registry_address
    finally:
        await world.close()


async def test_a_record_of_a_newer_format_leaves_no_lease_there() -> None:
    """SPEC.md §21: the newest record of the sale at a registry address, of a newer format,
    leaves no lease there (a warning says to update); and it holds the address (§31)."""
    world = await World.start(lot=1)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-52", sats=5_000_000)
        address = invoice.onchain_address
        assert address is not None
        assert [i.onchain_address for i in await client.find_invoices("ORD-52")] == [address]
        sale = world.sale("ORD-52")
        registry_address = onchain_registry_address(world.merchant.registry_key, address)
        newer = seal(
            record_type="registry-onchain",
            value={"version": 2},
            signer=sale.secret,
            key=outer_key(world.merchant.registry_key, registry_address, sale.author),
            address=registry_address,
            created_at=int(world.now) + 10,
            random=secrets.token_bytes,
        )
        # On one relay, the mark on the others: the read, at a majority with it, takes both.
        store(world, newer, on=world.relay_urls[:1])
        late = world.relays[world.relay_urls[2]]
        late.late = lambda filter_: filter_.get("authors") == [sale.author]
        late.late_by = 0.5
        assert await client.find_invoices("ORD-52") == []
        late.late = None
        assert any("newer format" in warning for warning in world.warnings)
        with pytest.raises(OnchainUnavailableError):
            await client.create_invoice(ref="ORD-53", sats=5_000_000)
    finally:
        await world.close()


async def test_a_lease_of_a_newer_format_holds_its_address_and_warns() -> None:
    """SPEC.md §12, §31: a lease of a newer format (a value the outer key seals) holds its
    address, never written over, and says to update."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        other = world.sale("ORD-OTHER")
        registry_address = onchain_registry_address(world.merchant.registry_key, address)
        newer = seal(
            record_type="registry-onchain",
            value={"version": 2},
            signer=other.secret,
            key=outer_key(world.merchant.registry_key, registry_address, other.author),
            address=registry_address,
            created_at=int(world.now) - 5,
            random=secrets.token_bytes,
        )
        store(world, newer)
        invoice = await world.client().create_invoice(ref="ORD-54", sats=49_000)
        assert invoice.onchain_address is None
        assert any(
            "lease of the integration's addresses has a newer format" in w for w in world.warnings
        )
    finally:
        await world.close()
