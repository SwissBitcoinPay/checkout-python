"""A sale's records in the merchant's registry (SPEC.md §21, The sale's declarations)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from . import _json
from ._address import payment_uri
from ._constants import GRACE_SECONDS, RECORD_KIND, SATS_CURRENCY
from ._declaration import Declaration, DeclaredPrice, Registry, open_declaration
from ._derive import SaleKeys
from ._lease import LeaseTerms, SaleRecord, open_sale_record
from ._log import Logger
from ._nostr import Event, hashed_event
from ._onchain import RestInvoice
from ._prices import minor_text
from ._records import Newer, Opened, first_d_tag
from ._relay import RelayPool, majority
from ._text import ascii_lower
from .errors import SattlemeError
from .models import DeclaredInvoice, Fiat, PaymentStatus


def fiat_of(price: DeclaredPrice) -> Fiat | None:
    """A declared price as a DeclaredInvoice's `fiat`: None for an order in sats."""
    if price.currency == SATS_CURRENCY:
        return None
    return Fiat(
        amount=minor_text(price.minor, price.digits),
        currency=price.currency,
        minor=price.minor,
        digits=price.digits,
    )


@dataclass(frozen=True, slots=True)
class EarlierInvoice:
    """A DeclaredInvoice, with the events that hold it (published again when it is handed out):
    its declaration (none for an order paid on-chain only) and its lease's (none without an
    address)."""

    invoice: DeclaredInvoice
    declaration: Event | None
    lease: Event | None

    @property
    def last_record(self) -> Event | None:
        """Its last record: its declaration; for an order paid on-chain only, its lease's event
        (its mark). The one shown has it on a majority of the relays."""
        return self.declaration or self.lease


@dataclass(frozen=True, slots=True)
class Lease:
    """The sale's lease of an address: its event and its terms."""

    event: Event
    terms: LeaseTerms


@dataclass
class SaleRecords:
    """What the merchant's relays hold of a sale."""

    invoices: list[EarlierInvoice] = field(default_factory=list)
    rests: dict[tuple[str, int], list[RestInvoice]] = field(default_factory=dict)
    rest_events: dict[str, Event] = field(default_factory=dict)
    unsure: set[str] = field(default_factory=set)
    leases: dict[str, Lease] = field(default_factory=dict)
    # The ids of the events each relay sent whose id is their hash, by relay (`holders`).
    copies: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)

    def rest_invoices(self, address: str, expires_at: int) -> list[RestInvoice]:
        return self.rests.get((address, expires_at), [])

    def lease_of(self, address: str) -> Lease | None:
        return self.leases.get(address)

    def holders(self, events: Iterable[Event]) -> dict[str, int]:
        """How many relays sent each of `events`, by id: a copy whose id is its hash
        (SPEC.md §11), whatever its signature."""
        found = {event.id: 0 for event in events}
        for sent in self.copies.values():
            for event_id in found:
                if event_id in sent:
                    found[event_id] += 1
        return found


