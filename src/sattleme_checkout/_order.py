"""An order's parameters, checked before any request (SPEC.md §17, The order). Never repaired."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ._constants import DURATION_MAX, DURATION_MIN, NOTE_MAX, SATS_MAX
from ._prices import OrderPrice, parse_order_price
from ._rates import ABSENT, RateSource, parse_rate_sources
from ._text import encode_utf8, has_control, trim_spaces
from ._urls import redirect_url_shown
from .errors import InvalidOrderError
from .models import Price

_REF = re.compile("[A-Za-z0-9._:-]{1,64}")
_SATS = re.compile("[0-9]{1,18}")


def is_ref(ref: object) -> bool:
    return isinstance(ref, str) and _REF.fullmatch(ref) is not None


def check_ref(ref: object) -> str:
    """`ref`: 1 to 64 characters `A-Z a-z 0-9 . _ : -`; else InvalidOrderError (`ref`)."""
    if not isinstance(ref, str) or not is_ref(ref):
        raise InvalidOrderError("ref: 1 to 64 characters of A-Z a-z 0-9 . _ : -", param="ref")
    return ref


def label_of(note: object, ref: str) -> str:
    """The note without its spaces (U+0020) around it, else the reference; 100 code points."""
    if note is None:
        return ref
    if not isinstance(note, str):
        raise InvalidOrderError("note: a string", param="note")
    if has_control(note):
        raise InvalidOrderError("note: no control characters", param="note")
    if encode_utf8(note) is None:
        raise InvalidOrderError("note: well-formed Unicode", param="note")
    label = trim_spaces(note) or ref
    if len(label) > NOTE_MAX:
        raise InvalidOrderError(f"note: {NOTE_MAX} characters at most", param="note")
    return label


def check_sats(value: object) -> int:
    """Sats: an int or a string of 1 to 18 digits, from 1 to 21 million bitcoins."""
    sats: int | None = None
    if isinstance(value, int) and not isinstance(value, bool):
        sats = value
    elif isinstance(value, str) and _SATS.fullmatch(value):
        sats = int(value)
    if sats is None or not 1 <= sats <= SATS_MAX:
        raise InvalidOrderError(f"sats: a whole number from 1 to {SATS_MAX}", param="sats")
    return sats


@dataclass(frozen=True, slots=True)
class Order:
    ref: str
    sats: int | None
    price: OrderPrice | None
    rate_sources: tuple[RateSource, ...] | None
    duration: int
    label: str
    checkout: bool
    redirect_url: str | None


def check_order(
    *,
    ref: object,
    sats: object,
    fiat: object,
    rate_source: object,
    duration_seconds: object,
    note: object,
    checkout: object,
    redirect_url: object,
    client_rate_sources: tuple[RateSource, ...],
) -> Order:
    """The order, checked; InvalidOrderError naming the parameter."""
    checked_ref = check_ref(ref)
    if (sats is None) == (fiat is None):
        raise InvalidOrderError("give either sats or fiat", param="sats")
    checked_sats = None if sats is None else check_sats(sats)
    price: OrderPrice | None = None
    if fiat is not None:
        if not isinstance(fiat, Price):
            raise InvalidOrderError('fiat: a Price("24.50", "CHF")', param="fiat")
        price = parse_order_price(fiat.amount, fiat.currency)
        if price is None:
            raise InvalidOrderError(
                "fiat: an ISO 4217 code in upper case, and an amount of digits with at most its "
                "decimals, above 0",
                param="fiat",
            )
    rate_sources: tuple[RateSource, ...] | None = None
    if rate_source is not ABSENT:
        if price is None:
            raise InvalidOrderError(
                "rate_source: only for an order priced in a currency", param="rate_source"
            )
        rate_sources = parse_rate_sources(rate_source)
        if rate_sources is None:
            raise InvalidOrderError(
                "rate_source: a source's name, a function, or a list of them", param="rate_source"
            )
    elif price is not None:
        rate_sources = client_rate_sources
    duration = duration_seconds
    if (
        not isinstance(duration, int)
        or isinstance(duration, bool)
        or not DURATION_MIN <= duration <= DURATION_MAX
    ):
        raise InvalidOrderError(
            f"duration_seconds: {DURATION_MIN} to {DURATION_MAX}", param="duration_seconds"
        )
    label = label_of(note, checked_ref)
    if not isinstance(checkout, bool):
        raise InvalidOrderError("checkout: True or False", param="checkout")
    if redirect_url is not None:
        if not checkout:
            raise InvalidOrderError("redirect_url: only with checkout=True", param="redirect_url")
        if redirect_url_shown(redirect_url) is None:
            raise InvalidOrderError(
                "redirect_url: an https URL (lowercase host, no user name nor password) or an "
                "app's link (scheme://...), 2048 characters at most, nothing to escape",
                param="redirect_url",
            )
    return Order(
        ref=checked_ref,
        sats=checked_sats,
        price=price,
        rate_sources=rate_sources,
        duration=duration,
        label=label,
        checkout=checkout,
        redirect_url=redirect_url if isinstance(redirect_url, str) else None,
    )
