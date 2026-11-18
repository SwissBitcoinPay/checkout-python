"""Rate sources (SPEC.md §26): the price of one bitcoin in a currency, checked (SPEC.md §25)."""

from __future__ import annotations

import asyncio
import enum
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeAlias

from . import _json
from ._constants import CLOCK_TOLERANCE, RATE_AGE
from ._crypto import Secret
from ._http import NO_JSON, Http
from ._log import Logger
from ._prices import Price, parse_price
from .errors import NetworkError, RateError, SattlemeError, UnsupportedCurrencyError

NAMES: Final = ("coingecko", "mempool", "opennode", "coinbase", "kraken")
DEFAULT_SOURCES: Final = ("coingecko", "mempool", "coinbase", "kraken", "opennode")
MEMPOOL_CURRENCIES: Final = frozenset({"USD", "EUR", "GBP", "CAD", "CHF", "AUD", "JPY"})


@dataclass(frozen=True, slots=True)
class RatePrice:
    """What a custom rate source may return: the price of one bitcoin, and when it was set.

    `price`: an exact decimal string (never a float, which may already have lost digits).
    `time`: Unix seconds when the source set the price, or None when it does not say (its age is
    then not checked).
    """

    price: str
    time: int | None = None


CustomRateSource: TypeAlias = Callable[[str], str | RatePrice | Awaitable[str | RatePrice]]
"""A function `(currency) -> price`: the price of one bitcoin in `currency` (ISO 4217, upper
case), as an exact decimal string or a `RatePrice`, returned or awaited. It raises
`UnsupportedCurrencyError` when it has no price in that currency. An error of this package it
raises goes through as it is; any other becomes a `RateError` (its `__cause__`). A plain function
runs in a worker thread, so that it may block."""

RateSource: TypeAlias = str | CustomRateSource
RateSourceOption: TypeAlias = RateSource | Sequence[RateSource]


class Absent(enum.Enum):
    """An option left out. `None` is a value, refused where it names nothing (a rate source)."""

    ABSENT = "absent"

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT: Final = Absent.ABSENT

_ABSENT: Final = object()


def parse_rate_sources(value: object) -> tuple[RateSource, ...] | None:
    """The sources `value` names, in the order they are asked; None when it names none."""
    if _is_source(value):
        return (value,)  # type: ignore[return-value]
    if not isinstance(value, list | tuple) or not value:
        return None
    if not all(_is_source(source) for source in value):
        return None
    names = [source for source in value if isinstance(source, str)]
    if len(set(names)) != len(names):
        return None
    return tuple(value)


def _is_source(value: object) -> bool:
    return (isinstance(value, str) and value in NAMES) or (
        callable(value) and not isinstance(value, type)
    )


def source_name(source: RateSource) -> str:
    return source if isinstance(source, str) else "custom"


@dataclass(frozen=True, slots=True)
class Rate:
    """A usable price, and what the RateInfo says of it."""

    price: Price
    source: str
    currency: str
    fetched_at: int
    time: int | None


@dataclass(frozen=True, slots=True)
class Quote:
    """A price as a source gave it, before it is checked."""

    price: object
    time: object = None


async def _source_json(
    http: Http, name: str, url: str, secret_headers: Mapping[str, Secret] | None = None
) -> Mapping[str, Any]:
    status, body = await http.get_json(url, secret_headers=secret_headers)
    if not 200 <= status < 300:
        # SPEC.md §26: try again for 500 to 599, 408 and 429 (a redirect is no answer, `get`
        # says so); any other status is the source's refusal, 600 and above included.
        if 500 <= status <= 599 or status in (408, 429):
            raise NetworkError(f"{name} answered HTTP {status}")
        raise RateError(f"{name} answered HTTP {status}")
    answer = _json.as_object(body) if body is not NO_JSON else None
    if answer is None:
        raise RateError(f"{name} gave no JSON object")
    return answer


def _object(value: object, name: str) -> Mapping[str, Any]:
    answer = _json.as_object(value)
    if answer is None:
        raise RateError(f"{name}: unexpected answer")
    return answer


def _price_at(container: Mapping[str, Any], key: str, name: str, currency: str) -> object:
    value = container.get(key, _ABSENT)
    if value is _ABSENT:
        raise UnsupportedCurrencyError(f"{name} has no price in {currency}", currency=currency)
    if not isinstance(value, str) and not _json.is_number(value):
        raise RateError(f"{name}: unexpected price")
    return value


async def _coingecko(http: Http, currency: str, api_key: Secret | None) -> Quote:
    code = currency.lower()
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids=bitcoin&vs_currencies={code}&include_last_updated_at=true"
    )
    # The key goes to CoinGecko only (SPEC.md §26), a secret until the request's headers are
    # made at the transport's call.
    keyed = None if api_key is None else {"x-cg-demo-api-key": api_key}
    bitcoin = _object(
        (await _source_json(http, "coingecko", url, keyed)).get("bitcoin"), "coingecko"
    )
    time = bitcoin.get("last_updated_at")
    return Quote(
        _price_at(bitcoin, code, "coingecko", currency), time if _json.is_number(time) else None
    )


async def _mempool(http: Http, currency: str) -> Quote:
    if currency not in MEMPOOL_CURRENCIES:
        raise UnsupportedCurrencyError(f"mempool has no price in {currency}", currency=currency)
    body = await _source_json(http, "mempool", "https://mempool.space/api/v1/prices")
    time = body.get("time")
    if not _json.is_number(time):
        raise RateError("mempool: its price has no time")
    return Quote(_price_at(body, currency, "mempool", currency), time)


