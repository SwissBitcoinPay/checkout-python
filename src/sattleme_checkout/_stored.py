"""Stored invoices: read back from their JSON, and checked before a payment check (SPEC.md §22)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import _bolt11, _json
from ._address import is_deposit_address
from ._constants import SATS_MAX
from ._order import is_ref
from ._text import is_hex32
from ._urls import is_https_url
from .errors import InvalidOrderError
from .models import DeclaredInvoice, Fiat, Invoice, RateInfo

_DIGITS = re.compile("[0-9]+")
# SPEC.md §22: 1 to 16 digits, a leading zero allowed (so never more than 4 300 for `int`).
_SATS = re.compile("[0-9]{1,16}")
_MISSING: Any = object()
_NOT_AN_INVOICE = "invoice: not an invoice made by create_invoice"


def _refuse(detail: str = "") -> InvalidOrderError:
    return InvalidOrderError(_NOT_AN_INVOICE + (f" ({detail})" if detail else ""), param="invoice")


def sats_of(value: object) -> int | None:
    """A string of 1 to 16 digits (0 to 9, a leading zero allowed) from 1 to
    2 100 000 000 000 000 (SPEC.md §22); None otherwise: `"00000000000000021"`, 17 digits, is
    none, whatever its value."""
    if not isinstance(value, str) or _SATS.fullmatch(value) is None:
        return None
    sats = int(value)
    return sats if 1 <= sats <= SATS_MAX else None


@dataclass(frozen=True, slots=True)
class InvoiceToCheck:
    """What a payment check needs of an invoice, checked."""

    ref: str | None
    bolt11: str | None
    payment_hash: str | None
    verify_url: str | None
    onchain_address: str | None
    sats: int
    expires_at: int


def invoice_to_check(value: object) -> InvoiceToCheck:
    """An Invoice, a DeclaredInvoice or their JSON, checked (SPEC.md §22); InvalidOrderError.

    Every member the check reads is there, but `onchainAddress` (SPEC.md §2: one left out is
    not null): `bolt11`, `paymentHash` and `verifyUrl` absent are no nulls, nor `ref` with an
    address. The check proves that the invoice is well formed, never who made it (SPEC.md §27):
    the shop checks only the invoices it stored itself."""
    if isinstance(value, Invoice | DeclaredInvoice):
        value = value.to_json()
    if not isinstance(value, Mapping):
        raise _refuse()
    stored: Mapping[str, Any] = value
    sats = sats_of(stored.get("sats"))
    expires_at = _json.as_safe_integer(stored.get("expiresAt"))
    if sats is None or expires_at is None:
        raise _refuse("sats, expiresAt")
    bolt11 = stored.get("bolt11", _MISSING)
    payment_hash = stored.get("paymentHash", _MISSING)
    verify_url = stored.get("verifyUrl", _MISSING)
    if bolt11 is None:
        if payment_hash is not None or verify_url is not None:
            raise _refuse("paymentHash, verifyUrl")
    else:
        decoded = _bolt11.decode(bolt11)
        if (
            decoded is None
            or decoded.network != "bc"
            or decoded.amount_msat != sats * 1000
            or not is_hex32(payment_hash)
            or decoded.payment_hash != payment_hash
            or decoded.expires_at != expires_at
            or not is_https_url(verify_url)
        ):
            raise _refuse("bolt11")
    address = stored.get("onchainAddress")
    if address is not None and not is_deposit_address(address):
        raise _refuse("onchainAddress")
    if bolt11 is None and address is None:
        raise _refuse("neither bolt11 nor onchainAddress")
    ref = stored.get("ref")
    if address is not None and not is_ref(ref):
        raise _refuse("ref")
    return InvoiceToCheck(
        ref=ref if isinstance(ref, str) and is_ref(ref) else None,
        bolt11=bolt11 if isinstance(bolt11, str) else None,
        payment_hash=payment_hash if isinstance(payment_hash, str) else None,
        verify_url=verify_url if isinstance(verify_url, str) else None,
        onchain_address=address if isinstance(address, str) else None,
        sats=sats,
        expires_at=expires_at,
    )


def _string(value: Mapping[str, Any], name: str, *, nullable: bool = False) -> Any:
    field = value.get(name, _MISSING)
    if (field is None and nullable) or isinstance(field, str):
        return field
    raise _refuse(name)


def _integer(value: Mapping[str, Any], name: str, *, nullable: bool = False) -> Any:
    field = value.get(name, _MISSING)
    if field is None and nullable:
        return None
    number = _json.as_safe_integer(field)
    if number is None:
        raise _refuse(name)
    return number


def _sats(value: Mapping[str, Any], name: str) -> int:
    sats = sats_of(value.get(name))
    if sats is None:
        raise _refuse(name)
    return sats


def _fiat(value: object) -> Fiat | None:
    """`fiat`: null, or a Fiat; absent (`_MISSING`) is no null (SPEC.md §3)."""
    if value is None:
        return None
    fiat = _json.as_object(value)
    if fiat is None:
        raise _refuse("fiat")
    minor = fiat.get("minor")
    if not isinstance(minor, str) or _DIGITS.fullmatch(minor) is None or len(minor) > 16:
        raise _refuse("fiat")
    return Fiat(
        amount=_string(fiat, "amount"),
        currency=_string(fiat, "currency"),
        minor=int(minor),
        digits=_integer(fiat, "digits"),
    )


def _rate(value: object) -> RateInfo | None:
    """`rate`: null, or a RateInfo; absent (`_MISSING`) is no null (SPEC.md §3)."""
    if value is None:
        return None
    rate = _json.as_object(value)
    if rate is None:
        raise _refuse("rate")
    return RateInfo(
        source=_string(rate, "source"),
        currency=_string(rate, "currency"),
        price=_string(rate, "price"),
        fetched_at=_integer(rate, "fetchedAt"),
        time=_integer(rate, "time", nullable=True),
    )


def _object(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _refuse()
    return value


def invoice_from_json(value: object) -> Invoice:
    stored = _object(value)
    return Invoice(
        ref=_string(stored, "ref"),
        bolt11=_string(stored, "bolt11", nullable=True),
        payment_hash=_string(stored, "paymentHash", nullable=True),
        verify_url=_string(stored, "verifyUrl", nullable=True),
        onchain_address=_string(stored, "onchainAddress", nullable=True),
        payment_uri=_string(stored, "paymentUri"),
        sats=_sats(stored, "sats"),
        fiat=_fiat(stored.get("fiat", _MISSING)),
        rate=_rate(stored.get("rate", _MISSING)),
        note=_string(stored, "note"),
        created_at=_integer(stored, "createdAt"),
        expires_at=_integer(stored, "expiresAt"),
        checkout_url=_string(stored, "checkoutUrl", nullable=True),
        redirect_url=_string(stored, "redirectUrl", nullable=True),
    )


def declared_from_json(value: object) -> DeclaredInvoice:
    stored = _object(value)
    return DeclaredInvoice(
        ref=_string(stored, "ref"),
        bolt11=_string(stored, "bolt11", nullable=True),
        payment_hash=_string(stored, "paymentHash", nullable=True),
        verify_url=_string(stored, "verifyUrl", nullable=True),
        onchain_address=_string(stored, "onchainAddress", nullable=True),
        payment_uri=_string(stored, "paymentUri"),
        sats=_sats(stored, "sats"),
        fiat=_fiat(stored.get("fiat", _MISSING)),
        note=_string(stored, "note"),
        created_at=_integer(stored, "createdAt"),
        expires_at=_integer(stored, "expiresAt"),
        declared_at=_integer(stored, "declaredAt"),
    )
