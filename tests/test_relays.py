"""The relay client (SPEC.md §13) against local relays."""

from __future__ import annotations

import asyncio
import dataclasses
import http
import json
import secrets
import time
import tracemalloc
from collections.abc import Callable
from typing import Any

import pytest
import websockets.asyncio.server

from fakes.relay import Relays
from sattleme_checkout import _json
from sattleme_checkout._crypto import Secret
from sattleme_checkout._nostr import Event, sign_event
from sattleme_checkout._records import Opened, open_record, seal
from sattleme_checkout._relay import (
    Connection,
    RelayPool,
    WebSocketConnection,
    _RelayRead,
    default_connect,
    fresh_key,
    majority,
)
from sattleme_checkout.errors import NetworkError


def _now() -> int:
    return int(time.time())


# A key of this run alone: no key but the vectors' is ever written in the repository.
KEY = fresh_key(secrets.token_bytes)


def _event(created_at: int, d: str = "a", key: Secret = KEY) -> Event:
    return sign_event(
        key, created_at=created_at, kind=30078, tags=[["d", d]], content="x", aux=bytes(32)
    )


def _all(value: object) -> object:
    """Keeps every event a relay sends (once per id)."""
    return value


async def _pool(relays: Relays, timeout_ms: int = 2000) -> RelayPool:
    return RelayPool(
        connect=relays.connect, timeout_ms=timeout_ms, random=secrets.token_bytes, now=_now
    )


def test_majority() -> None:
    assert [majority(n) for n in range(1, 8)] == [1, 2, 2, 3, 3, 4, 4]


async def test_a_read_goes_on_at_a_majority_and_a_mute_relay_slows_nothing() -> None:
    relays = Relays()
    urls = await relays.start("a.example", "b.example", "c.example")
    try:
        for url in urls:
            relays[url].store(_event(100))
        relays[urls[2]].mute = True
        async with await _pool(relays, timeout_ms=5000) as pool:
            started = time.monotonic()
            answers = await pool.read(urls, {"kinds": [30078]}, _all)
            assert time.monotonic() - started < 2
        assert set(answers) == set(urls[:2])
    finally:
        await relays.close()


async def test_fewer_than_a_majority_is_no_answer() -> None:
    relays = Relays()
    urls = await relays.start("a.example", "b.example", "c.example")
    try:
        relays[urls[1]].down = True
        relays[urls[2]].mute = True
        async with await _pool(relays, timeout_ms=300) as pool:
            with pytest.raises(NetworkError):
                await pool.read(urls, {"kinds": [30078]}, _all)
    finally:
        await relays.close()


async def test_a_late_relay_past_its_timeout_fails() -> None:
    relays = Relays()
    urls = await relays.start("a.example")
    try:
        relays[urls[0]].delay = 1.0
        async with await _pool(relays, timeout_ms=200) as pool:
            with pytest.raises(NetworkError):
                await pool.read(urls, {"kinds": [30078]}, _all)
        relays[urls[0]].delay = 0.1
        async with await _pool(relays, timeout_ms=2000) as pool:
            assert await pool.read(urls, {"kinds": [30078]}, _all) == {urls[0]: []}
    finally:
        await relays.close()


async def test_paged_reads_follow_until_and_keep_each_event_once() -> None:
    relays = Relays()
    urls = await relays.start("a.example")
    try:
        relay = relays[urls[0]]
        relay.cap = 2
        for index in range(7):
            relay.store(_event(1000 + index, d=f"d{index}"))
        # Two events of the same date across a page boundary.
        relay.store(_event(1003, d="same"))
        async with await _pool(relays) as pool:
            answers = await pool.read(urls, {"kinds": [30078]}, _all, paged=True)
        ids = [value["id"] for value in answers[urls[0]]]  # type: ignore[index]
        assert len(ids) == len(set(ids)) == 8
        assert relay.requests[0] == {"kinds": [30078]}
        assert all("until" in request for request in relay.requests[1:])
        assert len(relay.requests) <= 20
    finally:
        await relays.close()


