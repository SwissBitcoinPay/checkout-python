"""A merchant's world for the integration tests: local relays, the merchant's app (its channel
and its lot, written as the app writes them), a fake Lightning address, fake rate sources and
fake explorers. Every key is made for one run: no key but the vectors' is in the repository."""

from __future__ import annotations

import asyncio
import logging
import secrets
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import websockets.asyncio.client

from fakes.chain import FakeBlockCypher, FakeEsplora, FakeMempoolSocket, deposit_address
from fakes.http import FakeHttp
from fakes.lightning import FakeLightning
from fakes.relay import Relays
from sattleme_checkout import AsyncClient, Client
from sattleme_checkout._constants import MEMPOOL_WEBSOCKET
from sattleme_checkout._crypto import Secret, xonly_public_key
from sattleme_checkout._declaration import Registry
from sattleme_checkout._derive import (
    SaleKeys,
    channel_address,
    channel_encryption_key,
    lot_address,
    sale_keys,
)
from sattleme_checkout._key import IntegrationKey, parse_key
from sattleme_checkout._nostr import Event
from sattleme_checkout._records import seal
from sattleme_checkout._relay import fresh_key
from sattleme_checkout._text import base64url_encode

START = 1_791_378_000
# A merchant's Bitcoin node on its own network (SPEC.md §31), and one of the Internet.
NODE = "http://192.168.1.10:3006"
NODE_WEBSOCKET = "ws://192.168.1.10:3006/api/v1/ws"
INTERNET_NODE = "https://mempool.shop.example"
ABSENT: Any = object()


@dataclass
class FakeRates:
    """The five rate sources: a price per currency each, or an HTTP failure."""

    now: Callable[[], float]
    prices: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            "coingecko": {"CHF": 69012},
            "mempool": {"CHF": 69012},
            "coinbase": {"CHF": "69012"},
            "kraken": {"CHF": "69012.0"},
            "opennode": {"CHF": 69012},
        }
    )
    status: dict[str, int] = field(default_factory=dict)
    age: int = 0

    def _fail(self, name: str) -> tuple[int, Any] | None:
        status = self.status.get(name)
        return None if status is None else (status, {"error": "failure"})

    def coingecko(self, url: str) -> tuple[int, Any]:
        failed = self._fail("coingecko")
        if failed:
            return failed
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        currency = query["vs_currencies"].upper()
        prices = self.prices["coingecko"]
        found: dict[str, Any] = {"last_updated_at": int(self.now()) - self.age}
        if currency in prices:
            found[currency.lower()] = prices[currency]
        return 200, {"bitcoin": found}

    def mempool(self, _url: str) -> tuple[int, Any]:
        return self._fail("mempool") or (
            200,
            {"time": int(self.now()) - self.age, **self.prices["mempool"]},
        )

    def coinbase(self, _url: str) -> tuple[int, Any]:
        return self._fail("coinbase") or (
            200,
            {"data": {"currency": "BTC", "rates": self.prices["coinbase"]}},
        )

    def kraken(self, url: str) -> tuple[int, Any]:
        failed = self._fail("kraken")
        if failed:
            return failed
        pair = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["pair"]
        currency = pair.removeprefix("XBT")
        if currency not in self.prices["kraken"]:
            return 200, {"error": ["EQuery:Unknown asset pair"]}
        return 200, {"error": [], "result": {pair: {"c": [self.prices["kraken"][currency], "1"]}}}

    def opennode(self, _url: str) -> tuple[int, Any]:
        return self._fail("opennode") or (
            200,
            {"data": {f"BTC{c}": {c: p} for c, p in self.prices["opennode"].items()}},
        )


@dataclass
class Merchant:
    """What the merchant's app holds: the team's key, the integration's, the registry's."""

    team_secret: Secret
    team: str
    key: IntegrationKey
    key_text: str
    registry_key: Secret
    reader_secret: Secret
    reader: str

    @property
    def registry(self) -> Registry:
        return Registry(key=self.registry_key, reader=self.reader)