async def read_sale(
    pool: RelayPool,
    relays: Sequence[str],
    sale: SaleKeys,
    registry: Registry,
    logger: Logger,
    *,
    every_relay: bool = False,
) -> SaleRecords:
    """The sale's declarations and leases, paged (with `every_relay`, on every relay);
    NetworkError when fewer than a majority answer."""
    answers: dict[str, list[_Kept]] = await pool.read(
        relays,
        {"kinds": [RECORD_KIND], "authors": [sale.author]},
        lambda value: _open(value, sale, registry),
        key=sale.secret,
        paged=True,
        every_relay=every_relay,
    )
    # A relay sent a rest declaration only when what it sent opens as it: never by its id alone.
    senders: dict[str, set[str]] = {}
    declarations: dict[str, Declaration] = {}
    records: list[Opened[SaleRecord] | Newer] = []
    kept: set[str] = set()
    newer = False
    copies: dict[str, frozenset[str]] = {}
    for relay, values in answers.items():
        copies[relay] = frozenset(
            value if isinstance(value, str) else value.event.id for value in values
        )
        for result in values:
            if isinstance(result, str):
                continue
            senders.setdefault(result.event.id, set()).add(relay)
            if result.event.id in kept:
                continue
            kept.add(result.event.id)
            if isinstance(result, Newer):
                newer = True
                # The newest at its registry address, it leaves no lease there.
                records.append(result)
            elif isinstance(result.value, Declaration):
                d_tag = result.event.tag("d") or ""
                known = declarations.get(d_tag)
                if known is None or result.event.replaces(known.event):
                    declarations[d_tag] = result.value
            elif isinstance(result.value, SaleRecord):
                records.append(Opened(result.event, result.value))
    if newer:
        logger.warning(
            "A record of the sale has a newer format than this version of "
            "sattleme-checkout reads: update it"
        )
    found = SaleRecords(copies=copies)
    found.invoices, found.leases = earlier_invoices(sale.ref, declarations.values(), records)
    wanted = majority(len(relays))
    for declaration in declarations.values():
        if declaration.rest is None:
            continue
        decoded = declaration.decoded
        rest = RestInvoice(
            bolt11=declaration.invoice,
            payment_hash=decoded.payment_hash,
            verify_url=declaration.verify_url,
            sats=(decoded.amount_msat or 0) // 1000,
            created_at=decoded.timestamp,
            expires_at=decoded.expires_at,
        )
        key = (declaration.rest.address, declaration.rest.expires_at)
        found.rests.setdefault(key, []).append(rest)
        found.rest_events[decoded.payment_hash] = declaration.event
        if len(senders.get(declaration.event.id, ())) < wanted:
            found.unsure.add(decoded.payment_hash)
    for rests in found.rests.values():
        rests.sort(key=lambda rest: (rest.created_at, rest.payment_hash))
    return found


def newest(records: Iterable[Opened[SaleRecord] | Newer]) -> dict[str, Opened[SaleRecord] | Newer]:
    """Of the sale's records at registry addresses, the newest at each `d` tag (the highest
    `created_at`, then the lowest id, as relays keep them)."""
    found: dict[str, Opened[SaleRecord] | Newer] = {}
    for record in records:
        d_tag = record.event.tag("d") or ""
        current = found.get(d_tag)
        if current is None or record.event.replaces(current.event):
            found[d_tag] = record
    return found


def earlier_invoices(
    ref: str,
    declarations: Iterable[Declaration],
    records: Iterable[Opened[SaleRecord] | Newer],
) -> tuple[list[EarlierInvoice], dict[str, Lease]]:
    """An order's invoices from the sale's records (SPEC.md §21), oldest first (by date, then
    payment hash, none first, then address), and the sale's lease of each address (the newest
    record at its registry address: a withdrawal, or a record of a newer format, leaves none).

    One invoice per declaration (one with `rest` gives none), with the address of the newest
    confirmed lease naming its invoice; and one per confirmed lease of an order paid on-chain
    only. A lease that is not confirmed gives nothing: its call failed before its address was
    the order's. A lease naming an invoice that has no declaration is left out: that invoice was
    never shown."""
    leases: dict[str, Lease] = {}
    for record in newest(records).values():
        if isinstance(record, Opened) and record.value.terms is not None:
            leases[record.value.terms.address] = Lease(record.event, record.value.terms)
    confirmed = [lease for lease in leases.values() if lease.terms.confirmed]
    by_invoice: dict[str, Lease] = {}
    for lease in confirmed:
        if lease.terms.invoice is None:
            continue
        name = ascii_lower(lease.terms.invoice)
        known = by_invoice.get(name)
        if known is None or lease.event.replaces(known.event):
            by_invoice[name] = lease
    invoices: list[EarlierInvoice] = []
    for declaration in declarations:
        if declaration.rest is not None:
            # A rest invoice's: no invoice of the order.
            continue
        decoded = declaration.decoded
        sats = (decoded.amount_msat or 0) // 1000
        named = by_invoice.get(ascii_lower(declaration.invoice))
        address = None if named is None else named.terms.address
        invoices.append(
            EarlierInvoice(
                invoice=DeclaredInvoice(
                    ref=ref,
                    bolt11=declaration.invoice,
                    payment_hash=decoded.payment_hash,
                    verify_url=declaration.verify_url,
                    onchain_address=address,
                    payment_uri=payment_uri(address, sats, declaration.invoice),
                    sats=sats,
                    fiat=fiat_of(declaration.price),
                    note=declaration.note,
                    created_at=decoded.timestamp,
                    expires_at=decoded.expires_at,
                    declared_at=declaration.event.created_at,
                ),
                declaration=declaration.event,
                lease=None if named is None else named.event,
            )
        )
    for lease in confirmed:
        terms = lease.terms
        if terms.invoice is not None:
            continue
        invoices.append(
            EarlierInvoice(
                invoice=DeclaredInvoice(
                    ref=ref,
                    bolt11=None,
                    payment_hash=None,
                    verify_url=None,
                    onchain_address=terms.address,
                    payment_uri=payment_uri(terms.address, terms.sats, None),
                    sats=terms.sats,
                    fiat=fiat_of(terms.price),
                    note=terms.note,
                    created_at=terms.created_at,
                    expires_at=terms.expires_at,
                    # Its mark's date.
                    declared_at=lease.event.created_at,
                ),
                declaration=None,
                lease=lease.event,
            )
        )
    invoices.sort(
        key=lambda earlier: (
            earlier.invoice.created_at,
            earlier.invoice.payment_hash or "",
            earlier.invoice.onchain_address or "",
        )
    )
    return invoices, leases


