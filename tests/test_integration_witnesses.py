"""Integration tests of the witnesses (SPEC.md §31, Witnesses, The on-chain payment; PORTING.md,
Steps, 5): a witness written with both its tags and its invoice's `expiresAt`, a transaction
another invoice's witness names counting for nothing, a relay that hides such a witness while
another one that holds it answers late, and one transaction counting for one invoice at most."""

from __future__ import annotations

import asyncio
import secrets

import pytest

from fakes.chain import tx, txid
from fakes.world import World
from sattleme_checkout._constants import WITNESS_TYPE
from sattleme_checkout._derive import (
    address_tag,
    channel_encryption_key,
    onchain_registry_address,
    order_tag,
    seen_keys,
    witness_address,
)
from sattleme_checkout._records import seal
from sattleme_checkout._witness import Witness, witness_event, witness_value
from sattleme_checkout.models import Invoice


def pay(world: World, invoice: Invoice, sats: int = 49_000) -> str:
    assert invoice.onchain_address is not None
    paying = txid()
    world.explorer.add(invoice.onchain_address, tx(paying, {invoice.onchain_address: sats}))
    return paying


def store_witness(world: World, witness: Witness, on: list[str] | None = None) -> None:
    seen = seen_keys(world.merchant.key.secret)
    event = witness_event(
        seen, channel_encryption_key(world.merchant.key.secret), witness, secrets.token_bytes
    )
    for url in world.relay_urls if on is None else on:
        world.relays[url].store(event)


def of_sale(world: World, ref: str, address: str, paying: str, expires_at: int) -> Witness:
    return Witness(
        address=address,
        txid=paying,
        sale=world.sale(ref).author,
        sats=49_000,
        inputs=(f"{'f' * 64}:0",),
        replacement=False,
        seen_at=int(world.now) - 10,
        expires_at=expires_at,
    )


async def test_a_witness_has_both_tags_and_its_invoices_end() -> None:
    world = await World.start(lot=1)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-60", sats=49_000)
        assert invoice.onchain_address is not None
        paying = pay(world, invoice)
        relay = world.relays[world.relay_urls[0]]
        before = len(relay.requests)
        status = await client.get_status(invoice)
        assert (status.state, status.received) == ("unconfirmed", 49_000)
        seen = seen_keys(world.merchant.key.secret)
        [event] = [e for e in world.received() if e["pubkey"] == seen.author]
        sale = world.sale("ORD-60").author
        assert event["tags"][1:] == [
            ["o", order_tag(seen, invoice.onchain_address, sale)],
            ["o", address_tag(seen, invoice.onchain_address)],
        ]
        # Read again on every relay once on a majority: the witness read twice.
        reads = [r for r in relay.requests[before:] if "#o" in r]
        assert len(reads) == 2
        assert address_tag(seen, invoice.onchain_address) in reads[0]["#o"]
        assert paying
    finally:
        await world.close()


@pytest.mark.parametrize("of", ["another sale", "another invoice of the order"])
async def test_a_transaction_another_invoices_witness_names_counts_for_nothing(of: str) -> None:
    world = await World.start(lot=1)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-61", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        paying = pay(world, invoice)
        if of == "another sale":
            store_witness(world, of_sale(world, "ORD-OTHER", address, paying, invoice.expires_at))
        else:
            store_witness(world, of_sale(world, "ORD-61", address, paying, invoice.expires_at - 5))
        taken = len(world.received())
        status = await client.get_status(invoice)
        assert (status.state, status.received) == ("open", 0)
        # Nothing published, nothing asked for it.
        assert len(world.received()) == taken
        assert world.http.urls("https://api.blockcypher.com") == []
    finally:
        await world.close()


async def test_a_relay_that_hides_a_witness_while_another_holds_it_late() -> None:
    """The witnesses read again on every relay after this check published one (SPEC.md §31):
    another sale's witness of the transaction, which only a late relay sends, makes it count
    for nothing."""
    world = await World.start(lot=1)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-62", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        paying = pay(world, invoice)
        late = world.relay_urls[2]
        other = of_sale(world, "ORD-OTHER", address, paying, invoice.expires_at)
        store_witness(world, other, on=[late])
        world.relays[late].late = lambda filter_: "#o" in filter_
        world.relays[late].late_by = 0.5
        status = await client.get_status(invoice)
        assert (status.state, status.received) == ("open", 0)
    finally:
        await world.close()


