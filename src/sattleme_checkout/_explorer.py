"""The explorer (SPEC.md §31, Explorers): one explorer of the Esplora API, mempool.space or the
merchant's Bitcoin node when the channel names one (_node.py), mempool.space answering in its place
when the node fails and its `fallback` is "mempool"; and BlockCypher.

Every answer is checked as SPEC.md says; anything else is a failure (`network`). A payment counts
as made on what the explorer read alone shows.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, TypeVar

from . import _json
from ._constants import (
    BLOCKCYPHER,
    MEMPOOL,
    MEMPOOL_WEBSOCKET,
    NODE_FAILED_RECENTLY_SECONDS,
    SATS_MAX,
)
from ._http import NO_JSON, Http, without_bom
from ._node import BitcoinNode, Fallback, parse_node_url
from ._text import is_hex32
from .errors import NetworkError

_HEIGHT = re.compile(b"[0-9]{1,9}")
_VOUT_MAX = 4_294_967_295
_ABSENT = object()
# RFC 3339 `date-time`, a time zone required: `T` and `Z` in either case, as its ABNF allows.
# BlockCypher's spelling (SPEC.md §31, Explorers): UTC, a fraction of 1 to 9 digits, `Z`.
_RECEIVED = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,9})?Z"
)


@dataclass(frozen=True, slots=True)
class Transaction:
    """A transaction as an explorer shows it, for one address."""

    txid: str
    pays: int
    coins: tuple[str, ...]
    confirmed: bool
    block_height: int | None

    def confirmations(self, height: int) -> int:
        if not self.confirmed or self.block_height is None:
            return 0
        return max(1, height - self.block_height + 1)


def _failure(what: str) -> NetworkError:
    return NetworkError(f"The explorer gave no usable answer ({what})")


def parse_transaction(value: object, address: str) -> Transaction:
    """A transaction of an Esplora answer, and what it pays `address`; NetworkError otherwise."""
    tx = _json.as_object(value)
    if tx is None or not is_hex32(tx.get("txid")):
        raise _failure("txid")
    vin, vout, status = tx.get("vin"), tx.get("vout"), _json.as_object(tx.get("status"))
    if not isinstance(vin, list) or not isinstance(vout, list) or status is None:
        raise _failure("vin, vout, status")
    coins: list[str] = []
    for item in vin:
        coin = _json.as_object(item)
        if coin is None:
            raise _failure("vin")
        if coin.get("is_coinbase") is True:
            continue
        index = _json.as_safe_integer(coin.get("vout"))
        if not is_hex32(coin.get("txid")) or index is None or not 0 <= index <= _VOUT_MAX:
            raise _failure("vin")
        coins.append(f"{coin['txid']}:{index}")
    pays = 0
    # What its outputs hold together: every bitcoin there will ever be at most (SPEC.md §31,
    # Explorers), so that what it pays an address is a witness's `sats`.
    held = 0
    for item in vout:
        output = _json.as_object(item)
        value_sats = _json.as_safe_integer(output.get("value")) if output is not None else None
        if output is None or value_sats is None or value_sats < 0:
            raise _failure("vout")
        held += value_sats
        if held > SATS_MAX:
            raise _failure("outputs above every bitcoin")
        # A string when present (an output without an address, OP_RETURN, pays nothing): null
        # is not absent (SPEC.md §2), and no explorer's answer.
        script_address = output.get("scriptpubkey_address", _ABSENT)
        if script_address is not _ABSENT and not isinstance(script_address, str):
            raise _failure("vout")
        if script_address == address:
            pays += value_sats
    confirmed = status.get("confirmed")
    if not isinstance(confirmed, bool):
        raise _failure("status")
    block_height: int | None = None
    if confirmed:
        block_height = _json.as_safe_integer(status.get("block_height"))
        if block_height is None or block_height < 0:
            raise _failure("block_height")
    return Transaction(
        txid=tx["txid"],
        pays=pays,
        coins=tuple(coins),
        confirmed=confirmed,
        block_height=block_height,
    )


@dataclass(frozen=True, slots=True)
class Explorer:
    """An Esplora explorer over HTTP."""

    http: Http
    base: str

    async def address_transactions(self, address: str) -> list[Transaction]:
        """The transactions paying `address`, in order: those paying it nothing left out
        (SPEC.md §31, Explorers), then a txid listed twice taken once, its first entry
        (Replacements, step 1)."""
        status, body = await self.http.get_json(f"{self.base}/address/{address}/txs")
        if not 200 <= status < 300 or body is NO_JSON or not isinstance(body, list):
            raise _failure(f"HTTP {status}")
        transactions: dict[str, Transaction] = {}
        for value in body:
            tx = parse_transaction(value, address)
            if tx.pays > 0:
                transactions.setdefault(tx.txid, tx)
        return list(transactions.values())

    async def height(self) -> int:
        """The chain's height: a body of 1 to 9 digits (plain text)."""
        response = await self.http.get(f"{self.base}/blocks/tip/height")
        body = without_bom(response.body)
        if not 200 <= response.status < 300 or _HEIGHT.fullmatch(body) is None:
            raise _failure("height")
        return int(body)


T = TypeVar("T")

ExplorerName = Literal["node", "mempool"]
"""An explorer a read may ask: the merchant's Bitcoin node, or mempool.space."""

