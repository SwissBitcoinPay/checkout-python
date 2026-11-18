"""Prices in a currency (SPEC.md §25): an order's price, the price of one bitcoin, conversion.

Every amount is an integer; a price of one bitcoin is an exact decimal (`units` × 10^-`scale`).
No float is ever used in arithmetic: a JSON number is only read as the double every JSON reader
holds, then written as the shortest decimal that reads back as it (Python's `repr`, as
ECMAScript's `Number.prototype.toString`), then read as that text.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ._constants import (
    MINOR_MAX,
    PRICE_EXPONENT_MAX,
    PRICE_TEXT_MAX,
    SATS_CURRENCY,
)
from ._currencies import CURRENCIES

_AMOUNT = re.compile("([0-9]{1,15})(?:\\.([0-9]{1,15}))?")
_PRICE = re.compile("([0-9]+)(?:\\.([0-9]+))?(?:[eE]([+-]?)([0-9]+))?")


@dataclass(frozen=True, slots=True)
class OrderPrice:
    """An order's price in minor units of its currency."""

    amount: str
    currency: str
    minor: int
    digits: int


def currency_digits(code: object) -> int | None:
    """The decimals of the currency whose code is exactly `code` in the specification's table of
    the currencies an order may be priced in (`currencies.json`, SPEC.md §4, Functions, and
    §25): 2 for `"CHF"`, 0 for `"JPY"`, 3 for `"KWD"`. None for any other text (a code in lower
    case or around spaces, a fund, a metal, a test code, the empty text), and for anything that
    is not a text. It needs no key and reads nothing: a shop asks it whether it may offer its
    customers to pay in its currency, and in how many decimals its prices go to
    `create_invoice` (`Price`)."""
    if not isinstance(code, str):
        return None
    return CURRENCIES.get(code)


def parse_order_price(amount: object, currency: object) -> OrderPrice | None:
    """An order's price (SPEC.md §25); None when it is refused."""
    if not isinstance(amount, str) or not isinstance(currency, str):
        return None
    digits = currency_digits(currency)
    if currency == SATS_CURRENCY or digits is None:
        return None
    match = _AMOUNT.fullmatch(amount)
    if match is None:
        return None
    whole, fraction = match.group(1), (match.group(2) or "").rstrip("0")
    if len(fraction) > digits:
        return None
    minor = int(whole) * 10**digits + int(fraction.ljust(digits, "0") or "0")
    if not 1 <= minor <= MINOR_MAX:
        return None
    return OrderPrice(
        amount=minor_text(minor, digits), currency=currency, minor=minor, digits=digits
    )


def minor_text(minor: int, digits: int) -> str:
    """Minor units written with exactly `digits` decimals."""
    if digits == 0:
        return str(minor)
    whole, fraction = divmod(minor, 10**digits)
    return f"{whole}.{fraction:0{digits}d}"


@dataclass(frozen=True, slots=True)
class Price:
    """The price of one bitcoin: exactly `units` × 10^-`scale`, strictly positive."""

    units: int
    scale: int

    def plain(self) -> str:
        """Written plainly: no exponent, no trailing zero after the point, no point when whole."""
        if self.scale <= 0:
            return str(self.units * 10**-self.scale)
        whole, fraction = divmod(self.units, 10**self.scale)
        fraction_text = f"{fraction:0{self.scale}d}".rstrip("0")
        return f"{whole}.{fraction_text}" if fraction_text else str(whole)


def parse_price_text(text: object) -> Price | None:
    """A price a source wrote as a string; None when it is refused."""
    if not isinstance(text, str) or len(text) > PRICE_TEXT_MAX:
        return None
    match = _PRICE.fullmatch(text)
    if match is None:
        return None
    whole, fraction, sign, exponent = match.groups()
    exponent_value = int(exponent) if exponent is not None else 0
    if exponent_value > PRICE_EXPONENT_MAX:
        return None
    if sign == "-":
        exponent_value = -exponent_value
    fraction = fraction or ""
    units = int(whole + fraction)
    if units == 0:
        return None
    scale = len(fraction) - exponent_value
    # Normalized: no trailing zero in `units` (the value is the same, the text is shortest).
    while units % 10 == 0:
        units //= 10
        scale -= 1
    return Price(units=units, scale=scale)


def parse_price_number(value: int | float) -> Price | None:
    """A price a source wrote as a JSON number: the double, then its shortest decimal."""
    try:
        double = float(value)
    except OverflowError:
        return None
    if not math.isfinite(double):
        return None
    return parse_price_text(repr(double))


def parse_price(value: object) -> Price | None:
    """A price as a source writes it: a string, or a JSON number; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return parse_price_text(value)
    if isinstance(value, int | float):
        return parse_price_number(value)
    return None


def convert(minor: int, digits: int, price: Price) -> int:
    """ceil(minor × 10^8 × 10^scale / (units × 10^digits)), on integers: never less than due."""
    numerator = minor * 10**8
    denominator = price.units * 10**digits
    if price.scale >= 0:
        numerator *= 10**price.scale
    else:
        denominator *= 10**-price.scale
    return int(-(-numerator // denominator))
