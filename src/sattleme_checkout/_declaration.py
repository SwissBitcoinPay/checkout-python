"""The declaration of an invoice in the merchant's registry (SPEC.md §20)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import _bolt11, _json, _nip44
from ._address import is_deposit_address
from ._channel import version_of
from ._constants import DECLARATION_TYPE, DECLARATION_VERSION
from ._crypto import Secret
from ._derive import SaleKeys, inner_key, outer_key, registry_address
from ._nostr import Event
from ._records import Newer, Opened, RandomBytes, first_d_tag, open_record, plaintext_fits, seal
from ._text import is_hex32, is_well_formed
from ._urls import is_https_url

_CURRENCY = re.compile("[A-Z]{3}")
DIGITS_MAX = 8


@dataclass(frozen=True, slots=True)
class DeclaredPrice:
    """A sale's price as declarations hold it: `{"minor": n, "digits": d, "currency": c}`."""

    minor: int
    digits: int
    currency: str

    def to_json(self) -> dict[str, Any]:
        return {"minor": self.minor, "digits": self.digits, "currency": self.currency}


def parse_declared_price(value: object) -> DeclaredPrice | None:
    price = _json.as_object(value)
    if price is None:
        return None
    minor = _json.as_safe_integer(price.get("minor"))
    digits = _json.as_integer(price.get("digits"))
    currency = price.get("currency")
    if minor is None or minor < 0 or digits is None or not 0 <= digits <= DIGITS_MAX:
        return None
    if not isinstance(currency, str) or _CURRENCY.fullmatch(currency) is None:
        return None
    return DeclaredPrice(minor=minor, digits=digits, currency=currency)


@dataclass(frozen=True, slots=True)
class RestOf:
    """The invoice an invoice of a rest is of: its deposit address and its end."""

    address: str
    expires_at: int

    def to_json(self) -> dict[str, Any]:
        return {"address": self.address, "expiresAt": self.expires_at}


@dataclass(frozen=True, slots=True)
class Registry:
    """The merchant's registry, as the channel gives it."""

    key: Secret
    reader: str


def declaration_value(
    *,
    sale: SaleKeys,
    invoice: str,
    verify_url: str,
    price: DeclaredPrice,
    note: str,
    rest: RestOf | None,
) -> dict[str, Any]:
    """The declaration's JSON (inside), in the order of SPEC.md §20."""
    value: dict[str, Any] = {
        "version": DECLARATION_VERSION,
        "invoice": invoice,
        "verifyUrl": verify_url,
        "sale": {"key": sale.key.reveal().hex(), "fiat": price.to_json(), "note": note},
        "taker": sale.taker,
    }
    if rest is not None:
        value["rest"] = rest.to_json()
    return value


def declaration_event(
    *,
    registry: Registry,
    sale: SaleKeys,
    payment_hash: str,
    value: dict[str, Any],
    created_at: int,
    random: RandomBytes,
) -> Event | None:
    """The signed declaration: inside sealed to the registry reader, outside a sealed record.

    None when it is too large for NIP-44 (a valid invoice may be): never an exception, whose
    frames would hold the sale key (SPEC.md §27)."""
    inside = _json.dumps(value)
    if not _nip44.fits(inside):
        return None
    outside = {
        "sealed": _nip44.encrypt(inside, inner_key(sale.secret, registry.reader), random(32))
    }
    if not plaintext_fits(DECLARATION_TYPE, outside):
        return None
    address = registry_address(registry.key, payment_hash)
    return seal(
        record_type=DECLARATION_TYPE,
        value=outside,
        signer=sale.secret,
        key=outer_key(registry.key, address, sale.author),
        address=address,
        created_at=created_at,
        random=random,
    )


@dataclass(frozen=True, slots=True)
class Declaration:
    """A declaration of a sale, opened by the sale."""

    event: Event
    invoice: str
    decoded: _bolt11.Bolt11
    verify_url: str
    price: DeclaredPrice
    note: str
    rest: RestOf | None


def _parse_rest(value: object) -> RestOf | bool | None:
    """`rest`: None when absent, the RestOf, or False when invalid."""
    if value is _ABSENT:
        return None
    rest = _json.as_object(value)
    if rest is None:
        return False
    address = rest.get("address")
    expires_at = _json.as_safe_integer(rest.get("expiresAt"))
    if not is_deposit_address(address) or expires_at is None or expires_at < 0:
        return False
    return RestOf(address=address, expires_at=expires_at)


_ABSENT = object()


def open_declaration(
    value: object, *, sale: SaleKeys, registry: Registry
) -> Opened[Declaration] | Newer | None:
    """What a relay sent, as a declaration of `sale` (SPEC.md §20, Opening)."""
    address = first_d_tag(value)
    if not is_hex32(address):
        return None
    inner = inner_key(sale.secret, registry.reader)

    def parse(outside: object, event: Event) -> Declaration | None:
        sealed = _json.as_object(outside)
        payload = sealed.get("sealed") if sealed is not None else None
        if not isinstance(payload, str):
            return None
        try:
            inside = _json.as_object(_json.parse_text(_nip44.decrypt(payload, inner)))
        except _nip44.Nip44Error:
            return None
        if inside is None:
            return None
        if version_of(inside.get("version"), DECLARATION_VERSION) is None:
            return None
        invoice, verify_url = inside.get("invoice"), inside.get("verifyUrl")
        if not isinstance(invoice, str) or not isinstance(verify_url, str):
            return None
        if not is_https_url(verify_url):
            return None
        sale_value = _json.as_object(inside.get("sale"))
        if sale_value is None or sale_value.get("key") != sale.key.reveal().hex():
            return None
        note = sale_value.get("note")
        price = parse_declared_price(sale_value.get("fiat"))
        if not isinstance(note, str) or not is_well_formed(note) or price is None:
            return None
        rest = _parse_rest(inside.get("rest", _ABSENT))
        if rest is False:
            return None
        decoded = _bolt11.decode(invoice)
        if decoded is None or decoded.network != "bc" or decoded.amount_msat is None:
            return None
        if decoded.amount_msat <= 0 or decoded.amount_msat % 1000:
            return None
        if address != registry_address(registry.key, decoded.payment_hash):
            return None
        return Declaration(
            event=event,
            invoice=invoice,
            decoded=decoded,
            verify_url=verify_url,
            price=price,
            note=note,
            rest=rest if isinstance(rest, RestOf) else None,
        )

    return open_record(
        value,
        record_type=DECLARATION_TYPE,
        key=outer_key(registry.key, address, sale.author),
        address=address,
        author=sale.author,
        parse=parse,
    )