async def test_one_transaction_counts_for_one_invoice_at_most() -> None:
    """Two orders hold one address when every relay hid one's lease from the other (SPEC.md
    §27): a payment of it counts for one of their invoices at most, whichever checks first, or
    for neither when they check at once."""
    world = await World.start(lot=1)
    try:
        [address] = world.lot
        registry_address = onchain_registry_address(world.merchant.registry_key, address)
        client = world.client()
        first = await client.create_invoice(ref="ORD-63", sats=49_000)
        assert first.onchain_address == address
        author = world.sale("ORD-63").author
        for url in world.relay_urls:
            world.relays[url].hides = lambda e: (
                e.pubkey == author and e.tag("d") == registry_address
            )
        second = await client.create_invoice(ref="ORD-64", sats=49_000)
        assert second.onchain_address == address
        for url in world.relay_urls:
            world.relays[url].hides = None
        world.explorer.add(address, tx(txid(), {address: 49_000}))
        statuses = await asyncio.gather(client.get_status(first), client.get_status(second))
        counted = [status for status in statuses if status.received]
        assert len(counted) <= 1
        # Checked again, each sees the other's witness.
        again = [await client.get_status(first), await client.get_status(second)]
        assert sum(1 for status in again if status.received) <= 1
    finally:
        await world.close()


async def test_a_transaction_paying_the_addresses_of_two_orders_pays_each_its_part() -> None:
    """SPEC.md §31: a transaction may pay the addresses of two orders (a batch of withdrawals);
    the witnesses at each address tell its own order, and each order counts what its address
    got."""
    world = await World.start(lot=2)
    try:
        client = world.client()
        first = await client.create_invoice(ref="ORD-70", sats=49_000)
        second = await client.create_invoice(ref="ORD-71", sats=30_000)
        assert first.onchain_address is not None and second.onchain_address is not None
        assert first.onchain_address != second.onchain_address
        paying = txid()
        batch = tx(paying, {first.onchain_address: 49_000, second.onchain_address: 30_000})
        world.explorer.add(first.onchain_address, batch)
        world.explorer.add(second.onchain_address, batch)
        one = await client.get_status(first)
        other = await client.get_status(second)
        assert (one.state, one.received, one.txids) == ("unconfirmed", 49_000, (paying,))
        assert (other.state, other.received, other.txids) == ("unconfirmed", 30_000, (paying,))
        # Read again: each order's witness at its own address changes nothing for the other.
        assert (await client.get_status(first)).received == 49_000
        assert (await client.get_status(second)).received == 30_000
        seen = seen_keys(world.merchant.key.secret)
        # Each check published its witness at its own address, for its own sale.
        witnessed = {e["tags"][1][1] for e in world.received() if e["pubkey"] == seen.author}
        assert witnessed == {
            order_tag(seen, first.onchain_address, world.sale("ORD-70").author),
            order_tag(seen, second.onchain_address, world.sale("ORD-71").author),
        }
    finally:
        await world.close()


async def test_a_witness_at_another_address_with_this_reads_tags_is_ignored() -> None:
    """SPEC.md §31: a witness whose `address` is not the invoice's is of no invoice of the read,
    whatever its tags: one of another sale at another address, carrying this address's tag,
    leaves the transaction this invoice's."""
    world = await World.start(lot=2)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-72", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        [elsewhere] = [a for a in world.lot if a != address]
        paying = pay(world, invoice)
        seen = seen_keys(world.merchant.key.secret)
        other_sale = world.sale("ORD-OTHER").author
        witness = Witness(
            address=elsewhere,
            txid=paying,
            sale=other_sale,
            sats=49_000,
            inputs=(f"{'f' * 64}:0",),
            replacement=False,
            seen_at=int(world.now) - 10,
            expires_at=invoice.expires_at,
        )
        event = seal(
            record_type=WITNESS_TYPE,
            value=witness_value(witness),
            signer=seen.secret,
            key=channel_encryption_key(world.merchant.key.secret),
            address=witness_address(seen, elsewhere, paying, other_sale),
            created_at=witness.seen_at,
            random=secrets.token_bytes,
            tags=[["o", order_tag(seen, elsewhere, other_sale)], ["o", address_tag(seen, address)]],
        )
        for url in world.relay_urls:
            world.relays[url].store(event)
        status = await client.get_status(invoice)
        assert (status.state, status.received) == ("unconfirmed", 49_000)
    finally:
        await world.close()
