"""Leasing a deposit address of the lot (SPEC.md §31, The lot, Leasing an address)."""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

from . import _json
from ._constants import ADDRESSES_TRIED, LEASE_LOOKBACK, LOT_TYPE, RECORD_KIND
from ._declaration import Registry
from ._derive import (
    SaleKeys,
    address_tag,
    channel_encryption_key,
    lot_address,
    onchain_registry_address,
    seen_keys,
)
from ._explorer import CallExplorer, Explorer, Transaction
from ._key import IntegrationKey
from ._lease import (
    LeaseRecord,
    LeaseTerms,
    another_of_sale,
    is_confirmed,
    lease_event,
    leased,
    open_lease_record,
    withdrawal_event,
)
from ._log import Logger
from ._lot import parse_lot
from ._nostr import Event
from ._records import Newer, Newest, Opened, RandomBytes, newest_copy, open_record
from ._relay import RelayPool
from ._tasks import both
from ._witness import addresses_named
from .errors import InvalidInvoiceError, NetworkError


def d_tags(value: object) -> Iterator[str]:
    """The values of the `d` tags of what a relay sent, in their order."""
    event = _json.as_object(value)
    tags = event.get("tags") if event is not None else None
    if not isinstance(tags, list):
        return
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "d" and isinstance(tag[1], str):
            yield tag[1]


async def read_lot(
    pool: RelayPool, key: IntegrationKey, relays: Sequence[str], logger: Logger
) -> tuple[str, ...]:
    """The lot's addresses: empty without a valid copy (NetworkError: fewer than a majority)."""
    address = lot_address(key.secret)
    encryption = channel_encryption_key(key.secret)
    answers: dict[str, list[Opened[tuple[str, ...]] | Newer]] = await pool.read(
        relays,
        {"kinds": [RECORD_KIND], "authors": [key.team], "#d": [address]},
        lambda value: open_record(
            value,
            record_type=LOT_TYPE,
            key=encryption,
            address=address,
            author=key.team,
            parse=parse_lot,
        ),
    )
    newest: Newest[tuple[str, ...]] = newest_copy(
        copy for copies in answers.values() for copy in copies
    )
    found = newest.newest
    if newest.newest_newer is not None and (
        found is None or newest.newest_newer > found.event.created_at
    ):
        logger.warning(
            "The integration's lot of addresses has a newer format than this version of "
            "sattleme-checkout reads: update it"
        )
        return ()
    return () if found is None else found.value


@dataclass(frozen=True, slots=True)
class LeaseRead:
    """The records at the registry addresses of deposit addresses, by address, from a complete
    read (SPEC.md §13): whether a majority answered (else it is no answer: `network`), and
    whether a majority read completely."""

    records: dict[str, list[LeaseRecord]]
    answered: bool
    complete: bool

    def confirms(self, address: str, lease: Event, t: int) -> bool:
        """Whether this read, of a majority, complete, confirms `lease` of `address` at `t`
        (SPEC.md §31, Leasing an address, step 3.3: `is_confirmed`)."""
        return self.answered and self.complete and is_confirmed(self.records[address], lease, t)


async def read_leases(
    pool: RelayPool,
    registry: Registry,
    addresses: Sequence[str],
    relays: Sequence[str],
    now: int,
    *,
    every_relay: bool = False,
) -> LeaseRead:
    """The records at the registry addresses of `addresses` from the last 7200 s, by any author
    (leases are signed by sales the integration does not know), a complete read with a key of
    this read alone. One record per event id, each relay's copy of it one more signature to try
    (LeaseRecord)."""
    by_registry = {onchain_registry_address(registry.key, a): a for a in addresses}

    def keep(value: object) -> tuple[str, LeaseRecord] | None:
        registry_address = next((d for d in d_tags(value) if d in by_registry), None)
        if registry_address is None:
            return None
        record = open_lease_record(value, registry=registry, registry_address=registry_address)
        return None if record is None else (by_registry[registry_address], record)

    read = await pool.read_complete(
        relays,
        {"kinds": [RECORD_KIND], "#d": list(by_registry), "since": now - LEASE_LOOKBACK},
        keep,
        every_relay=every_relay,
    )
    found: dict[str, list[LeaseRecord]] = {address: [] for address in addresses}
    known: dict[str, LeaseRecord] = {}
    for copies in read.answers.values():
        for address, copy in copies:
            record = known.get(copy.id)
            if record is None:
                known[copy.id] = copy
                found[address].append(copy)
            else:
                record.merge(copy)
    return LeaseRead(found, read.answered, read.complete)