async def _opennode(http: Http, currency: str) -> Quote:
    body = await _source_json(http, "opennode", "https://api.opennode.com/v1/rates")
    data = _object(body.get("data"), "opennode")
    pair = data.get(f"BTC{currency}", _ABSENT)
    if pair is _ABSENT:
        raise UnsupportedCurrencyError(f"opennode has no price in {currency}", currency=currency)
    return Quote(_price_at(_object(pair, "opennode"), currency, "opennode", currency))


async def _coinbase(http: Http, currency: str) -> Quote:
    url = "https://api.coinbase.com/v2/exchange-rates?currency=BTC"
    data = _object((await _source_json(http, "coinbase", url)).get("data"), "coinbase")
    if data.get("currency") != "BTC":
        raise RateError("coinbase: not the rates of BTC")
    return Quote(_price_at(_object(data.get("rates"), "coinbase"), currency, "coinbase", currency))


async def _kraken(http: Http, currency: str) -> Quote:
    url = f"https://api.kraken.com/0/public/Ticker?pair=XBT{currency}"
    body = await _source_json(http, "kraken", url)
    errors = body.get("error")
    if not isinstance(errors, list):
        raise RateError("kraken: unexpected answer")
    if any(isinstance(e, str) and e.startswith("EQuery:Unknown asset pair") for e in errors):
        raise UnsupportedCurrencyError(f"kraken has no price in {currency}", currency=currency)
    if errors:
        raise RateError("kraken answered an error")
    # The result's key is not always the pair asked (XBTUSD gives XXBTZUSD): its only entry.
    entries = list(_object(body.get("result"), "kraken").values())
    if len(entries) != 1:
        raise RateError("kraken: unexpected answer")
    last = _object(entries[0], "kraken").get("c")
    if not isinstance(last, list):
        raise RateError("kraken: unexpected answer")
    return Quote(_price_at({"c0": last[0]} if last else {}, "c0", "kraken", currency))


async def _custom(source: CustomRateSource, currency: str) -> Quote:
    try:
        if inspect.iscoroutinefunction(source) or inspect.iscoroutinefunction(
            getattr(source, "__call__", None)  # noqa: B004 - an object with async __call__
        ):
            answer: object = await source(currency)  # type: ignore[misc]
        else:
            answer = await asyncio.to_thread(source, currency)
            if inspect.isawaitable(answer):
                answer = await answer
    except SattlemeError:
        raise
    except Exception as error:
        raise RateError("The custom rate source failed") from error
    if isinstance(answer, str):
        return Quote(answer)
    if isinstance(answer, RatePrice):
        if not isinstance(answer.price, str):
            raise RateError("The custom rate source gave a price that is not a string")
        return Quote(answer.price, answer.time)
    raise RateError("The custom rate source gave no price (a string, or a RatePrice)")


async def get_quote(
    source: RateSource, currency: str, *, http: Http, coingecko_api_key: Secret | None
) -> Quote:
    """The price of one bitcoin in `currency` as `source` gives it, not checked yet.
    `coingecko_api_key`: CoinGecko's key (its ASCII), sent to CoinGecko alone."""
    if not isinstance(source, str):
        return await _custom(source, currency)
    if source == "coingecko":
        return await _coingecko(http, currency, coingecko_api_key)
    if source == "mempool":
        return await _mempool(http, currency)
    if source == "opennode":
        return await _opennode(http, currency)
    if source == "coinbase":
        return await _coinbase(http, currency)
    return await _kraken(http, currency)


def check_quote(name: str, quote: Quote, currency: str, now: int) -> Rate:
    """A source's price, checked (SPEC.md §25): an exact decimal above 0, not too old."""
    price = parse_price(quote.price)
    if price is None:
        raise RateError(f"{name} gave no valid price in {currency}")
    time: int | None = None
    if quote.time is not None:
        time = _json.as_safe_integer(quote.time)
        if time is None or time > now + CLOCK_TOLERANCE:
            raise RateError(f"{name} gave an invalid date for its price")
        if now - time > RATE_AGE:
            raise RateError(f"{name}'s price in {currency} is too old")
    return Rate(price=price, source=name, currency=currency, fetched_at=now, time=time)


Fetch = Callable[[RateSource, str, int], Awaitable[Rate]]


async def first_rate(
    sources: Sequence[RateSource],
    currency: str,
    *,
    fetch: Fetch,
    now: Callable[[], int],
    logger: Logger,
) -> Rate:
    """The first usable price of `sources`, asked one at a time, in their order (SPEC.md §26)."""
    failure: NetworkError | RateError | None = None
    for index, source in enumerate(sources):
        try:
            return await fetch(source, currency, now())
        except (NetworkError, RateError, UnsupportedCurrencyError) as error:
            if isinstance(error, NetworkError | RateError):
                failure = error
            if index + 1 < len(sources):
                logger.warning(
                    "Rate source %s failed (%s): %s; trying %s",
                    source_name(source),
                    error.code,
                    error.message,
                    source_name(sources[index + 1]),
                )
    if failure is not None:
        raise failure
    names = ", ".join(source_name(source) for source in sources)
    raise UnsupportedCurrencyError(f"None of {names} has a price in {currency}", currency=currency)