NodeFailures = dict[str, int]
"""When the nodes a call read failed a read, by node URL (the clock's Unix seconds): of one call
alone (create_invoice, get_status, one wait_for_payment), never kept between calls."""


@dataclass(frozen=True, slots=True)
class Source:
    """An explorer a read asks: its name, its REST URL (`<explorer>`) and its WebSocket's."""

    name: ExplorerName
    rest: str
    web_socket: str


MEMPOOL_SOURCE = Source("mempool", MEMPOOL, MEMPOOL_WEBSOCKET)


def explorers_to_ask(fallback: Fallback | None, *, failed_recently: bool) -> list[ExplorerName]:
    """The explorers one read asks, in order (SPEC.md §31, `fallback`): without a node (None),
    mempool.space; with "none", the node alone; with "mempool", the node then mempool.space, or
    mempool.space first when the node failed a read less than 60 s ago in the same call. The next
    one is asked only when the one before failed."""
    if fallback is None:
        return ["mempool"]
    if fallback == "none":
        return ["node"]
    return ["mempool", "node"] if failed_recently else ["node", "mempool"]


class CallExplorer:
    """The explorer of one call (SPEC.md §31): mempool.space, or the channel's node with its
    fallback. Each read follows the fallback on its own; a node that fails is remembered in
    `failures`, which the call's reads (and its WebSocket) share, so that for 60 s it is asked
    after mempool.space. `now`: the clock, Unix seconds."""

    def __init__(
        self,
        http: Http,
        node: BitcoinNode | None,
        failures: NodeFailures,
        now: Callable[[], int],
    ) -> None:
        self._http = http
        at = None if node is None else parse_node_url(node.url)
        self._node = None if at is None else Source("node", at.rest, at.web_socket)
        self._url = None if at is None else at.url
        self._fallback: Fallback | None = None if node is None or at is None else node.fallback
        self._failures = failures
        self._now = now

    def sources(self) -> list[Source]:
        """The explorers the next read asks, in order (explorers_to_ask)."""
        node = self._node
        if node is None or self._url is None:
            return [MEMPOOL_SOURCE]
        failed_at = self._failures.get(self._url)
        recently = failed_at is not None and self._now() - failed_at < NODE_FAILED_RECENTLY_SECONDS
        return [
            node if name == "node" else MEMPOOL_SOURCE
            for name in explorers_to_ask(self._fallback, failed_recently=recently)
        ]

    def node_failed(self) -> None:
        """The node failed a read (or its WebSocket a connection): remembered for the call."""
        if self._url is not None:
            self._failures[self._url] = self._now()

    async def read(self, run: Callable[[Explorer], Awaitable[T]]) -> T:
        """One read of the explorer: `run` with each explorer of sources(), in order, until one
        answers. What `run` asks of one explorer at once (the chain's height with the address's
        transactions) ends when either fails (`_tasks.both`), and the whole read is asked again
        of the next one, never part of it. NetworkError when every one failed."""
        failures: list[NetworkError] = []
        for source in self.sources():
            try:
                return await run(Explorer(self._http, source.rest))
            except NetworkError as error:
                if source.name == "node":
                    self.node_failed()
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        raise NetworkError(
            "Neither the merchant's Bitcoin node nor mempool.space answered ("
            + "; ".join(error.message for error in failures)
            + ")"
        ) from failures[-1]


_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def parse_received(text: object) -> int | None:
    """BlockCypher's `received`, as BlockCypher writes it, in Unix milliseconds; None otherwise.

    UTC, `2026-10-10T13:05:12.345Z` (a fraction of 1 to 9 digits, or none), a real date and time:
    a month of 01 to 12, a day of that month, hours 00 to 23, minutes and seconds 00 to 59 (no
    leap second). The fraction past milliseconds is dropped, never rounded. Another spelling of
    RFC 3339 is not BlockCypher's.
    """
    if not isinstance(text, str):
        return None
    match = _RECEIVED.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    fraction = match.group(7)
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    if not 1 <= month <= 12:
        return None
    days = 29 if month == 2 and leap else _DAYS[month - 1]
    if not 1 <= day <= days or hour > 23 or minute > 59 or second > 59:
        return None
    if year < 1970:
        # Not above 0 anyway.
        return None
    seconds = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))
    milliseconds = int((fraction[1:] + "00")[:3]) if fraction else 0
    return seconds * 1000 + milliseconds


async def blockcypher_received(http: Http, txid: str) -> int | None:
    """When BlockCypher first saw a transaction (Unix ms); None when it does not know it yet."""
    status, body = await http.get_json(f"{BLOCKCYPHER}/txs/{txid}?limit=1&includeHex=false")
    if status == 404:
        return None
    answer = _json.as_object(body) if body is not NO_JSON else None
    if not 200 <= status < 300 or answer is None or answer.get("hash") != txid:
        raise NetworkError(f"BlockCypher gave no usable answer (HTTP {status})")
    return received_time(answer.get("received"))


def received_time(value: object) -> int:
    """BlockCypher's `received` of a transaction it knows, in Unix ms, above 0; NetworkError for
    anything else (another spelling included)."""
    received = parse_received(value)
    if received is None or received <= 0:
        raise NetworkError("BlockCypher gave no usable time")
    return received
