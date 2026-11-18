"""A local Nostr relay (NIP-01, NIP-42) for the tests: never the network.

It keeps the newest event of each address (kind, author, `d` tag) as relays do (or, given so, an
event as it is beside the others of its address), and can refuse events, require authentication
(NIP-42: an AUTH message, never an EVENT) and never answer it, cap its answers, honor a filter's
`limit` (its newest events) or answer one without it oldest first, hide events, stay mute, answer
late or never answer a query.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import websockets.asyncio.client
import websockets.asyncio.server
from websockets.asyncio.server import ServerConnection

from sattleme_checkout._nostr import Event, check_event, event_hash


def _matches(event: Event, filter_: dict[str, Any]) -> bool:
    if "ids" in filter_ and event.id not in filter_["ids"]:
        return False
    if "kinds" in filter_ and event.kind not in filter_["kinds"]:
        return False
    if "authors" in filter_ and event.pubkey not in filter_["authors"]:
        return False
    if "since" in filter_ and event.created_at < filter_["since"]:
        return False
    if "until" in filter_ and event.created_at > filter_["until"]:
        return False
    for name, values in filter_.items():
        if name.startswith("#") and len(name) == 2:
            tagged = {tag[1] for tag in event.tags if len(tag) >= 2 and tag[0] == name[1]}
            if not tagged & set(values):
                return False
    return True


@dataclass
class Relay:
    """One relay. Its behavior can change while a test runs."""

    name: str
    events: dict[tuple[Any, ...], Event | dict[str, Any]] = field(default_factory=dict)
    refuse: bool = False
    require_auth: bool = False
    # NIP-42, stricter: an event is taken only from its author, authenticated.
    auth_publisher: bool = False
    cap: int | None = None
    mute: bool = False
    delay: float = 0.0
    down: bool = False
    received: list[dict[str, Any]] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    # Every message the relay was sent, in order, as it came (answered or not: a mute relay's too),
    # and of every one it sent its first two members (its kind and subscription, or its event's id:
    # never an event, which a hostile relay's tests would keep in memory).
    messages: list[Any] = field(default_factory=list)
    sent: list[Any] = field(default_factory=list)
    authenticated: list[str] = field(default_factory=list)
    connections: int = 0
    on_event: Callable[[Event], bool] | None = None
    # Queries naming one of these authors are closed (CLOSED, "blocked:"), and those this says.
    closed_authors: set[str] = field(default_factory=set)
    closes: Callable[[dict[str, Any]], bool] | None = None
    # A hostile relay: values made from each query's filter, sent with what it holds.
    forge: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None
    # A relay that ignores a filter's `limit`, and answers oldest first.
    ignore_limit: bool = False
    # A relay that hides what it holds: the events it never sends (it takes and keeps them).
    hides: Callable[[Event], bool] | None = None
    # Answered late: each REQ whose filter this says is answered `late_by` seconds later (its
    # events, or its CLOSED: a relay that asks for authentication late).
    late: Callable[[dict[str, Any]], bool] | None = None
    late_by: float = 0.0
    # Never answered: each REQ whose filter this says (no event, no EOSE; the others are).
    stalls: Callable[[dict[str, Any]], bool] | None = None
    # Never answers an authentication (no OK); the ids of the AUTH events it was sent.
    mute_auth: bool = False
    auth_events: list[str] = field(default_factory=list)

    def store(self, event: Event) -> None:
        if 20000 <= event.kind < 30000:
            return
        address: tuple[Any, ...]
        if 30000 <= event.kind < 40000:
            address = (event.kind, event.pubkey, event.tag("d") or "")
        else:
            address = ("id", event.id)
        current = self.events.get(address)
        if not isinstance(current, Event) or event.replaces(current):
            self.events[address] = event

    def hold(self, event: Event) -> None:
        """Store an event as it is, beside the others of its address: a relay that keeps several
        records of one address (a vector's, so that the reader's own choice of the newest runs)."""
        self.events[("held", event.id)] = event

    def put(self, value: dict[str, Any]) -> None:
        """Store an event as it is given (a vector, a forged one): checked only for its shape."""
        event = check_event(value)
        if event is not None:
            self.store(event)
        else:
            self.events[("raw", len(self.events))] = value

    def query(self, filter_: dict[str, Any]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for event in self.events.values():
            if isinstance(event, Event):
                if _matches(event, filter_) and not (self.hides is not None and self.hides(event)):
                    found.append(event.to_json())
            else:
                found.append(event)  # a raw value: sent to every query, as a hostile relay would
        if self.forge is not None:
            found.extend(self.forge(filter_))

        def order(e: dict[str, Any]) -> tuple[int, str]:
            created_at = e.get("created_at")
            return (-created_at if isinstance(created_at, int) else 0, str(e.get("id")))

        found.sort(key=order)
        limit = filter_.get("limit")
        if self.ignore_limit:
            found.reverse()
        elif isinstance(limit, int):
            found = found[:limit]
        if self.cap is not None:
            found = found[: self.cap]
        return found

    async def _send(self, socket: ServerConnection, message: list[Any]) -> None:
        self.sent.append(message[:2])
        await socket.send(json.dumps(message))

    async def handle(self, socket: ServerConnection) -> None:
        self.connections += 1
        challenge = secrets.token_hex(16)
        authed: set[str] = set()
        if self.require_auth or self.auth_publisher:
            await self._send(socket, ["AUTH", challenge])
        with contextlib.suppress(websockets.ConnectionClosed):
            async for raw in socket:
                message = json.loads(raw)
                self.messages.append(message)
                if self.mute:
                    continue
                if self.delay:
                    await asyncio.sleep(self.delay)
                await self._answer(socket, message, challenge, authed)

    async def _answer(
        self, socket: ServerConnection, message: list[Any], challenge: str, authed: set[str]
    ) -> None:
        kind = message[0]
        if kind == "REQ":
            subscription, filter_ = message[1], message[2]
            self.requests.append(filter_)
            if self.late is not None and self.late(filter_):
                await asyncio.sleep(self.late_by)
            if self.require_auth and not authed:
                await self._send(socket, ["CLOSED", subscription, "auth-required: sign in"])
                return
            closed = self.closes is not None and self.closes(filter_)
            if closed or set(filter_.get("authors", ())) & self.closed_authors:
                await self._send(socket, ["CLOSED", subscription, "blocked: not here"])
                return
            if self.stalls is not None and self.stalls(filter_):
                return
            for found in self.query(filter_):
                await self._send(socket, ["EVENT", subscription, found])
            await self._send(socket, ["EOSE", subscription])
        elif kind == "AUTH":
            # NIP-42: the client's answer to the challenge, `["AUTH", <signed event>]`.
            answer: Any = message[1] if len(message) >= 2 else None
            auth = check_event(answer)
            auth_id = answer.get("id", "") if isinstance(answer, dict) else ""
            self.auth_events.append(auth_id)
            if self.mute_auth:
                return
            ok = (
                auth is not None
                and auth.kind == 22242
                and auth.tag("challenge") == challenge
                and auth.tag("relay") == f"wss://{self.name}"
            )
            if auth is not None and ok:
                authed.add(auth.pubkey)
                self.authenticated.append(auth.pubkey)
            await self._send(socket, ["OK", auth_id, ok, "" if ok else "auth failed"])
        elif kind == "EVENT":
            value: Any = message[1]
            checked = check_event(value)
            event_id = value.get("id", "") if isinstance(value, dict) else ""
            if checked is None:
                await self._send(socket, ["OK", event_id, False, "invalid: bad event"])
                return
            event: Event = checked
            if event.kind == 22242:
                # An authentication goes in an AUTH message, never published (NIP-42).
                await self._send(
                    socket, ["OK", event.id, False, "invalid: authentication is an AUTH message"]
                )
                return
            if (self.require_auth and not authed) or (
                self.auth_publisher and event.pubkey not in authed
            ):
                await self._send(socket, ["OK", event.id, False, "auth-required: sign in"])
                return
            accepted = not self.refuse and (self.on_event is None or self.on_event(event))
            if accepted:
                self.received.append(value)
                self.store(event)
            await self._send(socket, ["OK", event.id, accepted, "" if accepted else "blocked: no"])
        elif kind == "CLOSE":
            return


class Relays:
    """Local relays, each on its own port, reached through `connect` by their wss:// URL."""

    def __init__(self) -> None:
        self.relays: dict[str, Relay] = {}
        self._servers: dict[str, websockets.asyncio.server.Server] = {}
        self._ports: dict[str, int] = {}

    async def start(self, *names: str) -> list[str]:
        for name in names:
            relay = Relay(name)
            server = await websockets.asyncio.server.serve(relay.handle, "127.0.0.1", 0)
            self.relays[name] = relay
            self._servers[name] = server
            self._ports[name] = server.sockets[0].getsockname()[1]
        return [f"wss://{name}" for name in names]

    def __getitem__(self, name: str) -> Relay:
        return self.relays[name.removeprefix("wss://")]

    async def connect(self, url: str) -> Any:
        name = url.removeprefix("wss://")
        relay = self.relays.get(name)
        if relay is None or relay.down:
            raise ConnectionRefusedError(url)
        # As the package's own connector: no compression.
        return await websockets.asyncio.client.connect(
            f"ws://127.0.0.1:{self._ports[name]}",
            max_size=1 << 20,
            open_timeout=None,
            compression=None,
        )

    async def close(self) -> None:
        for server in self._servers.values():
            server.close()
            await server.wait_closed()


