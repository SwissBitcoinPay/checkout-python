"""Every rate source's answers and failures (SPEC.md §26), through the package's HTTP."""

from __future__ import annotations

from typing import Any

import pytest

from fakes.http import FakeHttp
from sattleme_checkout._crypto import Secret
from sattleme_checkout._http import Http
from sattleme_checkout._rates import check_quote, get_quote
from sattleme_checkout.errors import (
    NetworkError,
    RateError,
    SattlemeError,
    UnsupportedCurrencyError,
)
from spec_vectors import vector

NOW = 1_791_378_000

URLS = {
    "coingecko": "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bitcoin&vs_currencies=chf&include_last_updated_at=true",
    "mempool": "https://mempool.space/api/v1/prices",
    "opennode": "https://api.opennode.com/v1/rates",
    "coinbase": "https://api.coinbase.com/v2/exchange-rates?currency=BTC",
    "kraken": "https://api.kraken.com/0/public/Ticker?pair=XBTCHF",
}

GOOD: dict[str, Any] = {
    "coingecko": {"bitcoin": {"chf": 69012.5, "last_updated_at": NOW - 10}},
    "mempool": {"time": NOW - 10, "USD": 1, "CHF": 69012.5},
    "opennode": {"data": {"BTCCHF": {"CHF": 69012.5}}},
    "coinbase": {"data": {"currency": "BTC", "rates": {"CHF": "69012.5"}}},
    "kraken": {"error": [], "result": {"XXBTZCHF": {"c": ["69012.50000", "0.1"]}}},
}


async def _quote(name: str, status: int, body: Any, currency: str = "CHF") -> Any:
    http = FakeHttp()
    http.route(URLS[name].split("?")[0], lambda _url: (status, body))
    quote = await get_quote(name, currency, http=Http(http, 1000), coingecko_api_key=None)
    rate = check_quote(name, quote, currency, NOW)
    assert http.urls() == [URLS[name]] or currency != "CHF"
    return rate


@pytest.mark.parametrize("name", list(URLS))
async def test_a_price(name: str) -> None:
    rate = await _quote(name, 200, GOOD[name])
    assert rate.price.plain() == "69012.5"
    assert rate.time == (NOW - 10 if name in ("coingecko", "mempool") else None)