async def test_paged_reads_stop_at_twenty_pages() -> None:
    relays = Relays()
    urls = await relays.start("a.example")
    try:
        relay = relays[urls[0]]
        relay.cap = 1
        for index in range(30):
            relay.store(_event(1000 + index, d=f"d{index}"))
        async with await _pool(relays) as pool:
            answers = await pool.read(urls, {"kinds": [30078]}, _all, paged=True)
        assert len(relay.requests) == 20
        # until = the oldest date of a page with a new event, minus 1 after a page without one:
        # with one event a page, each new event takes two pages.
        assert len(answers[urls[0]]) == 10
    finally:
        await relays.close()


class _Flood:
    """A hostile relay's socket: each query answered with `count` events of `size` characters
    each, every one with an id of its own, then EOSE; each message made only as it is received,
    so that the test holds none of them."""

    def __init__(self, count: int, size: int) -> None:
        self.count = count
        self.size = size
        self.queries = 0
        self._sent = 0
        self._inbox: asyncio.Queue[tuple[str, int]] = asyncio.Queue()

    async def send(self, message: str) -> None:
        value = json.loads(message)
        if value[0] == "REQ":
            self.queries += 1
            for _ in range(self.count):
                self._inbox.put_nowait((value[1], self._sent))
                self._sent += 1
            self._inbox.put_nowait((value[1], -1))

    async def recv(self) -> str:
        subscription, index = await self._inbox.get()
        if index < 0:
            return json.dumps(["EOSE", subscription])
        event = {"id": f"{index:064x}", "created_at": 1000, "content": "x" * self.size}
        return json.dumps(["EVENT", subscription, event])

    async def close(self) -> None:
        return None


def _flooded(socket: _Flood) -> RelayPool:
    async def connect(_url: str) -> _Flood:
        return socket

    return RelayPool(connect=connect, timeout_ms=30_000, random=secrets.token_bytes, now=_now)


@pytest.mark.parametrize(
    ("read", "count", "size"),
    [("one query", 120, 1_000_000), ("paged", 6, 1_000_000), ("complete", 100, 60_000)],
    ids=["one query", "paged", "complete"],
)
async def test_what_a_relay_sends_never_piles_up(read: str, count: int, size: int) -> None:
    """SPEC.md §13, Limits: a read keeps of each event only what it needs, as the event comes
    (its id and date, and what the read keeps of it): over 100 MiB of events, a few MiB at
    most in memory at once, and the relay's answer is never cut at a size."""
    socket = _Flood(count, size)
    tracemalloc.start()
    try:
        async with _flooded(socket) as pool:
            if read == "complete":
                found = await pool.read_complete(["wss://a.example"], {"kinds": [30078]}, _id)
                # 20 full pages, each of new events of one second: incomplete, never failed.
                assert found.answered and not found.complete
                answers = found.answers
            else:
                answers = await pool.read(
                    ["wss://a.example"], {"kinds": [30078]}, _id, paged=read == "paged"
                )
        _now_held, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    pages = 1 if read == "one query" else 20
    assert socket.queries == pages
    assert count * size * pages > 100 << 20
    assert peak < 16 << 20
    assert len(answers["wss://a.example"]) == count * pages


def _id(value: object) -> str | None:
    """Keeps an event's id alone."""
    event = _json.as_object(value)
    return None if event is None else str(event["id"])


