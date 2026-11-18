"""A hostile relay: whatever it sends in answer to a query is ignored when it is not what the
query asks (SPEC.md §11: "A relay is never trusted: anything else is ignored"), never an error
outside SPEC.md §5."""

from __future__ import annotations

import itertools
import secrets
import tracemalloc
from typing import Any

import pytest

from fakes.chain import tx, txid
from fakes.relay import Copies
from fakes.world import World
from sattleme_checkout._channel import (
    ActiveChannel,
    Channel,
    lookup_channel,
    parse_channel,
)
from sattleme_checkout._constants import CHANNEL_TYPE
from sattleme_checkout._crypto import schnorr_sign
from sattleme_checkout._declaration import open_declaration
from sattleme_checkout._derive import (
    channel_address,
    channel_encryption_key,
    lot_address,
    onchain_registry_address,
    order_tag,
    seen_keys,
    witness_address,
)
from sattleme_checkout._nostr import event_hash
from sattleme_checkout._records import Newer, Opened, open_record
from sattleme_checkout._relay import RelayPool, fresh_key

LONE = chr(0xD800)

_ids = itertools.count(1)


def _id() -> str:
    """A distinct id for each forged value: a paged read keeps the first value of each id."""
    return f"{next(_ids):064x}"


def shapes(*, author: object, d: str, created_at: int) -> list[dict[str, Any]]:
    """Values of every wrong shape at `d`, claiming `author`."""
    base: dict[str, Any] = {
        "pubkey": author,
        "created_at": created_at,
        "kind": 30078,
        "tags": [["d", d]],
        "content": "#",
        "sig": "0" * 128,
    }
    found: list[dict[str, Any]] = [
        {**base, "tags": [["d", LONE]]},
        {**base, "tags": [["d", ["x"]], ["d", d]]},
        {**base, "tags": [["d", {"a": 1}], ["d", d]]},
        {**base, "tags": [["d"], ["d", d]]},
        {**base, "tags": "d"},
        {**base, "tags": [["d", 1]], "pubkey": LONE},
        {**base, "pubkey": LONE},
        {**base, "pubkey": ["x"]},
        {**base, "pubkey": None},
        {**base, "pubkey": author.upper() if isinstance(author, str) else author},
        {**base, "kind": "30078"},
        {**base, "kind": 1e400},
        {**base, "created_at": LONE},
        {**base, "created_at": True},
        {**base, "content": ["x"]},
        {**base, "content": LONE},
        {**base, "content": ""},
        {**base, "tags": [["d", d], ["o", LONE], ["expiration", ["x"]]]},
        {**base, "tags": [["d", d + LONE]]},
        {**base, "tags": [["d", d.upper()]]},
    ]
    return [{**value, "id": _id()} for value in found] + [{**base, "id": LONE}]


def forge(created_at: int) -> Any:
    """A relay that answers each query with values made from its filter: every author and `d`
    it names (a relay sees the registry addresses of a lease read, the sale's author)."""

    def answer(filter_: dict[str, Any]) -> list[dict[str, Any]]:
        authors = filter_.get("authors") or ["0" * 64]
        ds = filter_.get("#d") or ["0" * 64]
        return [
            value
            for author in authors
            for d in ds
            for value in shapes(author=author, d=d, created_at=created_at)
        ]

    return answer


@pytest.mark.parametrize("flavor", ["sale", "anyone"])
async def test_a_hostile_relay_slows_and_breaks_nothing(flavor: str) -> None:
    world = await World.start(lot=2)
    try:
        sale = world.sale("ORD-1")
        author = sale.author if flavor == "sale" else world.merchant.team
        for value in shapes(author=author, d="0" * 64, created_at=int(world.now)):
            world.relays["relay-a.example"].put(value)
            world.relays["relay-b.example"].put(value)
        await _an_order_goes_on(world)
    finally:
        await world.close()


async def test_a_relay_that_forges_from_the_filter_breaks_nothing() -> None:
    world = await World.start(lot=2)
    try:
        world.relays["relay-a.example"].forge = forge(int(world.now))
        world.relays["relay-b.example"].forge = forge(int(world.now))
        await _an_order_goes_on(world)
    finally:
        await world.close()


async def _an_order_goes_on(world: World) -> None:
    client = world.client()
    invoice = await client.create_invoice(ref="ORD-1", sats=49_000)
    assert invoice.onchain_address is not None
    assert [i.payment_hash for i in await client.find_invoices("ORD-1")] == [invoice.payment_hash]
    paying = txid()
    world.explorer.add(invoice.onchain_address, tx(paying, {invoice.onchain_address: 20_000}))
    status = await client.get_status(invoice)
    assert status.state == "open"
    assert status.received == 20_000
    assert (await client.merchant()).onchain_addresses == 2


