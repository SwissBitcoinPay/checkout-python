"""The merchant's acceptance (SPEC.md §31, The merchant's acceptance): the merchant may accept by
hand, in its app, what an order underpaid on-chain received as paying it in full.

The app publishes a record of the team (the integration key opens it but cannot make one) holding
what the order had received then; a check judges the payment against that amount when it is less
than the least the order's tolerance accepts (`_onchain.due_onchain`). The majority of the
merchant's relays holds the truth: a check reads it on every relay and counts a record only when
a majority of the relays sent it. A check never publishes one, nor publishes one again: the pages
of the order's checkout links learn of it only through an acceptance record (`_rest`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from . import _json
from ._channel import version_of
from ._constants import (
    GRACE_SECONDS,
    MERCHANT_ACCEPTANCE_TYPE,
    MERCHANT_ACCEPTANCE_VERSION,
    RECORD_KIND,
)
from ._crypto import Secret
from ._derive import channel_encryption_key, merchant_acceptance_address
from ._nostr import Event
from ._records import Newer, Opened, open_record
from ._relay import RelayPool, majority


@dataclass(frozen=True, slots=True)
class MerchantAcceptanceKeys:
    """What a check opens the merchant's acceptance of an invoice with: the team (its only
    author), the channel's encryption key, its address (`d` tag); and the invoice's `sats` and
    `expiresAt`, which bound what it counts."""

    team: str
    key: Secret
    address: str
    sats: int
    expires_at: int


def merchant_acceptance_keys(
    integration_key: Secret, team: str, sale: str, address: str, *, sats: int, expires_at: int
) -> MerchantAcceptanceKeys:
    """The keys of the merchant's acceptance of the invoice of the sale `sale` (its author) with
    the deposit address `address`, of `sats`, ending at `expires_at`, for the integration
    `integration_key` of `team`."""
    return MerchantAcceptanceKeys(
        team=team,
        key=channel_encryption_key(integration_key),
        address=merchant_acceptance_address(integration_key, sale, address, expires_at),
        sats=sats,
        expires_at=expires_at,
    )


@dataclass(frozen=True, slots=True)
class NewerSetAside:
    """A merchant's acceptance of a newer format dated after the lease's end: set aside as any
    record dated so, warned of all the same."""

    event: Event


OpenedAcceptance = Opened[int] | Newer | NewerSetAside
"""A merchant's acceptance a relay sent, opened: valid (what the order had received when the
merchant accepted it), of a newer format, or of a newer format and set aside."""


def _received(payload: object, keys: MerchantAcceptanceKeys) -> int | None:
    """Its value: an object whose `version` is 1 and whose `received` is a safe integer from 1,
    below `sats` (more fields ignored); NewerFormat for a `version` above 1."""
    value = _json.as_object(payload)
    if value is None or version_of(value.get("version"), MERCHANT_ACCEPTANCE_VERSION) is None:
        return None
    received = _json.as_safe_integer(value.get("received"))
    if received is None or not 1 <= received < keys.sats:
        return None
    return received


def open_merchant_acceptance(
    value: object, keys: MerchantAcceptanceKeys
) -> OpenedAcceptance | None:
    """What a relay sent, read as the merchant's acceptance of the invoice of `keys` (SPEC.md §31,
    Reading it): a record of type `integration-onchain-accepted`, author the team, sealed with the
    channel's key, at its address, that checks out (its id, its signature); of a newer format when
    its `version` is above 1 or its content of a newer NIP-44 version. None for anything else, and
    for a record dated after the lease's end (`expiresAt` + 120), set aside whatever its format:
    one of a newer format is still told (`NewerSetAside`), for the warning. Its `created_at` is
    never compared with now."""
    opened = open_record(
        value,
        record_type=MERCHANT_ACCEPTANCE_TYPE,
        key=keys.key,
        address=keys.address,
        author=keys.team,
        parse=lambda payload, _event: _received(payload, keys),
    )
    if opened is None:
        return None
    # After the lease's end: set aside, of a newer format or not (SPEC.md §31, Reading it).
    late = opened.event.created_at > keys.expires_at + GRACE_SECONDS
    if isinstance(opened, Newer):
        return NewerSetAside(opened.event) if late else opened
    return None if late else opened


@dataclass(frozen=True, slots=True)
class MerchantAcceptance:
    """The merchant's acceptance of an invoice, as a check counts it: the `received` of the one
    that counts (None without one), and whether a relay sent one of a newer format, whatever its
    date and however many relays sent it."""

    received: int | None = None
    newer: bool = False


def merchant_acceptance_of(
    answers: Iterable[Sequence[OpenedAcceptance]], relays: int
) -> MerchantAcceptance:
    """The merchant's acceptance of an invoice (SPEC.md §31, Counting it), from what each relay of
    the merchant's list (`relays`, their count) sent to its read on every relay, opened
    (`open_merchant_acceptance`: a copy that checks out). A relay sent a record when it sent a
    copy of it, counted once whatever copies it sent. Of the records valid or of a newer format
    that a majority of the list sent, the newest (the highest `created_at`, then the lowest id,
    as relays keep them) decides: a valid one is the merchant's acceptance, one of a newer format
    leaves the order without one. One that fewer than a majority sent never counts, whichever
    relays sent it."""
    records: dict[str, Opened[int] | Newer] = {}
    sent: dict[str, int] = {}
    newer = False
    for opened_by_relay in answers:
        ids: set[str] = set()
        for opened in opened_by_relay:
            if not isinstance(opened, Opened):
                newer = True
            if isinstance(opened, NewerSetAside):
                continue
            event_id = opened.event.id
            if event_id in ids:
                # One relay counts once for a record, whatever copies of it it sent.
                continue
            ids.add(event_id)
            records.setdefault(event_id, opened)
            sent[event_id] = sent.get(event_id, 0) + 1
    wanted = majority(relays)
    newest: Opened[int] | Newer | None = None
    for event_id, opened in records.items():
        if sent[event_id] >= wanted and (newest is None or opened.event.replaces(newest.event)):
            newest = opened
    received = newest.value if isinstance(newest, Opened) else None
    return MerchantAcceptance(received=received, newer=newer)


def merchant_acceptance_filter(keys: MerchantAcceptanceKeys) -> dict[str, list[object]]:
    """The query of the merchant's acceptance of an invoice: the team's records at its address."""
    return {"kinds": [RECORD_KIND], "authors": [keys.team], "#d": [keys.address]}


async def read_merchant_acceptance(
    pool: RelayPool, relays: Sequence[str], keys: MerchantAcceptanceKeys
) -> MerchantAcceptance:
    """The merchant's acceptance of the invoice of `keys` (`merchant_acceptance_of`), read on
    every relay of the merchant's `relays` (SPEC.md §13: once every relay answered or failed, or
    one timeout after a majority answered), not paged, with a key of this read alone.
    NetworkError when fewer than a majority answered. Publishes nothing."""
    answers: dict[str, list[OpenedAcceptance]] = await pool.read(
        relays,
        merchant_acceptance_filter(keys),
        lambda value: open_merchant_acceptance(value, keys),
        every_relay=True,
    )
    return merchant_acceptance_of(answers.values(), len(relays))