class _Copies:
    """A relay's socket that answers each query (its `until` and `limit` honored) with `events`,
    each made as it is sent: dicts sent as they are."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.queries: list[dict[str, Any]] = []
        self._inbox: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, message: str) -> None:
        value = json.loads(message)
        if value[0] != "REQ":
            return
        subscription, filter_ = value[1], value[2]
        self.queries.append(filter_)
        until = filter_.get("until")
        sent = [e for e in self.events if until is None or e["created_at"] <= until]
        for event in sent[: filter_.get("limit", len(sent))]:
            self._inbox.put_nowait(json.dumps(["EVENT", subscription, event]))
        self._inbox.put_nowait(json.dumps(["EOSE", subscription]))

    async def recv(self) -> str:
        return await self._inbox.get()

    async def close(self) -> None:
        return None


def _channel_like() -> tuple[Event, Callable[[object], object], list[int]]:
    """A sealed record, and what a read keeps of it (it opens), counting each opening."""
    key = Secret(secrets.token_bytes(32))
    record = seal(
        record_type="team-channel",
        value={"version": 1},
        signer=KEY,
        key=key,
        address="ab" * 32,
        created_at=1_791_378_000,
        random=secrets.token_bytes,
    )
    openings: list[int] = []

    def keep(value: object) -> object:
        openings.append(1)
        return open_record(
            value,
            record_type="team-channel",
            key=key,
            address="ab" * 32,
            author=record.pubkey,
            parse=lambda opened, _event: opened,
        )

    return record, keep, openings


async def _read_each_way(
    socket: _Copies, keep: Callable[[object], object]
) -> list[tuple[str, list[object], int]]:
    """A read of one query, a paged read and a complete read of one relay: what each kept, and
    how many queries it asked."""

    async def connect(_url: str) -> _Copies:
        return socket

    found: list[tuple[str, list[object], int]] = []
    for read in ("one query", "paged", "complete"):
        asked = len(socket.queries)
        async with RelayPool(
            connect=connect, timeout_ms=30_000, random=secrets.token_bytes, now=_now
        ) as pool:
            if read == "complete":
                answers = (await pool.read_complete([URL], {"kinds": [30078]}, keep)).answers
            else:
                answers = await pool.read([URL], {"kinds": [30078]}, keep, paged=read == "paged")
        found.append((read, answers[URL], len(socket.queries) - asked))
    return found


URL = "wss://copies.example"


async def test_a_relays_copies_of_one_event_are_read_once_without_what_it_added() -> None:
    """SPEC.md §13, Limits: of one relay's answer to a query, the first event of each id is the
    one read; a later one (a copy) only counts. Of an event kept, only its NIP-01 members: a
    relay may add any member to an event that checks out, its id and signature covering none.
    50 copies of a record, each with 100 000 characters added: one kept, exactly the record,
    one opening per read (a paged read asks their second again, and reads it no more)."""
    record, keep, openings = _channel_like()
    socket = _Copies([{**record.to_json(), "padding": "x" * 100_000} for _ in range(50)])
    for read, kept, _queries in await _read_each_way(socket, keep):
        [opened] = kept
        assert isinstance(opened, Opened), read
        assert opened.event == record
        assert opened.event.to_json() == record.to_json()
        assert [f.name for f in dataclasses.fields(opened.event)] == [
            "id", "pubkey", "created_at", "kind", "tags", "content", "sig"
        ]  # fmt: skip
    assert len(openings) == 3


async def test_a_later_event_of_an_id_is_never_opened_nor_checked() -> None:
    """A relay sends 50 forgeries under a record's id (its content changed: none checks out),
    then the record itself: the first event of that id is the one read, opened once per read;
    the others, the record included, only count toward the query's 1000 events."""
    record, keep, openings = _channel_like()
    forged = {**record.to_json(), "content": record.content[:-4] + "AAA="}
    socket = _Copies([forged] * 50 + [record.to_json()])
    for read, kept, _queries in await _read_each_way(socket, keep):
        assert kept == [], read
    assert len(openings) == 3


async def test_a_read_that_fails_on_an_event_fails_that_relay_alone() -> None:
    """What a read makes of an event failing (a bug, never the relay's doing) fails that relay's
    read, never the connection nor the other relays' reads."""
    relays = Relays()
    urls = await relays.start("a.example", "b.example", "c.example")
    try:
        for url in urls:
            relays[url].store(_event(100, d=url))

        def keep(value: object) -> object:
            event = _json.as_object(value)
            assert event is not None
            if event["tags"] == [["d", urls[0]]]:
                raise ValueError("a bug")
            return value

        async with await _pool(relays) as pool:
            answers = await pool.read(urls, {"kinds": [30078]}, keep)
            assert set(answers) == set(urls[1:])
            with pytest.raises(NetworkError):
                await pool.read(urls[:1], {"kinds": [30078]}, keep)
            # The connection still serves the call.
            assert await pool.read(urls[:1], {"kinds": [30078]}, _all) != {}
        assert relays[urls[0]].connections == 1
    finally:
        await relays.close()


