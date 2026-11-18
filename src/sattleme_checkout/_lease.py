"""Leases of deposit addresses (SPEC.md §31, Leases).

Anyone holding the registry key opens a lease's outside, whatever its author; but anybody can
write an event at a lease's `d` tag. So an event there counts only once its id is its hash, a
content of a newer NIP-44 version there is no record at all (only a value of a newer version,
which the outer key seals, is of a newer format), and a record's signature is checked only where
it decides whether the address is leased: that of a record that would end an older one's hold.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import _bolt11, _json, _nip44
from ._address import is_deposit_address
from ._channel import tolerance_of, version_of
from ._constants import (
    CLOCK_TOLERANCE,
    GRACE_SECONDS,
    LEASE_TYPE,
    LEASE_VERSION,
    RECORD_KIND,
    SATS_MAX,
)
from ._declaration import DeclaredPrice, Registry, parse_declared_price
from ._derive import SaleKeys, inner_key, onchain_registry_address, outer_key
from ._nostr import Event, hashed_event, signature_checks
from ._records import Newer, NewerFormat, Opened, RandomBytes, open_record, plaintext_fits, seal
from ._text import is_hex32, is_well_formed, utf8_length
from ._urls import is_https_url

_ABSENT = object()

SIZED_ADDRESS = "bc1p" + "q" * 58
"""A text of a deposit address's length (`bc1p` and 58 more, as every one): the size of a lease
before its address is chosen."""


@dataclass(frozen=True, slots=True)
class LeaseTerms:
    """What a lease holds (inside), which only the sale and the merchant read."""

    address: str
    sats: int
    invoice: str | None
    verify_url: str | None
    created_at: int
    expires_at: int
    price: DeclaredPrice
    note: str
    # Never shown (SPEC.md §27).
    tolerance: int = field(repr=False)
    # False as first published, True once marked (SPEC.md §31, Leases).
    confirmed: bool = False

    @property
    def end(self) -> int:
        return self.expires_at + GRACE_SECONDS


def lease_inside(terms: LeaseTerms, sale: SaleKeys) -> dict[str, Any]:
    value: dict[str, Any] = {
        "version": LEASE_VERSION,
        "address": terms.address,
        "sats": terms.sats,
        "invoice": terms.invoice,
        "verifyUrl": terms.verify_url,
        "createdAt": terms.created_at,
        "expiresAt": terms.expires_at,
        "sale": {"key": sale.key.reveal().hex(), "fiat": terms.price.to_json(), "note": terms.note},
        "taker": sale.taker,
    }
    if terms.tolerance > 0:
        value["tolerance"] = terms.tolerance
    value["confirmed"] = terms.confirmed
    return value


def lease_fits(terms: LeaseTerms, sale: SaleKeys) -> bool:
    """Whether the lease of `terms` fits NIP-44, sealed twice as `lease_event` seals it (its terms
    inside, then its outside): told by its size alone, nothing encrypted, no random byte drawn,
    so that checking it changes nothing of what the client then publishes."""
    length = utf8_length(_json.dumps(lease_inside(terms, sale)))
    if length is None or not 1 <= length <= _nip44.PLAINTEXT_MAX:
        return False
    sealed = "A" * _nip44.payload_length(length)
    return plaintext_fits(
        LEASE_TYPE, {"version": LEASE_VERSION, "end": terms.end, "sealed": sealed}
    )


def lease_event(
    *,
    registry: Registry,
    sale: SaleKeys,
    terms: LeaseTerms,
    created_at: int,
    random: RandomBytes,
) -> Event | None:
    """The lease: its end outside, its terms sealed to the registry reader inside.

    None when it is too large for NIP-44 (a valid invoice may be), before any random byte is
    drawn: never an exception, whose frames would hold the sale key and the tolerance
    (SPEC.md §27)."""
    if not lease_fits(terms, sale):
        return None
    inside = _json.dumps(lease_inside(terms, sale))
    outside = {
        "version": LEASE_VERSION,
        "end": terms.end,
        "sealed": _nip44.encrypt(inside, inner_key(sale.secret, registry.reader), random(32)),
    }
    address = onchain_registry_address(registry.key, terms.address)
    return seal(
        record_type=LEASE_TYPE,
        value=outside,
        signer=sale.secret,
        key=outer_key(registry.key, address, sale.author),
        address=address,
        created_at=created_at,
        random=random,
    )


def withdrawal_event(
    *, registry: Registry, sale: SaleKeys, address: str, created_at: int, random: RandomBytes
) -> Event:
    """The withdrawal of the sale's lease of `address` (`created_at` strictly after it)."""
    registry_address = onchain_registry_address(registry.key, address)
    return seal(
        record_type=LEASE_TYPE,
        value={"version": LEASE_VERSION, "withdrawn": True},
        signer=sale.secret,
        key=outer_key(registry.key, registry_address, sale.author),
        address=registry_address,
        created_at=created_at,
        random=random,
    )