async def read_named(
    pool: RelayPool, key: IntegrationKey, addresses: Sequence[str], relays: Sequence[str]
) -> set[str]:
    """The addresses of `addresses` a witness names, read by their address tags, not paged,
    with a key of this read alone; NetworkError when fewer than a majority answered."""
    seen = seen_keys(key.secret)
    by_tag = {address_tag(seen, address): address for address in addresses}
    answers = await pool.read(
        relays,
        {"kinds": [RECORD_KIND], "authors": [seen.author], "#o": list(by_tag)},
        lambda value: addresses_named(value, seen, by_tag),
    )
    return {address for named in answers.values() for found in named for address in found}


def shuffled(items: Sequence[str], random: RandomBytes) -> list[str]:
    """`items` in a random order (Fisher-Yates, unbiased draws from the CSPRNG)."""
    result = list(items)
    for index in range(len(result) - 1, 0, -1):
        bound = index + 1
        limit = (1 << 32) - (1 << 32) % bound
        while True:
            draw = int.from_bytes(random(4), "big")
            if draw < limit:
                break
        other = draw % bound
        result[index], result[other] = result[other], result[index]
    return result


@dataclass(frozen=True, slots=True)
class Leased:
    """An address leased for the order: its lease confirmed, then marked."""

    address: str


PublishWitnessNone = Callable[[str, Transaction], Awaitable[None]]


def _transactions_of(address: str) -> Callable[[Explorer], Awaitable[list[Transaction]]]:
    """The read of an address's transactions, of whichever explorer the read asks."""

    async def read(explorer: Explorer) -> list[Transaction]:
        return await explorer.address_transactions(address)

    return read