async def test_a_relay_that_redirects_its_handshake_failed() -> None:
    """SPEC.md §13: the opening handshake follows no redirect, which would skip the checks of a
    relay's URL: a failed connection, the relay's failure for the call."""
    relays = Relays()
    urls = await relays.start("a.example", "b.example")
    reached: list[str] = []

    async def elsewhere(socket: websockets.asyncio.server.ServerConnection) -> None:
        reached.append("elsewhere")
        await socket.close()

    target = await websockets.asyncio.server.serve(elsewhere, "127.0.0.1", 0)
    target_port = target.sockets[0].getsockname()[1]

    def redirect(connection: Any, _request: Any) -> Any:
        reached.append("moved")
        response = connection.respond(http.HTTPStatus.FOUND, "")
        response.headers["Location"] = f"ws://127.0.0.1:{target_port}/"
        return response

    async def never(_socket: websockets.asyncio.server.ServerConnection) -> None:
        raise AssertionError("not a WebSocket")

    moved = await websockets.asyncio.server.serve(never, "127.0.0.1", 0, process_request=redirect)
    moved_url = "wss://moved.example"
    local = {url: f"ws://127.0.0.1:{relays._ports[url.removeprefix('wss://')]}" for url in urls}
    local[moved_url] = f"ws://127.0.0.1:{moved.sockets[0].getsockname()[1]}"

    async def connect(url: str) -> WebSocketConnection:
        # The package's own connector, to the local servers.
        return await default_connect(local[url])

    try:
        for url in urls:
            relays[url].store(_event(100))
        listed = [moved_url, *urls]
        async with RelayPool(
            connect=connect, timeout_ms=2000, random=secrets.token_bytes, now=_now
        ) as pool:
            answers = await pool.read(listed, {"kinds": [30078]}, _all)
            assert set(answers) == set(urls)
            # Failed for the call: never asked again.
            answers = await pool.read(listed, {"kinds": [30078]}, _all, every_relay=True)
            assert set(answers) == set(urls)
            with pytest.raises(NetworkError):
                await pool.read([moved_url], {"kinds": [30078]}, _all)
        # Its handshake asked once, the place it redirects to never.
        assert reached == ["moved"]
    finally:
        for server in (moved, target):
            server.close()
            await server.wait_closed()
        await relays.close()


async def test_a_thousand_events_fit_in_a_read() -> None:
    relays = Relays()
    urls = await relays.start("a.example")
    try:
        content = "A" * 2400  # a record's sealed content, as large as this package's
        for index in range(1000):
            relays[urls[0]].store(
                sign_event(
                    KEY,
                    created_at=1000 + index,
                    kind=30078,
                    tags=[["d", f"{index:064x}"], ["expiration", "2000000000"]],
                    content=content,
                    aux=bytes(32),
                )
            )
        async with await _pool(relays, timeout_ms=10_000) as pool:
            answers = await pool.read(urls, {"kinds": [30078]}, _all, paged=True)
        assert len(answers[urls[0]]) == 1000
    finally:
        await relays.close()


async def test_one_connection_per_relay_per_call() -> None:
    relays = Relays()
    urls = await relays.start("a.example", "b.example")
    try:
        async with await _pool(relays) as pool:
            await pool.read(urls, {"kinds": [30078]}, _all)
            await pool.read(urls, {"kinds": [1]}, _all)
            await pool.publish(urls, _event(5), key=KEY)
        assert [relays[url].connections for url in urls] == [1, 1]
    finally:
        await relays.close()