def may_still_be_paid(invoices: Sequence[EarlierInvoice], now: int) -> list[EarlierInvoice]:
    """The declared invoices that may still be paid at `now`: until their end and its grace
    period (SPEC.md §21, step 1)."""
    return [e for e in invoices if now < e.invoice.expires_at + GRACE_SECONDS]


def held_on_majority(
    again: SaleRecords, pending: Sequence[EarlierInvoice], relays: int
) -> list[bool]:
    """Of several invoices that may still be paid, whether a majority of the `relays` (their
    count) each sent its last record when the sale's records were read again on every relay
    (SPEC.md §21, step 1): the one shown has it so."""
    lasts = [earlier.last_record for earlier in pending]
    sent = again.holders(last for last in lasts if last is not None)
    wanted = majority(relays)
    return [last is not None and sent[last.id] >= wanted for last in lasts]


Outcome = tuple[Literal["paid", "failed", "hand_out"], int]
"""Of the invoices that may still be paid: the one `order_paid` carries, the one whose error is
the check's, or the one handed out (an index)."""


def pending_outcome(
    statuses: Sequence[PaymentStatus | SattlemeError],
    held: Sequence[bool] | SattlemeError,
) -> Outcome | SattlemeError:
    """SPEC.md §21, step 1, of the invoices that may still be paid (oldest first), from each
    one's payment check and, of several, the sale's records read again: whether a majority of
    the list sent each one's last record (`held`), or that read's error (fewer than a majority
    answered). One alone is never read again (`held` empty).

    The first paid gives order_paid, whatever the read (a payment proves itself); else the first
    error is the error (that invoice may have been paid); else the read's error. Else one is
    handed out: the only one; of several, the only one held (the one shown), else the newest that
    received something (unconfirmed, or open with received above 0), else the newest."""
    for index, status in enumerate(statuses):
        if isinstance(status, PaymentStatus) and status.state == "paid":
            return "paid", index
    for index, status in enumerate(statuses):
        if isinstance(status, SattlemeError):
            return "failed", index
    if len(statuses) == 1:
        return "hand_out", 0
    if isinstance(held, SattlemeError):
        return held
    shown = [index for index, is_held in enumerate(held) if is_held]
    if len(shown) == 1:
        return "hand_out", shown[0]
    received = [
        index
        for index, status in enumerate(statuses)
        if isinstance(status, PaymentStatus)
        and (
            status.state == "unconfirmed" or (status.state == "open" and (status.received or 0) > 0)
        )
    ]
    return "hand_out", received[-1] if received else len(statuses) - 1


_Kept = Opened[Declaration] | Opened[SaleRecord] | Newer | str


def _open(value: object, sale: SaleKeys, registry: Registry) -> _Kept | None:
    """What a read of the sale keeps of what a relay sent: a record of the sale (a declaration,
    else a lease or its withdrawal); else, of an event whose id is its hash, that id, which tells
    a relay that holds a record (step 1 of SPEC.md §21: whatever its signature)."""
    declaration = open_declaration(value, sale=sale, registry=registry)
    if declaration is not None:
        return declaration
    d_tag = first_d_tag(value)
    if d_tag is not None:
        record = open_sale_record(value, sale=sale, registry=registry, registry_address=d_tag)
        if record is not None:
            return record
    event = _json.as_object(value)
    if event is None or event.get("pubkey") != sale.author:
        # No copy of a record of the sale: its id is the hash of its author too.
        return None
    copy = hashed_event(value)
    return None if copy is None else copy.id
