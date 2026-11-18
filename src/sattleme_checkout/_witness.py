"""Witnesses (SPEC.md §31, Witnesses): the integration saw a transaction paying an address."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from . import _json
from ._address import is_deposit_address
from ._channel import version_of
from ._constants import (
    RECORD_KIND,
    SATS_MAX,
    WITNESS_INPUTS_MAX,
    WITNESS_TYPE,
    WITNESS_VERSION,
)
from ._crypto import Secret
from ._derive import SeenKeys, address_tag, order_tag, witness_address
from ._nostr import Event, check_event
from ._records import Newer, Opened, RandomBytes, first_d_tag, open_record, seal
from ._text import is_hex32

_COIN = re.compile("([0-9a-f]{64}):(0|[1-9][0-9]*)")
_VOUT_MAX = 4_294_967_295


def is_coin(value: object) -> bool:
    """`<txid>:<vout>`: the txid in lowercase hex, the vout in decimal without leading zeros."""
    if not isinstance(value, str):
        return False
    match = _COIN.fullmatch(value)
    return match is not None and len(match.group(2)) <= 10 and int(match.group(2)) <= _VOUT_MAX


@dataclass(frozen=True, slots=True)
class Witness:
    """A witness: `sale` None for `none`. `expires_at`: the `expiresAt` of the invoice whose
    lease the transaction came in (or stands for); None for `none`."""

    address: str
    txid: str
    sale: str | None
    sats: int
    inputs: tuple[str, ...]
    replacement: bool
    seen_at: int
    expires_at: int | None


def witness_value(witness: Witness) -> dict[str, Any]:
    return {
        "version": WITNESS_VERSION,
        "address": witness.address,
        "txid": witness.txid,
        "sale": witness.sale,
        "sats": witness.sats,
        "inputs": list(witness.inputs[:WITNESS_INPUTS_MAX]),
        "replacement": witness.replacement,
        "expiresAt": None if witness.sale is None else witness.expires_at,
    }


def witness_event(
    seen: SeenKeys, channel_key: Secret, witness: Witness, random: RandomBytes
) -> Event:
    return seal(
        record_type=WITNESS_TYPE,
        value=witness_value(witness),
        signer=seen.secret,
        key=channel_key,
        address=witness_address(seen, witness.address, witness.txid, witness.sale),
        created_at=witness.seen_at,
        random=random,
        tags=[
            ["o", order_tag(seen, witness.address, witness.sale)],
            ["o", address_tag(seen, witness.address)],
        ],
    )


def parse_witness(value: object, event: Event, seen: SeenKeys) -> Witness | None:
    """A witness's value; None when invalid; NewerFormat when newer."""
    witness = _json.as_object(value)
    if witness is None:
        return None
    if version_of(witness.get("version"), WITNESS_VERSION) is None:
        return None
    if "sale" not in witness:
        # Null for none: a witness without it is none of them (SPEC.md §2: absent is no null).
        return None
    address, txid, sale = witness.get("address"), witness.get("txid"), witness["sale"]
    sats = _json.as_safe_integer(witness.get("sats"))
    inputs, replacement = witness.get("inputs"), witness.get("replacement")
    if not is_deposit_address(address) or not is_hex32(txid):
        return None
    if sale is not None and not is_hex32(sale):
        return None
    if sats is None or not 1 <= sats <= SATS_MAX:
        return None
    if not isinstance(inputs, list) or len(inputs) > WITNESS_INPUTS_MAX:
        return None
    if not all(is_coin(coin) for coin in inputs) or len(set(inputs)) != len(inputs):
        return None
    if not isinstance(replacement, bool) or (sale is None and replacement):
        return None
    # Null for none, the invoice's end for a sale; a witness without it does not open.
    if "expiresAt" not in witness:
        return None
    expires_at: int | None = None
    if sale is None:
        if witness["expiresAt"] is not None:
            return None
    else:
        expires_at = _json.as_safe_integer(witness["expiresAt"])
        if expires_at is None:
            return None
    if event.tag("d") != witness_address(seen, address, txid, sale):
        return None
    # Both `o` tags: the order tag of its address and sale, and the address tag of its address.
    o_tags = {t[1] for t in event.tags if len(t) >= 2 and t[0] == "o"}
    if order_tag(seen, address, sale) not in o_tags or address_tag(seen, address) not in o_tags:
        return None
    return Witness(
        address=address,
        txid=txid,
        sale=sale,
        sats=sats,
        inputs=tuple(inputs),
        replacement=replacement,
        seen_at=event.created_at,
        expires_at=expires_at,
    )


