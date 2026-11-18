"""Checkout links (SPEC.md §30): the merchant's web app page of one invoice."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from . import _bolt11, _json
from ._address import is_deposit_address
from ._channel import is_business_name
from ._constants import (
    CHECKOUT_LIFE,
    CHECKOUT_MARKER,
    CHECKOUT_PATH,
    CHECKOUT_TYPE,
    CHECKOUT_VERSION,
    CHECKOUT_VERSION_ONCHAIN,
    NOTE_MAX,
    SATS_CURRENCY,
    SATS_MAX,
)
from ._crypto import Secret, is_secret_key, is_xonly_point, xonly_public_key
from ._declaration import DeclaredPrice, parse_declared_price
from ._derive import LinkKeys, link_keys
from ._node import RecordNode, record_node_of
from ._nostr import Event
from ._records import NewerFormat, RandomBytes, seal
from ._text import (
    base64url_decode,
    base64url_encode,
    decode_utf8,
    has_control,
    is_hex32,
    is_well_formed,
)
from ._urls import is_https_url, join_relay_hosts, redirect_url_shown, relay_list

_NEWER_MARKER = re.compile("[a-z0-9]{1,8}")


@dataclass(frozen=True, slots=True)
class Link:
    """A link's keys: its author's secret (forgotten once published) and its link key."""

    author_secret: Secret
    author: str
    link_key: Secret
    keys: LinkKeys


def new_link(random: RandomBytes) -> Link:
    """A link's keys, from the CSPRNG; the author's secret drawn until it is a secret key."""
    link_key = Secret(random(32))
    while True:
        candidate = random(32)
        if is_secret_key(candidate):
            break
    author_secret = Secret(candidate)
    return Link(
        author_secret=author_secret,
        author=xonly_public_key(author_secret).hex(),
        link_key=link_key,
        keys=link_keys(link_key),
    )


def link_url(origin: str, author: str, link_key: Secret, relays: Sequence[str]) -> str:
    """`<origin>/pay#c.<author>.<key>.<relays>`."""
    hosts = join_relay_hosts(relays).encode("utf-8")
    parts = (
        CHECKOUT_MARKER,
        base64url_encode(bytes.fromhex(author)),
        base64url_encode(link_key.reveal()),
        base64url_encode(hosts),
    )
    return f"{origin}{CHECKOUT_PATH}#{'.'.join(parts)}"


@dataclass(frozen=True, slots=True)
class Fragment:
    author: str
    # Whoever holds it reads the link's record.
    link_key: str = field(repr=False)
    relays: tuple[str, ...]


def parse_fragment(fragment: str) -> Fragment | Literal["newer", "invalid", "other"]:
    """What a URL's fragment names (SPEC.md §30, Reading the fragment)."""
    parts = fragment.removeprefix("#").split(".")
    marker = parts[0]
    if marker != CHECKOUT_MARKER:
        return "newer" if _NEWER_MARKER.fullmatch(marker) else "other"
    if len(parts) != 4:
        return "invalid"
    author, key, relays = (base64url_decode(part) for part in parts[1:])
    if author is None or key is None or relays is None:
        return "invalid"
    if len(author) != 32 or not is_xonly_point(author) or len(key) != 32:
        return "invalid"
    hosts = decode_utf8(relays)
    if not relays or hosts is None:
        return "invalid"
    urls = relay_list(["wss://" + host for host in hosts.split(",")])
    if urls is None:
        return "invalid"
    return Fragment(author=author.hex(), link_key=key.hex(), relays=urls)


@dataclass(frozen=True, slots=True)
class RestFields:
    """The rest author and rest key a version 2 record carries."""

    author: str
    key: str


def link_value(
    *,
    business_name: str,
    invoice: str | None,
    verify_url: str | None,
    onchain: tuple[str, int, int, int] | None,
    price: DeclaredPrice | None,
    note: str,
    redirect_url: str | None,
    rest: RestFields | None,
    node: RecordNode | None = None,
) -> dict[str, Any]:
    """A checkout link record's value: version 1 without an address, 2 with one.

    `onchain`: the address, the sats, the Invoice's `created_at` and `expires_at`. `price`: None
    for an order in sats. `node`: the merchant's Bitcoin node the page reads (_node.py,
    record_node_for: the channel's with `payers` true), in a record of version 2 only, absent
    (never null) without one (SPEC.md §30).
    """
    fiat = None if price is None or price.currency == SATS_CURRENCY else price.to_json()
    if onchain is None:
        return {
            "version": CHECKOUT_VERSION,
            "businessName": business_name,
            "invoice": invoice,
            "verifyUrl": verify_url,
            "fiat": fiat,
            "note": note,
            "redirectUrl": redirect_url,
        }
    address, sats, created_at, expires_at = onchain
    value: dict[str, Any] = {
        "version": CHECKOUT_VERSION_ONCHAIN,
        "businessName": business_name,
        "invoice": invoice,
        "verifyUrl": verify_url,
        "onchainAddress": address,
        "sats": sats,
        "createdAt": created_at,
        "expiresAt": expires_at,
        "fiat": fiat,
        "note": note,
        "redirectUrl": redirect_url,
    }
    if rest is not None:
        value["restAuthor"] = rest.author
        value["restKey"] = rest.key
    if node is not None:
        value["bitcoinNode"] = node.to_json()
    return value