@dataclass(frozen=True, slots=True)
class Outside:
    """A lease as anyone holding the registry key reads it: its end, or a withdrawal."""

    end: int | None
    sealed: str | None

    @property
    def withdrawn(self) -> bool:
        return self.end is None


def parse_outside(value: object) -> Outside | None:
    outside = _json.as_object(value)
    if outside is None:
        return None
    if version_of(outside.get("version"), LEASE_VERSION) is None:
        return None
    if outside.get("withdrawn") is True:
        return Outside(end=None, sealed=None)
    end = _json.as_safe_integer(outside.get("end"))
    sealed = outside.get("sealed")
    if end is None or not isinstance(sealed, str):
        return None
    return Outside(end=end, sealed=sealed)


class LeaseRecord:
    """A record at a deposit address's registry address, as anyone holding the registry key
    opens it, whatever its author: of its event (whose id is its hash) its id, author and date;
    of its outside its end (None: a withdrawal), or that it is of a newer format. Nothing else of
    it is kept: anybody can write a copy of a lease under another id (other tags, another date;
    its signature is not checked to open it), so that a read keeps a few bytes of each, never its
    tags nor its content (SPEC.md §13, Limits). Its signature is checked when `signed` is first
    asked, against every copy of the event the relays sent: one copy with a damaged signature
    never hides another that checks out (SPEC.md §11: a copy that does not check out is
    ignored)."""

    __slots__ = ("_signatures", "_signed", "created_at", "end", "id", "newer", "pubkey")

    def __init__(self, event: Event, outside: Outside | None) -> None:
        self.id = event.id
        self.pubkey = event.pubkey
        self.created_at = event.created_at
        self.newer = outside is None
        self.end = None if outside is None else outside.end
        self._signatures = [event.sig]
        self._signed: bool | None = None

    def merge(self, copy: LeaseRecord) -> None:
        """Another relay's copy of the event (the same id): its signatures to try too."""
        for sig in copy._signatures:
            if sig not in self._signatures:
                self._signatures.append(sig)
                if self._signed is False:
                    self._signed = None

    def signed(self) -> bool:
        """Whether a copy's signature checks out (checked once)."""
        if self._signed is None:
            self._signed = any(
                signature_checks(self.id, self.pubkey, sig) for sig in self._signatures
            )
        return self._signed

    def replaces(self, other: Event | LeaseRecord) -> bool:
        """Whether relays keep this one rather than `other`: later, then the lowest id."""
        return self.created_at > other.created_at or (
            self.created_at == other.created_at and self.id < other.id
        )

    def holds(self, t: int) -> bool:
        """Whether it holds its address at `t`: a lease whose end is at most 300 s before `t`
        (another server's clock may be behind), or a record of a newer format (an unreadable
        lease is never written over). A withdrawal, or a lease that ended, holds nothing."""
        if self.newer:
            return True
        return self.end is not None and self.end >= t - CLOCK_TOLERANCE