async def test_a_publication_succeeds_on_a_majority_and_fails_without_one() -> None:
    relays = Relays()
    urls = await relays.start("a.example", "b.example", "c.example")
    try:
        relays[urls[0]].refuse = True
        async with await _pool(relays) as pool:
            took = await pool.publish(urls, _event(10), key=KEY)
        assert sorted(took) == sorted(urls[1:])
        relays[urls[1]].refuse = True
        async with await _pool(relays) as pool:
            with pytest.raises(NetworkError):
                await pool.publish(urls, _event(11), key=KEY)
    finally:
        await relays.close()


async def test_nip42_authentication_lifts_a_refusal() -> None:
    relays = Relays()
    urls = await relays.start("a.example")
    try:
        relays[urls[0]].require_auth = True
        relays[urls[0]].store(_event(100))
        async with await _pool(relays) as pool:
            answers = await pool.read(urls, {"kinds": [30078]}, _all, key=KEY)
            assert len(answers[urls[0]]) == 1
            await pool.publish(urls, _event(101, d="b"), key=KEY)
        # Once per key and challenge on a connection: the publication needed no second one.
        assert relays[urls[0]].authenticated == [_event(1).pubkey]
        async with await _pool(relays) as pool:
            # A read without a key authenticates with a key of that read alone.
            await pool.read(urls, {"kinds": [30078]}, _all)
        assert len(relays[urls[0]].authenticated) == 2
        assert relays[urls[0]].authenticated[1] != _event(1).pubkey
    finally:
        await relays.close()


async def test_a_relay_whose_authentication_runs_out_of_time_failed_for_the_call() -> None:
    """SPEC.md §13: the wait for the OK of a NIP-42 authentication is part of the request's time.
    A relay asks for authentication and never answers it: the request runs out of time there,
    and that relay counts as failed for the rest of the call, never as a refusal asked again: a
    read on every relay goes on without it, at once, and nothing more is asked of it."""
    relays = Relays()
    urls = await relays.start("a.example", "b.example", "c.example")
    try:
        mute = relays[urls[2]]
        mute.require_auth = True
        mute.mute_auth = True
        for url in urls:
            relays[url].store(_event(100))
        async with await _pool(relays, timeout_ms=3000) as pool:
            started = time.monotonic()
            with pytest.raises(NetworkError):
                await pool.read([urls[2]], {"kinds": [30078]}, _all, key=KEY)
            # Its timeout, the authentication's wait included: not sooner.
            assert time.monotonic() - started >= 3.0
            started = time.monotonic()
            answers = await pool.read(urls, {"kinds": [30078]}, _all, key=KEY, every_relay=True)
            # Not waited for (one timeout more): a relay that ran out of time failed at once.
            assert time.monotonic() - started < 2.5
            assert set(answers) == set(urls[:2])
            took = await pool.publish(urls, _event(101, d="b"), key=KEY)
            assert sorted(took) == sorted(urls[:2])
        # One query, one authentication: never asked again in the call.
        assert len(mute.requests) == 1
        assert len(mute.auth_events) == 1
        assert mute.authenticated == []
    finally:
        await relays.close()


async def test_a_cancelled_call_closes_its_connections() -> None:
    relays = Relays()
    urls = await relays.start("a.example")
    try:
        relays[urls[0]].mute = True
        pool = await _pool(relays, timeout_ms=10_000)

        async def read() -> None:
            async with pool:
                await pool.read(urls, {"kinds": [30078]}, _all)

        task = asyncio.ensure_future(read())
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool._connections == {}
    finally:
        await relays.close()


class _Recorded:
    """A connection whose messages sent are recorded: by whom, what, and whether the read the
    test runs had gone on (`state["went_on"]`) when it was sent."""

    def __init__(
        self, url: str, socket: Any, log: list[tuple[str, str, bool]], state: dict[str, bool]
    ) -> None:
        self._url, self._socket, self._log, self._state = url, socket, log, state

    async def send(self, message: str) -> None:
        self._log.append((self._url, json.loads(message)[0], self._state["went_on"]))
        await self._socket.send(message)

    async def recv(self) -> str | bytes:
        received: str | bytes = await self._socket.recv()
        return received

    async def close(self) -> None:
        await self._socket.close()