class Copies:
    """A hostile relay's socket (a `WebSocketConnection`): each query answered with the genuine
    event first when `genuine`, then `count` copies of it, each under an id of its own (a large
    tag added, the id recomputed, its signature left as it is), each made as it is sent, then
    EOSE."""

    def __init__(self, event: Event, count: int, size: int, *, genuine: bool = False) -> None:
        self.event = event.to_json()
        self.count = count
        self.size = size
        self.genuine = genuine
        self.sent = 0
        self._inbox: asyncio.Queue[tuple[str, int]] = asyncio.Queue()

    async def send(self, message: str) -> None:
        value = json.loads(message)
        if value[0] == "REQ":
            if self.genuine:
                self._inbox.put_nowait((value[1], -2))
            for _ in range(self.count):
                self._inbox.put_nowait((value[1], self.sent))
                self.sent += 1
            self._inbox.put_nowait((value[1], -1))

    async def recv(self) -> str:
        subscription, index = await self._inbox.get()
        if index == -1:
            return json.dumps(["EOSE", subscription])
        event: Any = self.event
        if index == -2:
            return json.dumps(["EVENT", subscription, event])
        tags = [*event["tags"], ["x", f"{index:08d}" + "y" * self.size]]
        digest = event_hash(
            event["pubkey"], event["created_at"], event["kind"], tags, event["content"]
        )
        assert digest is not None
        return json.dumps(["EVENT", subscription, {**event, "tags": tags, "id": digest.hex()}])

    async def close(self) -> None:
        return None