@pytest.mark.parametrize("name", list(URLS))
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (500, NetworkError),
        (503, NetworkError),
        (599, NetworkError),
        (408, NetworkError),
        (429, NetworkError),
        # A redirect is no answer (SPEC.md §15).
        (301, NetworkError),
        (308, NetworkError),
        (400, RateError),
        (401, RateError),
        (404, RateError),
        (418, RateError),
        (499, RateError),
        # Not 5xx: the source's refusal (SPEC.md §26), as `http.client` gives any status.
        (600, RateError),
        (999, RateError),
        (101, RateError),
    ],
)
async def test_an_http_failure(name: str, status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        await _quote(name, status, {"error": "x"})


@pytest.mark.parametrize("name", list(URLS))
@pytest.mark.parametrize("case", vector("rate-sources.json")["statuses"], ids=lambda c: c["status"])
async def test_the_statuses_of_the_vectors(name: str, case: dict[str, Any]) -> None:
    """rate-sources.json, `statuses`: whichever source answers a status other than 2xx (a
    redirect is no answer, SPEC.md §15), `network` for 500 to 599, 408 and 429, else `rate`
    (SPEC.md §26), 600 and above included."""
    with pytest.raises(SattlemeError) as raised:
        await _quote(name, case["status"], {"error": "x"})
    assert {"error": raised.value.code} == case["expected"]


@pytest.mark.parametrize("name", list(URLS))
@pytest.mark.parametrize("body", [b"not json", [1, 2], "a string", None])
async def test_not_a_json_object(name: str, body: Any) -> None:
    with pytest.raises(RateError):
        await _quote(name, 200, body)


@pytest.mark.parametrize(
    ("name", "body", "error"),
    [
        ("coingecko", {"bitcoin": {"last_updated_at": NOW}}, UnsupportedCurrencyError),
        ("coingecko", {"bitcoin": []}, RateError),
        ("coingecko", {}, RateError),
        ("coingecko", {"bitcoin": {"chf": None}}, RateError),
        ("coingecko", {"bitcoin": {"chf": [1]}}, RateError),
        ("mempool", {"time": NOW, "USD": 1}, UnsupportedCurrencyError),
        ("mempool", {"CHF": 1}, RateError),
        ("mempool", {"time": "now", "CHF": 1}, RateError),
        ("mempool", {"time": NOW + 301, "CHF": 1}, RateError),
        ("mempool", {"time": NOW - 3601, "CHF": 1}, RateError),
        ("mempool", {"time": NOW - 3600, "CHF": True}, RateError),
        ("opennode", {"data": {"BTCUSD": {"USD": 1}}}, UnsupportedCurrencyError),
        ("opennode", {"data": {"BTCCHF": {}}}, UnsupportedCurrencyError),
        ("opennode", {"data": []}, RateError),
        ("opennode", {"data": {"BTCCHF": 1}}, RateError),
        ("coinbase", {"data": {"currency": "ETH", "rates": {"CHF": "1"}}}, RateError),
        ("coinbase", {"data": {"currency": "BTC", "rates": {}}}, UnsupportedCurrencyError),
        ("coinbase", {"data": {"currency": "BTC", "rates": []}}, RateError),
        ("coinbase", {"data": {"currency": "BTC", "rates": {"CHF": "-1"}}}, RateError),
        ("coinbase", {"data": {"currency": "BTC", "rates": {"CHF": "1e65"}}}, RateError),
        ("kraken", {"error": ["EQuery:Unknown asset pair"]}, UnsupportedCurrencyError),
        ("kraken", {"error": ["EGeneral:Too many requests"]}, RateError),
        ("kraken", {"error": "none", "result": {}}, RateError),
        ("kraken", {"error": [], "result": {}}, RateError),
        ("kraken", {"error": [], "result": {"A": {"c": ["1"]}, "B": {"c": ["1"]}}}, RateError),
        ("kraken", {"error": [], "result": {"A": {"c": "1"}}}, RateError),
        ("kraken", {"error": [], "result": {"A": {"c": [0]}}}, RateError),
    ],
)
async def test_each_sources_rules(name: str, body: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        await _quote(name, 200, body)


async def test_mempool_has_seven_currencies() -> None:
    http = FakeHttp()
    with pytest.raises(UnsupportedCurrencyError) as raised:
        await get_quote("mempool", "SEK", http=Http(http, 1000), coingecko_api_key=None)
    assert raised.value.currency == "SEK"
    # Refused before any request.
    assert http.asked == []


async def test_coingecko_asks_in_lowercase_and_sends_its_key_alone() -> None:
    http = FakeHttp()
    http.route("https://api.coingecko.com/", lambda _url: (200, {"bitcoin": {"eur": 1}}))
    await get_quote("coingecko", "EUR", http=Http(http, 1000), coingecko_api_key=Secret(b"CG-x"))
    [asked] = http.asked
    assert "vs_currencies=eur" in asked.url
    assert asked.headers["x-cg-demo-api-key"] == "CG-x"
    assert asked.headers["Accept"] == "application/json"


async def test_a_price_as_a_number_is_its_shortest_decimal() -> None:
    rate = await _quote("coingecko", 200, {"bitcoin": {"chf": 0.1 + 0.2}})
    assert rate.price.plain() == "0.30000000000000004"


async def test_errors_are_the_specifications() -> None:
    for error in (NetworkError("x"), RateError("x")):
        assert isinstance(error, SattlemeError)
        assert error.retryable is True
    assert UnsupportedCurrencyError("x", currency="SEK").retryable is False