async def _until(condition: Callable[[], bool], seconds: float = 10.0) -> None:
    """Wait until `condition` holds, `seconds` at most."""
    async with asyncio.timeout(seconds):
        while not condition():  # noqa: ASYNC110 - polls what a local relay sent
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("every_relay", [False, True])
async def test_the_relays_a_read_dropped_ask_nothing_more(every_relay: bool) -> None:
    """SPEC.md §13, Reads: the read goes on as soon as a majority answered; the others are
    dropped then: their requests end, and they ask nothing more (no page, no authentication), nor
    draw random bytes for it (a subscription id, an authentication's). Of five relays, three
    answer a paged read at once; d answers its first page late, with an event that would page the
    read on; e asks for authentication late (its query CLOSED with `auth-required:`). Once the
    read went on, while the call goes on with its connections open and d's page and e's CLOSED
    come, the client sends nothing more, and its random source is not called. A read on every
    relay waits for them instead (one timeout at most): then d is asked its next pages and e
    authenticates, which a dropped relay never does."""
    relays = Relays()
    urls = await relays.start("a.example", "b.example", "c.example", "d.example", "e.example")
    try:
        for url in urls:
            relays[url].store(_event(100))
        late_page, late_auth = relays[urls[3]], relays[urls[4]]
        for relay in (late_page, late_auth):
            relay.late = lambda _filter: True
            relay.late_by = 0.5
        late_auth.require_auth = True
        drawn: list[int] = []
        sent: list[tuple[str, str, bool]] = []
        state = {"went_on": False}

        def random(size: int) -> bytes:
            drawn.append(size)
            return secrets.token_bytes(size)

        async def connect(url: str) -> WebSocketConnection:
            return _Recorded(url, await relays.connect(url), sent, state)

        pool = RelayPool(connect=connect, timeout_ms=5000, random=random, now=_now)
        async with pool:
            answers = await pool.read(
                urls, {"kinds": [30078]}, _all, key=KEY, paged=True, every_relay=every_relay
            )
            # The read went on: nothing of this step ran between its decision and here.
            state["went_on"] = True
            went_on = len(drawn)
            # d's first page and e's CLOSED come after the read went on (or, on every relay, were
            # waited for); then a while longer, the connections still open.
            await _until(lambda: any(message[0] == "EOSE" for message in late_page.sent))
            await _until(lambda: any(message[0] == "CLOSED" for message in late_auth.sent))
            await asyncio.sleep(0.3)
            assert [message for message in sent if message[2]] == []
            assert len(drawn) == went_on

        def to(url: str) -> list[str]:
            return [kind for to_url, kind, _after in sent if to_url == url]

        if every_relay:
            assert set(answers) == set(urls)
            # d paged on; e authenticated, and was asked again.
            assert to(urls[3])[:3] == ["REQ", "CLOSE", "REQ"]
            assert to(urls[4])[:3] == ["REQ", "AUTH", "REQ"]
        else:
            assert set(answers) == set(urls[:3])
            assert to(urls[3]) == ["REQ"]
            assert to(urls[4]) == ["REQ"]
    finally:
        await relays.close()


