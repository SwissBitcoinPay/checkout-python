"""Prices in a currency (SPEC.md §25), rate sources (SPEC.md §26), currencies."""

from __future__ import annotations

import logging
from typing import Any

import pytest

import sattleme_checkout
from sattleme_checkout import currency_digits
from sattleme_checkout._currencies import CURRENCIES
from sattleme_checkout._json import loads
from sattleme_checkout._prices import (
    convert,
    parse_order_price,
    parse_price_number,
    parse_price_text,
)
from sattleme_checkout._rates import (
    DEFAULT_SOURCES,
    Quote,
    Rate,
    RateSource,
    check_quote,
    first_rate,
    parse_rate_sources,
    source_name,
)
from sattleme_checkout.errors import (
    NetworkError,
    RateError,
    SattlemeError,
    UnsupportedCurrencyError,
)
from spec_vectors import spec_file, vector

FIAT: Any = vector("fiat.json")
PRICES: Any = vector("prices.json")
CONVERSIONS: Any = vector("conversions.json")
RATES: Any = vector("rate-sources.json")


def test_the_currency_table_is_the_specification_s() -> None:
    assert spec_file("currencies.json")["currencies"] == CURRENCIES


@pytest.mark.parametrize("case", FIAT["cases"], ids=lambda c: f"{c['input']}")
def test_order_price(case: dict[str, Any]) -> None:
    price = parse_order_price(case["input"]["amount"], case["input"]["currency"])
    expected = case["expected"]
    if "error" in expected:
        assert price is None
        return
    assert price is not None
    assert {
        "amount": price.amount,
        "minor": str(price.minor),
        "digits": price.digits,
        "currency": price.currency,
    } == expected


@pytest.mark.parametrize("case", FIAT["digits"], ids=lambda c: repr(c["code"]))
def test_currency_digits(case: dict[str, Any]) -> None:
    """fiat.json, `digits` (SPEC.md §4, Functions): what the package's public `currency_digits`
    gives for a text, the decimals of the currency whose code it is exactly, or None."""
    found = currency_digits(case["code"])
    expected = case["expected"]
    assert found == expected
    assert type(found) is type(expected)


def test_currency_digits_is_the_lookup_of_an_orders_price() -> None:
    """The function a shop calls is public, and the one an order's price reads its currency's
    decimals with: every code of the table, and nothing that is not a text."""
    assert "currency_digits" in sattleme_checkout.__all__
    for code, digits in CURRENCIES.items():
        assert currency_digits(code) == digits
        price = parse_order_price("1", code)
        assert price is not None
        assert price.digits == digits
    for value in (None, 2, b"CHF", ("CHF",)):
        assert currency_digits(value) is None


@pytest.mark.parametrize("case", PRICES["text"], ids=lambda c: repr(c["price"]))
def test_price_text(case: dict[str, Any]) -> None:
    price = parse_price_text(case["price"])
    assert (None if price is None else price.plain()) == case["expected"]


@pytest.mark.parametrize("case", PRICES["numbers"], ids=lambda c: c["json"])
def test_price_number(case: dict[str, Any]) -> None:
    number = loads(case["json"])
    assert isinstance(number, int | float)
    price = parse_price_number(number)
    assert (None if price is None else price.plain()) == case["expected"]


@pytest.mark.parametrize(
    ("json", "written", "other"),
    [
        ("1660867710597813.25", "1660867710597813.2", "1660867710597813.3"),
        ("1660867710597813.75", "1660867710597813.8", "1660867710597813.7"),
        ("1125899906842624.25", "1125899906842624.2", "1125899906842624.3"),
        ("1125899906842625.75", "1125899906842625.8", "1125899906842625.7"),
    ],
)
def test_a_price_number_of_a_tie_is_written_with_its_even_digit(
    json: str, written: str, other: str
) -> None:
    """SPEC.md §25: a JSON number is written back as the shortest decimal that reads back as its
    double; of two such, the nearer; of two as near, the one whose last digit is even. Each of
    these doubles lies halfway between two decimals of 17 digits that read back as it."""
    number = loads(json)
    assert isinstance(number, float)
    assert float(written) == float(other) == number
    price = parse_price_number(number)
    assert price is not None
    assert price.plain() == written


@pytest.mark.parametrize("case", CONVERSIONS["cases"], ids=lambda c: f"{c['fiat']} {c['price']}")
def test_conversion(case: dict[str, Any]) -> None:
    price = parse_order_price(case["fiat"]["amount"], case["fiat"]["currency"])
    rate = parse_price_text(case["price"])
    assert price is not None and rate is not None
    assert str(convert(price.minor, price.digits, rate)) == case["sats"]


def test_the_default_list() -> None:
    assert list(DEFAULT_SOURCES) == RATES["default"]


@pytest.mark.parametrize("case", RATES["values"], ids=lambda c: c["name"])
def test_rate_source_values(case: dict[str, Any]) -> None:
    sources = (
        DEFAULT_SOURCES if "rateSource" not in case else parse_rate_sources(case["rateSource"])
    )
    expected = case["expected"]
    if "error" in expected:
        assert sources is None
    else:
        assert sources is not None and list(sources) == expected["sources"]


def test_custom_sources_are_functions() -> None:
    def custom(currency: str) -> str:
        return "69012"

    assert parse_rate_sources(custom) == (custom,)
    assert parse_rate_sources(["kraken", custom, custom]) == ("kraken", custom, custom)
    assert parse_rate_sources(str) is None
    assert source_name(custom) == "custom"


_ERRORS: dict[str, type[SattlemeError]] = {
    "network": NetworkError,
    "rate": RateError,
}


@pytest.mark.parametrize("case", RATES["lists"]["cases"], ids=lambda c: c["name"])
async def test_rate_source_lists(case: dict[str, Any]) -> None:
    currency, now = RATES["lists"]["currency"], RATES["lists"]["now"]
    asked: list[str] = []

    async def fetch(source: RateSource, wanted: str, at: int) -> Rate:
        assert isinstance(source, str) and wanted == currency and at == now
        asked.append(source)
        answer = case["answers"][source]
        if answer == "unsupported_currency":
            raise UnsupportedCurrencyError("none", currency=currency)
        if isinstance(answer, str):
            raise _ERRORS[answer]("failed")
        return check_quote(source, Quote(answer["price"], answer["time"]), wanted, at)

    expected = case["expected"]
    logger = logging.getLogger("test")
    if "error" in expected:
        with pytest.raises(SattlemeError) as raised:
            await first_rate(case["sources"], currency, fetch=fetch, now=lambda: now, logger=logger)
        assert raised.value.code == expected["error"]
    else:
        rate = await first_rate(
            case["sources"], currency, fetch=fetch, now=lambda: now, logger=logger
        )
        assert rate.source == expected["source"]
        assert rate.price.plain() == expected["price"]
    assert asked == expected["asked"]
