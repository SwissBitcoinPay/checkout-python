"""The rest of an underpayment (SPEC.md §31): rest records, acceptance records, asking for a rest
invoice."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from . import _bolt11, _json
from ._channel import version_of
from ._constants import (
    ACCEPTANCE_TYPE,
    ACCEPTANCE_VERSION,
    CHECKOUT_LIFE,
    CLOCK_TOLERANCE,
    REST_MIN_LEFT,
    REST_TYPE,
    REST_VERSION,
)
from ._derive import RestKeys, RestReader
from ._nostr import Event
from ._records import Newer, Opened, RandomBytes, first_d_tag, open_record, seal
from ._relay import majority
from ._text import is_hex32
from ._urls import is_https_url


def rest_record_event(
    keys: RestKeys,
    *,
    invoice: str,
    verify_url: str,
    payment_hash: str,
    expires_at: int,
    created_at: int,
    random: RandomBytes,
) -> Event:
    """A rest record: shows a rest invoice to the pages of the order's checkout links."""
    return seal(
        record_type=REST_TYPE,
        value={"version": REST_VERSION, "invoice": invoice, "verifyUrl": verify_url},
        signer=keys.secret,
        key=keys.encryption_key,
        address=keys.record_address(payment_hash),
        created_at=created_at,
        random=random,
        tags=[["expiration", str(expires_at + CHECKOUT_LIFE)]],
    )


def acceptance_event(
    keys: RestKeys, *, received: int, expires_at: int, created_at: int, random: RandomBytes
) -> Event:
    """An acceptance record: this payment counts (never the tolerance)."""
    return seal(
        record_type=ACCEPTANCE_TYPE,
        value={"version": ACCEPTANCE_VERSION, "received": received},
        signer=keys.secret,
        key=keys.encryption_key,
        address=keys.acceptance_address,
        created_at=created_at,
        random=random,
        tags=[["expiration", str(expires_at + CHECKOUT_LIFE)]],
    )


@dataclass(frozen=True, slots=True)
class RestShown:
    """A rest record as the page reads it: the invoice it shows."""

    invoice: str
    payment_hash: str
    verify_url: str
    sats: int


@dataclass(frozen=True, slots=True)
class LinkTerms:
    """What a checkout link's record says of its invoice: its sats, date and end."""

    sats: int
    created_at: int
    expires_at: int


def open_rest_record(
    value: object, keys: RestReader, link: LinkTerms | None
) -> Opened[RestShown] | Newer | None:
    """A rest record, at its own `d` tag, by the rest author (SPEC.md §31, Reading a rest record).

    `link`: the bounds of the page's link; None for the server's own read (no bounds).
    """
    address = first_d_tag(value)
    if not is_hex32(address) or address == keys.acceptance_address:
        # The acceptance record, which the read of rest records also returns: no rest record, of
        # a newer format or not.
        return None

    def parse(payload: object, _event: Event) -> RestShown | None:
        record = _json.as_object(payload)
        if record is None or version_of(record.get("version"), REST_VERSION) is None:
            return None
        invoice, verify_url = record.get("invoice"), record.get("verifyUrl")
        decoded = _bolt11.decode(invoice)
        if decoded is None or decoded.network != "bc" or decoded.amount_msat is None:
            return None
        if decoded.amount_msat <= 0 or decoded.amount_msat % 1000:
            return None
        sats = decoded.amount_msat // 1000
        if link is not None:
            if sats >= link.sats:
                return None
            if decoded.timestamp < link.created_at - CLOCK_TOLERANCE:
                return None
            if decoded.expires_at > link.expires_at + CLOCK_TOLERANCE:
                return None
        if not isinstance(invoice, str) or not isinstance(verify_url, str):
            return None
        if not is_https_url(verify_url):
            return None
        if address != keys.record_address(decoded.payment_hash):
            return None
        return RestShown(
            invoice=invoice, payment_hash=decoded.payment_hash, verify_url=verify_url, sats=sats
        )

    return open_record(
        value,
        record_type=REST_TYPE,
        key=keys.encryption_key,
        address=address,
        author=keys.author,
        parse=parse,
    )


def open_acceptance(value: object, keys: RestReader, link_sats: int) -> Opened[int] | Newer | None:
    """An acceptance record: its `received`, from 1 and below the link's `sats`."""

    def parse(payload: object, _event: Event) -> int | None:
        record = _json.as_object(payload)
        if record is None or version_of(record.get("version"), ACCEPTANCE_VERSION) is None:
            return None
        received = _json.as_safe_integer(record.get("received"))
        if received is None or not 1 <= received < link_sats:
            return None
        return received

    return open_record(
        value,
        record_type=ACCEPTANCE_TYPE,
        key=keys.encryption_key,
        address=keys.acceptance_address,
        author=keys.author,
        parse=parse,
    )


@dataclass(frozen=True, slots=True)
class Ask:
    """A rest invoice to ask for: its sats and its expiry (`expiresAt` − now)."""

    sats: int
    expiry: int


def should_ask(
    *,
    rest_sats: int | None,
    rest_bolt11: str | None,
    expires_at: int,
    now: int,
    lease_matches: bool,
    sendable: tuple[int, int] | None,
) -> Ask | None:
    """Whether a check asks for a rest invoice (SPEC.md §31, Asking for a rest invoice)."""
    if rest_sats is None or rest_bolt11 is not None:
        return None
    if now + REST_MIN_LEFT > expires_at or not lease_matches or sendable is None:
        return None
    if not sendable[0] <= rest_sats <= sendable[1]:
        return None
    return Ask(sats=rest_sats, expiry=expires_at - now)


def acceptance_known(found: Sequence[int | None], received: int, relays: int) -> bool:
    """Whether the pages already know `received` (SPEC.md §31, Publishing it): a majority of the
    `relays` (their count) each sent a valid acceptance record (`found`: the `received` of the
    newest each sent, None for none) whose `received` is at most the status's."""
    known = sum(1 for value in found if value is not None and value <= received)
    return known >= majority(relays)


def newest_acceptances(
    answers: Iterable[Iterable[Opened[int] | Newer]],
) -> tuple[list[int | None], bool]:
    """Of each relay's acceptance records (`open_acceptance`), the `received` of the newest valid
    one (None for none), and whether a relay sent one of a newer format."""
    newer = False
    found: list[int | None] = []
    for records in answers:
        newest: Opened[int] | None = None
        for opened in records:
            if isinstance(opened, Newer):
                newer = True
            elif newest is None or opened.event.replaces(newest.event):
                newest = opened
        found.append(None if newest is None else newest.value)
    return found, newer