class _AsksAuthentication:
    """A relay's socket that answers a query with a challenge and `auth-required:`, and records
    what it is sent (`log`)."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self._inbox: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, message: str) -> None:
        value = json.loads(message)
        self._log.append(value[0])
        if value[0] == "REQ":
            self._inbox.put_nowait(json.dumps(["AUTH", "challenge"]))
            self._inbox.put_nowait(json.dumps(["CLOSED", value[1], "auth-required: sign in"]))

    async def recv(self) -> str:
        return await self._inbox.get()

    async def close(self) -> None:
        return None


async def test_a_request_dropped_once_it_signed_its_authentication_sends_nothing_more() -> None:
    """SPEC.md §13: a read's dropped relays ask nothing more, no authentication included. A
    request whose query a relay CLOSED with `auth-required:` signs its authentication in the step
    that reads the CLOSED; dropped right after that step (as a read that goes on in that turn of
    the event loop drops it), it sends nothing more: its authentication goes out in that step,
    from the request itself, or never."""
    log: list[str] = []
    request: asyncio.Future[Any] | None = None

    def dropped() -> None:
        log.append("dropped")
        assert request is not None
        request.cancel()

    def random(size: int) -> bytes:
        if size == 32:
            # The authentication's auxiliary bytes, drawn in the step that read the CLOSED: the
            # request is dropped as that step ends.
            asyncio.get_running_loop().call_soon(dropped)
        return secrets.token_bytes(size)

    connection = Connection("wss://a.example", _AsksAuthentication(log), random)
    request = asyncio.ensure_future(
        connection.query({"kinds": [30078]}, KEY, _now, _RelayRead(_all))
    )
    with pytest.raises(asyncio.CancelledError):
        await request
    # Whatever it left to run, run.
    await asyncio.sleep(0.1)
    await connection.close()
    assert log == ["REQ", "AUTH", "dropped"]


class _Authenticating:
    """A relay's socket that asks for authentication (NIP-42) until it got one, and records
    what it is sent (`log`); it answers an authentication once `answer` is set."""

    def __init__(self, log: list[str], answer: asyncio.Event) -> None:
        self._log = log
        self._answer = answer
        self._authenticated = False
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        self._later: set[asyncio.Task[None]] = set()

    async def send(self, message: str) -> None:
        value = json.loads(message)
        self._log.append(value[0])
        if value[0] == "REQ" and self._authenticated:
            self._inbox.put_nowait(json.dumps(["EOSE", value[1]]))
        elif value[0] == "REQ":
            self._inbox.put_nowait(json.dumps(["AUTH", "challenge"]))
            self._inbox.put_nowait(json.dumps(["CLOSED", value[1], "auth-required: sign in"]))
        elif value[0] == "AUTH":
            task = asyncio.ensure_future(self._ok(value[1]["id"]))
            self._later.add(task)
            task.add_done_callback(self._later.discard)

    async def _ok(self, event_id: str) -> None:
        await self._answer.wait()
        self._authenticated = True
        self._inbox.put_nowait(json.dumps(["OK", event_id, True, ""]))

    async def recv(self) -> str:
        return await self._inbox.get()

    async def close(self) -> None:
        return None


@pytest.mark.parametrize("first_dropped", [False, True])
async def test_requests_at_once_share_one_authentication(first_dropped: bool) -> None:
    """NIP-42, SPEC.md §13: one authentication per key and challenge per connection. Two queries
    at once, with one key, both refused with `auth-required:`: the first sends the
    authentication, the second waits for its `OK`, and both are asked again once it came; the
    first dropped meanwhile (a read gone on), the second still gets it."""
    log: list[str] = []
    answer = asyncio.Event()
    connection = Connection("wss://a.example", _Authenticating(log, answer), secrets.token_bytes)
    first, second = (
        asyncio.ensure_future(connection.query({"kinds": [kind]}, KEY, _now, _RelayRead(_all)))
        for kind in (1, 2)
    )
    await _until(lambda: log.count("REQ") == 2 and "AUTH" in log)
    await asyncio.sleep(0.05)
    if first_dropped:
        first.cancel()
    answer.set()
    pages = await asyncio.gather(first, second, return_exceptions=True)
    await connection.close()
    asked = [kind for kind in log if kind != "CLOSE"]
    if first_dropped:
        assert isinstance(pages[0], asyncio.CancelledError)
        assert asked == ["REQ", "REQ", "AUTH", "REQ"]
    else:
        assert asked == ["REQ", "REQ", "AUTH", "REQ", "REQ"]
    assert pages[1].count == 0  # type: ignore[union-attr]