def open_lease(
    value: object, *, registry: Registry, registry_address: str
) -> tuple[Event, Outside | None] | None:
    """What a relay sent, opened as a record at the registry address `registry_address` of a
    deposit address, by the event's own author (SPEC.md §31, Opening a lease): an event whose id
    is its hash (one that is not is ignored, and hides no other event of that id), and its outside
    (None: of a newer format); a content of a newer NIP-44 version is none here; its signature is
    left to the walk. None when it is not one."""
    hashed = hashed_event(value)
    if hashed is None or hashed.kind != RECORD_KIND:
        return None
    if not any(
        len(tag) >= 2 and tag[0] == "d" and tag[1] == registry_address for tag in hashed.tags
    ):
        return None
    try:
        text = _nip44.decrypt(
            hashed.content, outer_key(registry.key, registry_address, hashed.pubkey)
        )
    except _nip44.Nip44Error:
        # A content of a newer NIP-44 version included: anybody can write one at this `d` tag.
        return None
    envelope = _json.as_object(_json.parse_text(text))
    if envelope is None or "v" not in envelope or envelope.get("t") != LEASE_TYPE:
        return None
    try:
        outside = parse_outside(envelope["v"])
    except NewerFormat:
        # Only a holder of the registry key seals a value: one of a newer version holds.
        return hashed, None
    return None if outside is None else (hashed, outside)


def open_lease_record(
    value: object, *, registry: Registry, registry_address: str
) -> LeaseRecord | None:
    """What a relay sent, as a lease read keeps it (`open_lease`, `LeaseRecord`)."""
    opened = open_lease(value, registry=registry, registry_address=registry_address)
    return None if opened is None else LeaseRecord(*opened)


def parse_inside(
    outside: Outside, *, sale: SaleKeys, registry: Registry, registry_address: str
) -> LeaseTerms | None:
    """A lease opened as the sale (inside): its terms; None when invalid; NewerFormat."""
    if outside.sealed is None or outside.end is None:
        return None
    try:
        inside = _json.as_object(
            _json.parse_text(
                _nip44.decrypt(outside.sealed, inner_key(sale.secret, registry.reader))
            )
        )
    except _nip44.Nip44Error:
        return None
    if inside is None:
        return None
    if version_of(inside.get("version"), LEASE_VERSION) is None:
        return None
    address = inside.get("address")
    if not is_deposit_address(address):
        return None
    if onchain_registry_address(registry.key, address) != registry_address:
        return None
    sats = _json.as_safe_integer(inside.get("sats"))
    if sats is None or not 1 <= sats <= SATS_MAX:
        return None
    created_at = _json.as_safe_integer(inside.get("createdAt"))
    expires_at = _json.as_safe_integer(inside.get("expiresAt"))
    if created_at is None or expires_at is None or created_at >= expires_at:
        return None
    invoice = inside.get("invoice", _ABSENT)
    verify_url = inside.get("verifyUrl", _ABSENT)
    if invoice is _ABSENT or verify_url is _ABSENT:
        # Both null for an order paid on-chain only: absent is no null (SPEC.md §2).
        return None
    if invoice is not None or verify_url is not None:
        decoded = _bolt11.decode(invoice)
        if decoded is None or decoded.network != "bc" or decoded.amount_msat != sats * 1000:
            return None
        if not is_https_url(verify_url) or not isinstance(invoice, str):
            return None
        if decoded.timestamp != created_at or decoded.expires_at != expires_at:
            return None
    if outside.end != expires_at + GRACE_SECONDS:
        return None
    sale_value = _json.as_object(inside.get("sale"))
    if sale_value is None or sale_value.get("key") != sale.key.reveal().hex():
        return None
    note = sale_value.get("note")
    price = parse_declared_price(sale_value.get("fiat"))
    if not isinstance(note, str) or not is_well_formed(note) or price is None:
        return None
    return LeaseTerms(
        address=address,
        sats=sats,
        invoice=invoice if isinstance(invoice, str) else None,
        verify_url=verify_url if isinstance(verify_url, str) else None,
        created_at=created_at,
        expires_at=expires_at,
        price=price,
        note=note,
        tolerance=tolerance_of(inside.get("tolerance")),
        # Confirmed when true; anything else, absent included: not confirmed.
        confirmed=inside.get("confirmed") is True,
    )