def link_event(
    link: Link, value: dict[str, Any], *, expires_at: int, created_at: int, random: RandomBytes
) -> Event:
    """The link's record, signed by its author, dropped by NIP-40 relays a week after the end."""
    return seal(
        record_type=CHECKOUT_TYPE,
        value=value,
        signer=link.author_secret,
        key=link.keys.encryption_key,
        address=link.keys.address,
        created_at=created_at,
        random=random,
        tags=[["expiration", str(expires_at + CHECKOUT_LIFE)]],
    )


@dataclass(frozen=True, slots=True)
class Shown:
    """What the page shows of a valid record."""

    sats: int
    payment_hash: str | None
    onchain_address: str | None
    expires_at: int
    redirect: str | None
    rest: RestFields | None
    # The merchant's Bitcoin node the page reads the chain from, with its fallback: of a record of
    # version 2 whose `bitcoinNode` is valid; None for mempool.space.
    bitcoin_node: RecordNode | None = None


_ABSENT = object()


def _note_of(value: object) -> str | None:
    if not isinstance(value, str) or has_control(value) or not 1 <= len(value) <= NOTE_MAX:
        return None
    return value if is_well_formed(value) else None


def _fiat_of(value: object) -> bool:
    """Whether a record's `fiat` is valid: null, or a price of minor units above 0, not SAT."""
    if value is None:
        return True
    price = parse_declared_price(value)
    return price is not None and price.minor >= 1 and price.currency != SATS_CURRENCY


def _invoice_of(invoice: object, verify_url: object) -> _bolt11.Bolt11 | None:
    decoded = _bolt11.decode(invoice)
    if decoded is None or decoded.network != "bc" or decoded.amount_msat is None:
        return None
    if decoded.amount_msat <= 0 or decoded.amount_msat % 1000 or not is_https_url(verify_url):
        return None
    return decoded


def read_link_value(value: object) -> Shown | None:
    """A record's value as the page reads it (SPEC.md §30, Reading); NewerFormat when newer."""
    record = _json.as_object(value)
    if record is None:
        return None
    raw_version = record.get("version")
    if _json.is_number(raw_version) and raw_version > CHECKOUT_VERSION_ONCHAIN:  # type: ignore[operator]
        raise NewerFormat
    version = _json.as_integer(raw_version)
    if version not in (CHECKOUT_VERSION, CHECKOUT_VERSION_ONCHAIN):
        return None
    if not is_business_name(record.get("businessName")):
        return None
    if not _fiat_of(record.get("fiat", _ABSENT)) or _note_of(record.get("note")) is None:
        return None
    redirect_url = record.get("redirectUrl", _ABSENT)
    if redirect_url is _ABSENT:
        return None
    redirect = None if redirect_url is None else redirect_url_shown(redirect_url)
    if redirect_url is not None and redirect is None:
        return None
    invoice = record.get("invoice", _ABSENT)
    verify_url = record.get("verifyUrl", _ABSENT)
    if version == CHECKOUT_VERSION:
        decoded = _invoice_of(invoice, verify_url)
        if decoded is None:
            return None
        return Shown(
            sats=(decoded.amount_msat or 0) // 1000,
            payment_hash=decoded.payment_hash,
            onchain_address=None,
            expires_at=decoded.expires_at,
            redirect=redirect,
            rest=None,
        )
    address = record.get("onchainAddress")
    sats = _json.as_safe_integer(record.get("sats"))
    created_at = _json.as_safe_integer(record.get("createdAt"))
    expires_at = _json.as_safe_integer(record.get("expiresAt"))
    if not is_deposit_address(address) or sats is None or not 1 <= sats <= SATS_MAX:
        return None
    if created_at is None or expires_at is None or created_at >= expires_at:
        return None
    if invoice is _ABSENT or verify_url is _ABSENT:
        # Both null for an order paid on-chain only: absent is no null (SPEC.md §2).
        return None
    payment_hash: str | None = None
    if invoice is not None or verify_url is not None:
        decoded = _invoice_of(invoice, verify_url)
        if decoded is None or decoded.amount_msat != sats * 1000:
            return None
        if decoded.timestamp != created_at or decoded.expires_at != expires_at:
            return None
        payment_hash = decoded.payment_hash
    rest_author = record.get("restAuthor", _ABSENT)
    rest_key = record.get("restKey", _ABSENT)
    rest: RestFields | None = None
    if rest_author is not _ABSENT or rest_key is not _ABSENT:
        if not is_hex32(rest_author) or not is_hex32(rest_key):
            return None
        if not is_xonly_point(bytes.fromhex(rest_author)):
            return None
        rest = RestFields(author=rest_author, key=rest_key)
    return Shown(
        sats=sats,
        payment_hash=payment_hash,
        onchain_address=address,
        expires_at=expires_at,
        redirect=redirect,
        rest=rest,
        # Invalid in any way (null included), set aside: the page reads mempool.space, and the
        # record stays valid. In version 1, ignored (above).
        bitcoin_node=record_node_of(record.get("bitcoinNode")),
    )
