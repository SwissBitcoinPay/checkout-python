"""Fake explorers: Esplora APIs (mempool.space's), BlockCypher, and mempool.space's WebSocket.
Never the network, never a real transaction."""

from __future__ import annotations

import contextlib
import json
import secrets
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import websockets.asyncio.server
from websockets.asyncio.server import ServerConnection

from sattleme_checkout._bech32 import Encoding, convert_bits, encode


def deposit_address() -> str:
    """A taproot address of a random program: a deposit address of no wallet."""
    words = convert_bits(list(secrets.token_bytes(32)), 8, 5, pad=True)
    assert words is not None
    return encode("bc", [1, *words], Encoding.BECH32M)


def txid() -> str:
    return secrets.token_hex(32)


def tx(
    txid_: str,
    pays: dict[str, int],
    *,
    inputs: list[str] | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """An Esplora transaction paying `pays` (address → sats), spending `inputs` (txid:vout)."""
    coins = inputs if inputs is not None else [f"{txid()}:0"]
    return {
        "txid": txid_,
        "vin": [
            {"txid": coin.split(":")[0], "vout": int(coin.split(":")[1]), "is_coinbase": False}
            for coin in coins
        ],
        "vout": [{"value": sats, "scriptpubkey_address": address} for address, sats in pays.items()]
        + [{"value": 1234, "scriptpubkey_address": deposit_address()}],
        "status": {"confirmed": True, "block_height": height}
        if height is not None
        else {"confirmed": False},
    }


@dataclass
class FakeEsplora:
    """An Esplora explorer: the transactions it lists for each address, and its chain's height."""

    base: str
    height: int = 900_000
    listed: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    known: dict[str, dict[str, Any]] = field(default_factory=dict)
    failing: bool = False

    def add(self, address: str, transaction: dict[str, Any]) -> None:
        self.listed.setdefault(address, []).insert(0, transaction)
        self.known[transaction["txid"]] = transaction

    def drop(self, address: str, txid_: str) -> None:
        """A transaction replaced or dropped: listed no more."""
        self.listed[address] = [t for t in self.listed.get(address, []) if t["txid"] != txid_]
        self.known.pop(txid_, None)

    def confirm(self, txid_: str, height: int) -> None:
        self.known[txid_]["status"] = {"confirmed": True, "block_height": height}

    def handle(self, url: str) -> tuple[int, Any]:
        return self._answer(self.base, url)

    def at(self, base: str) -> Callable[[str], tuple[int, Any]]:
        """The same chain served at another `<explorer>` URL: a merchant's Bitcoin node."""
        return lambda url: self._answer(base, url)

    def _answer(self, base: str, url: str) -> tuple[int, Any]:
        if self.failing:
            return 503, b"unavailable"
        path = urllib.parse.urlsplit(url).path.removeprefix(urllib.parse.urlsplit(base).path)
        if path == "/blocks/tip/height":
            return 200, str(self.height).encode()
        if path.startswith("/address/") and path.endswith("/txs"):
            return 200, self.listed.get(path.split("/")[2], [])
        return 404, b"not found"


@dataclass
class FakeBlockCypher:
    """When BlockCypher first saw each transaction (RFC 3339); unknown ones are HTTP 404."""

    received: dict[str, str] = field(default_factory=dict)
    failing: bool = False

    def handle(self, url: str) -> tuple[int, Any]:
        if self.failing:
            return 429, {"error": "Limits reached."}
        asked = urllib.parse.urlsplit(url).path.rsplit("/", 1)[1]
        if asked not in self.received:
            return 404, {"error": f"Transaction {asked} not found."}
        return 200, {"hash": asked, "received": self.received[asked]}


@dataclass
class FakeMempoolSocket:
    """mempool.space's WebSocket: it records what clients ask, and pushes what a test says."""

    connections: list[ServerConnection] = field(default_factory=list)
    messages: list[Any] = field(default_factory=list)
    opened: int = 0
    refuse: bool = False
    _server: websockets.asyncio.server.Server | None = None
    port: int = 0

    async def start(self) -> None:
        self._server = await websockets.asyncio.server.serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, socket: ServerConnection) -> None:
        if self.refuse:
            await socket.close()
            return
        self.opened += 1
        self.connections.append(socket)
        try:
            with contextlib.suppress(websockets.ConnectionClosed):
                async for raw in socket:
                    self.messages.append(json.loads(raw))
        finally:
            self.connections.remove(socket)

    def tracked(self) -> list[str]:
        return [m["track-address"] for m in self.messages if "track-address" in m]

    async def push(self, value: Any) -> None:
        for socket in list(self.connections):
            with contextlib.suppress(websockets.ConnectionClosed):
                await socket.send(json.dumps(value))

    async def drop(self) -> None:
        """Close every connection, as a lost socket."""
        for socket in list(self.connections):
            await socket.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