@dataclass(frozen=True, slots=True)
class SaleRecord:
    """A record of the sale at an address: its lease's terms, or None for a withdrawal."""

    event: Event
    terms: LeaseTerms | None


def open_sale_record(
    value: object, *, sale: SaleKeys, registry: Registry, registry_address: str
) -> Opened[SaleRecord] | Newer | None:
    """A record of the sale at `registry_address`, opened as the sale."""
    if not is_hex32(registry_address):
        # What a relay sent: a registry address is 32 bytes in hex (SPEC.md §31, Leases).
        return None

    def parse(payload: object, event: Event) -> SaleRecord | None:
        outside = parse_outside(payload)
        if outside is None:
            return None
        if outside.withdrawn:
            return SaleRecord(event=event, terms=None)
        terms = parse_inside(
            outside, sale=sale, registry=registry, registry_address=registry_address
        )
        return None if terms is None else SaleRecord(event=event, terms=terms)

    return open_record(
        value,
        record_type=LEASE_TYPE,
        key=outer_key(registry.key, registry_address, sale.author),
        address=registry_address,
        author=sale.author,
        parse=parse,
    )


def newest_first(records: Iterable[LeaseRecord]) -> list[LeaseRecord]:
    """Records from the newest: the highest `created_at`, then the lowest id (as relays keep
    them)."""
    return sorted(records, key=lambda record: (-record.created_at, record.id))


def author_holds(records: Sequence[LeaseRecord], t: int) -> bool:
    """Whether one author's records of an address hold it at `t`, walked from the newest: one
    that holds makes it held; one that holds nothing ends the walk, unless an older one holds and
    the signature of this one does not check out: it is then passed over. The signature of a
    record that holds is never checked (only a holder of the registry key seals one)."""
    walk = newest_first(records)
    # held_from[i]: whether a record from the i-th on holds (one pass, however many records).
    held_from = [False] * (len(walk) + 1)
    for index in range(len(walk) - 1, -1, -1):
        held_from[index] = held_from[index + 1] or walk[index].holds(t)
    for index, record in enumerate(walk):
        if record.holds(t):
            return True
        if not held_from[index + 1] or record.signed():
            return False
    return False


def by_author(records: Iterable[LeaseRecord]) -> dict[str, list[LeaseRecord]]:
    found: dict[str, list[LeaseRecord]] = {}
    for record in records:
        found.setdefault(record.pubkey, []).append(record)
    return found


def leased(records: Iterable[LeaseRecord], t: int) -> bool:
    """Whether an address is leased at `t`: one author of its records holds it. Every author is
    walked (the signatures each walk checks are the vectors')."""
    held = [author_holds(of_author, t) for of_author in by_author(records).values()]
    return any(held)


def is_confirmed(records: Iterable[LeaseRecord], lease: Event, t: int) -> bool:
    """Whether `lease` (an event of a sale) is confirmed by `records`, the records of its
    address a complete read of a majority showed (SPEC.md §31, Leasing an address, step 3.3): it
    is among them, no record of its author there is newer (its signature not checked: one forged
    by a holder of the registry key only keeps the address from the order), and no other
    author's records lease the address at `t`."""
    found = list(records)
    if not any(record.id == lease.id for record in found):
        return False
    if any(
        record.pubkey == lease.pubkey and record.id != lease.id and record.replaces(lease)
        for record in found
    ):
        return False
    return not leased((record for record in found if record.pubkey != lease.pubkey), t)


def another_of_sale(records: Iterable[LeaseRecord], lease: Event) -> bool:
    """Whether `records` hold another record of `lease`'s sale whose signature checks out:
    another call of the order made it (SPEC.md §31, Leasing an address, step 3.3)."""
    return any(
        record.pubkey == lease.pubkey and record.id != lease.id and record.signed()
        for record in records
    )