class World:
    """Start with `await World.start()`, end with `await world.close()`."""

    def __init__(self) -> None:
        self.now: float = START
        self.relays = Relays()
        self.relay_urls: list[str] = []
        self.http = FakeHttp()
        self.lightning = FakeLightning(now=self.clock)
        self.rates = FakeRates(now=self.clock)
        self.explorer = FakeEsplora("https://mempool.space/api")
        self.blockcypher = FakeBlockCypher()
        self.socket = FakeMempoolSocket()
        # The WebSocket of the merchant's Bitcoin node (NODE_WEBSOCKET).
        self.node_socket = FakeMempoolSocket()
        # The WebSockets opened, by URL.
        self.connections: dict[str, int] = {}
        self.lot: list[str] = []
        self.logger = logging.getLogger(f"world.{id(self)}")
        self.warnings: list[str] = []
        self.merchant: Merchant
        self.business_name = "Sunrise Bakery"
        self._channel_at = START - 3600

    def clock(self) -> float:
        return self.now

    @classmethod
    async def start(
        cls,
        *,
        relays: tuple[str, ...] = ("relay-a.example", "relay-b.example", "relay-c.example"),
        checkout_origin: str | None = "https://pay.example",
        tolerance: object = None,
        lot: int | list[str] = 5,
        channel_relays: list[str] | None = None,
        integration_key: bytes | None = None,
        lightning_address: str = "shop@breez.tips",
        metadata: str | None = None,
        now: int = START,
        business_name: str = "Sunrise Bakery",
        key_relays: int | None = None,
        bitcoin_node: object = ABSENT,
    ) -> World:
        world = cls()
        world.now = now
        world._channel_at = now - 3600
        name, domain = lightning_address.split("@")
        world.lightning.name, world.lightning.domain = name, domain
        if metadata is not None:
            world.lightning.metadata = metadata
        world.business_name = business_name
        world.relay_urls = await world.relays.start(*relays)
        await world.socket.start()
        await world.node_socket.start()
        world._routes()
        world._listen()
        team_secret = fresh_key(secrets.token_bytes)
        integration = secrets.token_bytes(32) if integration_key is None else integration_key
        reader_secret = fresh_key(secrets.token_bytes)
        team = xonly_public_key(team_secret).hex()
        in_key = world.relay_urls if key_relays is None else world.relay_urls[:key_relays]
        hosts = ",".join(url.removeprefix("wss://") for url in in_key)
        key_text = "sattleme_ck_" + ".".join(
            (
                base64url_encode(bytes.fromhex(team)),
                base64url_encode(integration),
                base64url_encode(hosts.encode()),
            )
        )
        world.merchant = Merchant(
            team_secret=team_secret,
            team=team,
            key=parse_key(key_text),
            key_text=key_text,
            registry_key=Secret(secrets.token_bytes(32)),
            reader_secret=reader_secret,
            reader=xonly_public_key(reader_secret).hex(),
        )
        world.publish_channel(
            checkout_origin=checkout_origin,
            tolerance=tolerance,
            relays=channel_relays,
            bitcoin_node=bitcoin_node,
        )
        world.publish_lot([deposit_address() for _ in range(lot)] if isinstance(lot, int) else lot)
        return world

    def _routes(self) -> None:
        self.http.route(f"https://{self.lightning.domain}/", self.lightning.handle)
        self.http.route("https://api.coingecko.com/", self.rates.coingecko)
        self.http.route("https://mempool.space/api/v1/prices", self.rates.mempool)
        self.http.route("https://api.coinbase.com/", self.rates.coinbase)
        self.http.route("https://api.kraken.com/", self.rates.kraken)
        self.http.route("https://api.opennode.com/", self.rates.opennode)
        self.http.route("https://mempool.space/api/", self.explorer.handle)
        self.http.route("https://api.blockcypher.com/v1/btc/main/", self.blockcypher.handle)

    def _listen(self) -> None:
        warnings = self.warnings

        class Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                warnings.append(record.getMessage())

        self.logger.addHandler(Collect())
        self.logger.propagate = False

    async def connect(self, url: str) -> Any:
        self.connections[url] = self.connections.get(url, 0) + 1
        if url == MEMPOOL_WEBSOCKET:
            return await websockets.asyncio.client.connect(
                f"ws://127.0.0.1:{self.socket.port}", max_size=1 << 20, open_timeout=None
            )
        if url == NODE_WEBSOCKET:
            if f"{NODE}/" in self.http.down:
                # Unreachable: refused after a while, as a connection to a host of another network.
                await asyncio.sleep(0.2)
                raise ConnectionRefusedError("the merchant's node is unreachable")
            return await websockets.asyncio.client.connect(
                f"ws://127.0.0.1:{self.node_socket.port}", max_size=1 << 20, open_timeout=None
            )
        return await self.relays.connect(url)

    def client(self, **options: Any) -> AsyncClient:
        settings: dict[str, Any] = {
            "http": self.http,
            "websocket": self.connect,
            "now": self.clock,
            "logger": self.logger,
            "relay_timeout_ms": 2000,
            "http_timeout_ms": 2000,
            **options,
        }
        return AsyncClient(self.merchant.key_text, **settings)

    def sync_client(self, **options: Any) -> Client:
        settings: dict[str, Any] = {
            "http": self.http,
            "websocket": self.connect,
            "now": self.clock,
            "logger": self.logger,
            "relay_timeout_ms": 2000,
            "http_timeout_ms": 2000,
            **options,
        }
        return Client(self.merchant.key_text, **settings)

    # The merchant's app.

    def _team_record(self, record_type: str, value: dict[str, Any], address: str) -> Event:
        key = self.merchant.key.secret
        return seal(
            record_type=record_type,
            value=value,
            signer=self.merchant.team_secret,
            key=channel_encryption_key(key),
            address=address,
            created_at=value["updatedAt"],
            random=secrets.token_bytes,
        )

    def publish_channel(
        self,
        *,
        checkout_origin: str | None = "https://pay.example",
        tolerance: object = None,
        relays: list[str] | None = None,
        status: str = "active",
        on: list[str] | None = None,
        business_name: str | None = None,
        bitcoin_node: object = ABSENT,
    ) -> Event:
        self._channel_at += 1
        value: dict[str, Any] = {
            "version": 1,
            "status": status,
            "name": "Online shop",
            "role": "integration",
            "profile": {
                "businessName": self.business_name if business_name is None else business_name,
                "lightningAddress": self.lightning.address,
                "currency": "CHF",
                "onchain": False,
            },
            "registryKey": self.merchant.registry_key.reveal().hex(),
            "registryReader": self.merchant.reader,
            "updatedAt": self._channel_at,
        }
        if relays is not None:
            value["relays"] = relays
        if checkout_origin is not None:
            value["checkout"] = {"origin": checkout_origin}
        if tolerance is not None:
            value["onchainTolerance"] = tolerance
        if bitcoin_node is not ABSENT:
            value["bitcoinNode"] = bitcoin_node
            url = bitcoin_node.get("url") if isinstance(bitcoin_node, dict) else None
            if isinstance(url, str) and url.startswith("http"):
                # The node serves the same chain as mempool.space's fake, at its own URL.
                self.http.route(f"{url}/api/", self.explorer.at(f"{url}/api"))
        event = self._team_record("team-channel", value, channel_address(self.merchant.key.secret))
        for url in on if on is not None else self.relay_urls:
            self.relays[url].store(event)
        return event

    def publish_lot(self, addresses: list[str], on: list[str] | None = None) -> Event:
        self.lot = addresses
        self._channel_at += 1
        value = {"version": 1, "addresses": addresses, "updatedAt": self._channel_at}
        event = self._team_record(
            "integration-onchain-lot", value, lot_address(self.merchant.key.secret)
        )
        for url in on if on is not None else self.relay_urls:
            self.relays[url].store(event)
        return event

    def sale(self, ref: str) -> SaleKeys:
        return sale_keys(self.merchant.key.secret, ref)

    def held(self, url: str, author: str) -> list[Event]:
        """The events of `author` a relay holds now."""
        return [
            event
            for event in self.relays[url].events.values()
            if isinstance(event, Event) and event.pubkey == author
        ]

    def received(self, *, kind: int = 30078) -> list[dict[str, Any]]:
        """Every event the relays took, once each."""
        seen: dict[str, dict[str, Any]] = {}
        for relay in self.relays.relays.values():
            for value in relay.received:
                if value["kind"] == kind:
                    seen[value["id"]] = value
        return list(seen.values())

    async def close(self) -> None:
        await self.socket.close()
        await self.node_socket.close()
        await self.relays.close()