def open_witness(
    value: object, seen: SeenKeys, channel_key: Secret
) -> Opened[Witness] | Newer | None:
    """What a relay sent, as a witness of this integration (at its own `d` tag)."""
    address = first_d_tag(value)
    if not is_hex32(address):
        return None
    return open_record(
        value,
        record_type=WITNESS_TYPE,
        key=channel_key,
        address=address,
        author=seen.author,
        parse=lambda payload, event: parse_witness(payload, event, seen),
    )


@dataclass(frozen=True, slots=True)
class InvoiceWitnesses:
    """The witnesses of an invoice with an address `A`, for its order's sale `S`: of this
    invoice (of `S` with its `expiresAt`), of other invoices (of another sale,
    or of `S` with another `expiresAt`), of `none`, and the witness addresses of those of a newer
    format."""

    of_invoice: Mapping[str, Witness]
    of_others: Mapping[str, Witness]
    of_none: Mapping[str, Witness]
    newer: frozenset[str]

    @staticmethod
    def none() -> InvoiceWitnesses:
        return InvoiceWitnesses({}, {}, {}, frozenset())


def read_witness(
    value: object, seen: SeenKeys, channel_key: Secret, address: str, tags: Iterable[str]
) -> Opened[Witness] | Newer | None:
    """What a relay sent to the read of the witnesses of an invoice with the address `address`
    (SPEC.md §31, Reading the witnesses of an invoice), whose `o` tags are `tags`: a witness that
    holds one of them and, unless of a newer format (its value cannot be read, its tags can), whose
    `address` is `address`. Anything else is of no invoice of the read: only a relay that does not
    keep to the filter sends it."""
    opened = open_witness(value, seen, channel_key)
    if opened is None:
        return None
    wanted = set(tags)
    if not any(len(tag) >= 2 and tag[0] == "o" and tag[1] in wanted for tag in opened.event.tags):
        return None
    if isinstance(opened, Opened) and opened.value.address != address:
        return None
    return opened


def invoice_witnesses(
    witnesses: Iterable[Opened[Witness] | Newer], sale: str, expires_at: int
) -> InvoiceWitnesses:
    """Of each witness address, the newest of the witnesses a read sent (`read_witness`) is the
    witness; one of a newer format is kept as such (its `d` tag can be told, not what it names)."""
    newest: dict[str, Opened[Witness] | Newer] = {}
    for opened in witnesses:
        d_tag = opened.event.tag("d") or ""
        current = newest.get(d_tag)
        if current is None or opened.event.replaces(current.event):
            newest[d_tag] = opened
    of_invoice: dict[str, Witness] = {}
    of_others: dict[str, Witness] = {}
    of_none: dict[str, Witness] = {}
    newer: set[str] = set()
    for d_tag, opened in newest.items():
        if isinstance(opened, Newer):
            newer.add(d_tag)
            continue
        witness = opened.value
        if witness.sale is None:
            of_none[witness.txid] = witness
        elif witness.sale == sale and witness.expires_at == expires_at:
            of_invoice[witness.txid] = witness
        else:
            of_others[witness.txid] = witness
    return InvoiceWitnesses(of_invoice, of_others, of_none, frozenset(newer))


def invoice_tags(seen: SeenKeys, address: str, sale: str) -> list[str]:
    """The `o` tags of the read of an invoice's witnesses: every one of its sale and of `none`
    at `address`, and every one of another sale that names `address` with its address tag."""
    return [
        order_tag(seen, address, sale),
        order_tag(seen, address, None),
        address_tag(seen, address),
    ]


def invoice_filter(seen: SeenKeys, address: str, sale: str) -> dict[str, Any]:
    """The query of an invoice's witnesses (`invoice_tags`)."""
    return {
        "kinds": [RECORD_KIND],
        "authors": [seen.author],
        "#o": invoice_tags(seen, address, sale),
    }


def addresses_named(
    value: object, seen: SeenKeys, by_tag: Mapping[str, str]
) -> frozenset[str] | None:
    """The addresses a witness names (SPEC.md §31, Leasing an address, step 3), of those whose
    address tags are `by_tag`'s keys: an event of the seen author that checks out, with an
    address's tag as an `o` tag, names that address, one of a newer format included. None when
    it names none."""
    event = _json.as_object(value)
    tags = event.get("tags") if event is not None else None
    if event is None or event.get("pubkey") != seen.author or not isinstance(tags, list):
        return None
    tagged = frozenset(
        by_tag[tag[1]]
        for tag in tags
        if isinstance(tag, list)
        and len(tag) >= 2
        and tag[0] == "o"
        and isinstance(tag[1], str)
        and tag[1] in by_tag
    )
    if not tagged or check_event(value) is None:
        return None
    return tagged