async def test_a_relay_never_sends_a_record_by_its_id_alone() -> None:
    """SPEC.md §31, The rest: a rest invoice whose declaration fewer than a majority of the relays
    sent is published again. A relay that sends a value with the declaration's id, but not the
    declaration, has not sent it."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-2", sats=49_000)
        assert invoice.onchain_address is not None
        world.explorer.add(invoice.onchain_address, tx(txid(), {invoice.onchain_address: 20_000}))
        status = await client.get_status(invoice)
        assert status.rest is not None
        assert status.rest.bolt11 is not None
        sale = world.sale("ORD-2")
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
        # On relay a alone; relay b sends a value with its id, and nothing else of it.
        for relay in (b, c):
            for address, held in list(relay.events.items()):
                if getattr(held, "id", None) == rest.id:
                    del relay.events[address]
        b.put({**rest.to_json(), "content": "#", "sig": "0" * 128})
        b.received.clear()
        # The read's majority is relay a, then relay b: c is down, b answers late.
        c.down = True
        b.delay = 0.2
        again = await client.get_status(invoice)
        assert again.rest is not None
        assert again.rest.bolt11 == status.rest.bolt11
        # Published again: relay a alone sent it.
        assert [value["id"] for value in b.received] == [rest.id]
        assert any(event.id == rest.id for event in world.held(world.relay_urls[1], sale.author))
    finally:
        await world.close()


def unsigned_newer(
    author: str, d: str, created_at: int, tags: list[list[str]] | None = None
) -> dict[str, Any]:
    """A record of a newer format (its content `#`) at `d`, claiming `author`: its id its hash,
    its signature another key's. Anybody can write one."""
    all_tags = [["d", d], *(tags or [])]
    digest = event_hash(author, created_at, 30078, all_tags, "#")
    assert digest is not None
    sig = schnorr_sign(digest, fresh_key(secrets.token_bytes), bytes(32)).hex()
    return {
        "id": digest.hex(),
        "pubkey": author,
        "created_at": created_at,
        "kind": 30078,
        "tags": all_tags,
        "content": "#",
        "sig": sig,
    }


async def test_records_of_a_newer_format_without_the_authors_signature_are_ignored() -> None:
    """SPEC.md §12: a record is of a newer format only once its event checks out; anybody can
    send one with the author's `pubkey`, the `d` tag and a content such as `#`, never with the
    author's signature. On every relay, at the channel's, the lot's, a lease's and a witness's
    places, newer than what is there: ignored, nothing warns, nothing changes."""
    world = await World.start(lot=1)
    try:
        key = world.merchant.key
        [address] = world.lot
        sale = world.sale("ORD-1")
        later = int(world.now) + 10
        forged = [
            unsigned_newer(key.team, channel_address(key.secret), later),
            unsigned_newer(key.team, lot_address(key.secret), later),
            unsigned_newer(
                sale.author, onchain_registry_address(world.merchant.registry_key, address), later
            ),
        ]
        paying = txid()
        seen = seen_keys(key.secret)
        forged.append(
            unsigned_newer(
                seen.author,
                witness_address(seen, address, paying, sale.author),
                later,
                [["o", order_tag(seen, address, sale.author)]],
            )
        )
        for url in world.relay_urls:
            for value in forged:
                world.relays[url].put(value)
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-1", sats=49_000)
        assert invoice.onchain_address == address
        world.explorer.add(address, tx(paying, {address: 49_000}))
        status = await client.get_status(invoice)
        assert (status.state, status.received) == ("unconfirmed", 49_000)
        assert (await client.merchant()).onchain_addresses == 1
        assert not any("newer format" in warning for warning in world.warnings)
    finally:
        await world.close()


async def test_copies_of_a_record_under_other_ids_are_never_kept() -> None:
    """SPEC.md §13, Limits: a copy of a record under an id of its own (a large tag added, the id
    recomputed, its signature left as it is) does not check out: a read keeps none of them, some
    100 MB of them a few MiB in memory, and finds the record."""
    world = await World.start(lot=1)
    try:
        key = world.merchant.key
        address = channel_address(key.secret)
        [channel] = [
            event
            for event in world.held(world.relay_urls[0], key.team)
            if event.tag("d") == address
        ]
        socket = Copies(channel, count=999, size=100_000, genuine=True)

        async def connect(_url: str) -> Copies:
            return socket

        encryption = channel_encryption_key(key.secret)
        tracemalloc.start()
        try:
            async with RelayPool(
                connect=connect, timeout_ms=30_000, random=secrets.token_bytes, now=lambda: 0
            ) as pool:
                answers: dict[str, list[Opened[Channel] | Newer]] = await pool.read(
                    ["wss://a.example"],
                    {"kinds": [30078], "authors": [key.team], "#d": [address]},
                    lambda value: open_record(
                        value,
                        record_type=CHANNEL_TYPE,
                        key=encryption,
                        address=address,
                        author=key.team,
                        parse=parse_channel,
                    ),
                )
                found = await lookup_channel(pool, key, ["wss://a.example"], world.logger)
            _held, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # The genuine record alone: no copy checks out.
        assert [opened.event.id for opened in answers["wss://a.example"]] == [channel.id]
        assert isinstance(found, ActiveChannel)
        assert socket.sent == 2 * 999
        assert peak < 16 << 20
        assert not any("newer format" in warning for warning in world.warnings)
    finally:
        await world.close()