async def lease_address(
    pool: RelayPool,
    *,
    key: IntegrationKey,
    registry: Registry,
    relays: Sequence[str],
    sale: SaleKeys,
    explorer: CallExplorer,
    terms: Callable[[str], LeaseTerms],
    now: Callable[[], int],
    random: RandomBytes,
    logger: Logger,
    publish_witness_none: PublishWitnessNone,
    created_at: int | None,
) -> Leased | Literal["none", "explorer_failed"]:
    """Lease an address of the lot (step 7 of SPEC.md §17, Leasing an address).

    "none": no address could be leased; "explorer_failed": the explorer failed, with its fallback
    (the merchant's Bitcoin node with "none" and down: no address; `network` for an order paid
    on-chain only). `created_at`: the lease's date when it is the Invoice's (an order
    paid on-chain only), else now. NetworkError when the relays fail: a lease that was not
    confirmed is withdrawn first; one that was is never withdrawn (its mark failing included).
    """
    lot = await read_lot(pool, key, relays, logger)
    if not lot:
        return "none"
    # The leases, complete or not (the confirmation decides), and the addresses a witness names.
    leases, named = await both(
        read_leases(pool, registry, lot, relays, now()), read_named(pool, key, lot, relays)
    )
    if not leases.answered:
        raise NetworkError("Fewer than a majority of the merchant's relays answered")
    if any(record.newer for records in leases.records.values() for record in records):
        # It holds its address: never written over, never used (SPEC.md §12).
        logger.warning(
            "A lease of the integration's addresses has a newer format than this version of "
            "sattleme-checkout reads: update it"
        )
    moment = now()
    # Not where a record of the sale is (its signature checked): an earlier call of the order
    # tried it, and a new lease could be of the second of its withdrawal, and either replace the
    # other.
    free = [
        address
        for address in lot
        if address not in named
        and not leased(leases.records[address], moment)
        and not any(
            record.pubkey == sale.author and record.signed() for record in leases.records[address]
        )
    ]
    for address in shuffled(free, random)[:ADDRESSES_TRIED]:
        try:
            # Of the explorer the call reads, with its fallback (SPEC.md §31): an address the node
            # shows paid is skipped too.
            paying = await explorer.read(_transactions_of(address))
        except NetworkError:
            return "explorer_failed"
        if paying:
            # Paid on-chain already: the merchant's app takes it out of the lot.
            for tx in paying:
                await publish_witness_none(address, tx)
            continue
        unconfirmed = dataclasses.replace(terms(address), confirmed=False)
        lease = lease_event(
            registry=registry,
            sale=sale,
            terms=unconfirmed,
            created_at=now() if created_at is None else created_at,
            random=random,
        )
        if lease is None:  # pragma: no cover - its size was checked before anything was read
            raise InvalidInvoiceError("The invoice is too large for the lease of an address")

        async def withdraw_it(withdrawn: Event = lease, at: str = address) -> None:
            await withdraw(
                pool,
                registry=registry,
                sale=sale,
                address=at,
                lease=withdrawn,
                relays=relays,
                random=random,
                logger=logger,
            )

        try:
            await pool.publish(relays, lease, key=sale.secret)
        except NetworkError as error:
            # Some relays may hold it: withdrawn there, never the order's.
            await withdraw_it()
            raise NetworkError(
                "The lease of a deposit address is not on a majority of the merchant's relays: "
                + error.message
            ) from error
        # On every relay: a relay that hides a lease cannot hide it while one that holds it
        # answers in time (SPEC.md §13, §27).
        again = await read_leases(pool, registry, [address], relays, now(), every_relay=True)
        records = again.records[address]
        if not again.confirms(address, lease, now()):
            # Another server leased it at once (the later reader sees both), the read was not
            # complete, or fewer than a majority answered: withdrawn (safe on some relays only:
            # the address never was the order's), but where another call of the order wrote at
            # once: its record is not this call's to end.
            if not another_of_sale(records, lease):
                await withdraw_it()
            if not again.answered:
                raise NetworkError("Fewer than a majority of the merchant's relays answered")
            continue
        # Confirmed: never withdrawn from now on, whatever fails (a withdrawal some relays only
        # took would leave them disagreeing on an address that may be the order's). Marked: from
        # now on the address is the order's.
        mark = lease_event(
            registry=registry,
            sale=sale,
            terms=dataclasses.replace(unconfirmed, confirmed=True),
            created_at=max(now(), lease.created_at + 1),
            random=random,
        )
        if mark is None:  # pragma: no cover - as large as the lease, which fitted
            raise InvalidInvoiceError("The invoice is too large for the lease of an address")
        try:
            await pool.publish(relays, mark, key=sale.secret)
        except NetworkError as error:
            raise NetworkError(
                "The lease of a deposit address is not marked confirmed on a majority of the "
                "merchant's relays: " + error.message
            ) from error
        return Leased(address=address)
    return "none"


async def withdraw(
    pool: RelayPool,
    *,
    registry: Registry,
    sale: SaleKeys,
    address: str,
    lease: Event,
    relays: Sequence[str],
    random: RandomBytes,
    logger: Logger,
) -> None:
    """Withdraw the sale's lease of `address` that was not confirmed: its withdrawal a second
    after it, so that it replaces the lease and no later record of the sale. A failure is only
    logged: the lease then holds the address until its end, and never gives its invoice one."""
    event = withdrawal_event(
        registry=registry,
        sale=sale,
        address=address,
        created_at=lease.created_at + 1,
        random=random,
    )
    try:
        await pool.publish(relays, event, key=sale.secret)
    except NetworkError as error:
        logger.warning("A lease that was not confirmed was not withdrawn: %s", error.message)
