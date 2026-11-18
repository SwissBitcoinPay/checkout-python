"""The relays (SPEC.md §13): NIP-01 and NIP-42 over WebSocket.

One `RelayPool` per call of the client: one connection per relay, opened at its first request
and closed when the call is done. Reads ask every relay of a list at once and go on as soon as a
majority answered; publications succeed once a majority took the event, and fail as soon as a
majority no longer can. A relay past the majority never slows a call: its requests are dropped.

Two reads wait for more (SPEC.md §13): a complete read (the leases of deposit addresses) pages
until a page is not full, and goes on once a majority read completely; a read on every relay (a
lease's confirmation, the witnesses read again, the choice among an order's invoices) waits for
every relay, up to one timeout after a majority answered, so that a relay that hides a record
cannot hide it while one that holds it answers in time; a relay still reading when it goes on has
not answered (its pages are kept). A relay that ran out of time or lost its connection earlier in
the call is not waited for again.

A read keeps of each event only what it needs, as the event comes (SPEC.md §13, Limits): its id
and date, to page the read and count each event once, and what the read's `keep` makes of it (the
record it opens to: of its event, the NIP-01 members alone); anything else is dropped there. Of a
relay's answer, the first event of each id is the one read, a later one only counted. So what a
relay sends never piles up, whatever its size, and no answer is ever cut at a size.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, Protocol, TypeVar

import websockets.asyncio.client
from websockets.frames import CloseCode
from websockets.protocol import State

from . import _json
from ._constants import (
    AUTH_KIND,
    COMPLETE_READ_LIMIT,
    EVENTS_PER_QUERY,
    HTTP_BODY_MAX_BYTES,
    HTTP_HEAD_MAX_BYTES,
    MESSAGE_MAX_BYTES,
    PAGES_MAX,
)
from ._crypto import Secret, is_secret_key, sha256, xonly_public_key
from ._http import USER_AGENT
from ._nostr import Event, sign_event
from ._records import RandomBytes
from .errors import NetworkError

_CLOSE_SECONDS = 1.0

T = TypeVar("T")

Keep = Callable[[object], T | None]
"""What a read keeps of an event a relay sent, as it comes: what the event opens to (SPEC.md §12),
or None to drop it. It runs once per event id of a relay's read: on its first event of that id."""


class WebSocketConnection(Protocol):
    """A WebSocket connection: what the package needs of one."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


WebSocketConnect = Callable[[str], Awaitable[WebSocketConnection]]
"""Opens a WebSocket connection to a URL. It SHOULD cap a message at 1 MiB."""


class _DefaultConnection(websockets.asyncio.client.ClientConnection):
    """The default client's connection: `websockets`' own, bounded twice.

    Its answer to its opening handshake is bounded as an HTTP answer's head (SPEC.md §15):
    `websockets` bounds its status line and headers (128 lines of 8 KiB), and the body of a
    refusal (1 MiB), not the count of that body's size lines when it is chunked (one per byte,
    each up to 8 KiB). What comes before the handshake ended counts toward 10 MiB besides that
    body; past it, the connection is dropped: no answer.

    Its close ends it, whatever the other end does (SPEC.md §13: nothing a call started runs on
    once it returned). `websockets` waits for the closing handshake up to its close timeout, then
    aborts the TCP connection; but a close cancelled first (the bound a call's pool puts on it)
    left the connection open, and `websockets`' keepalive task running, until its next ping. The
    connection is now aborted once the closing handshake ended or was cancelled, and `close`
    returns once `websockets` saw it end and its keepalive task ended."""

    _handshake_bytes = 0

    def data_received(self, data: bytes) -> None:
        if self.protocol.state is State.CONNECTING:
            self._handshake_bytes += len(data)
            if self._handshake_bytes > HTTP_HEAD_MAX_BYTES + HTTP_BODY_MAX_BYTES:
                self.transport.abort()
                return
        super().data_received(data)

    async def close(self, code: int = CloseCode.NORMAL_CLOSURE, reason: str = "") -> None:
        # `websockets` forgets its keepalive task once the connection ended.
        keepalive = self.keepalive_task
        try:
            await super().close(code, reason)
        finally:
            self.transport.abort()
            await asyncio.shield(self.connection_lost_waiter)
            if keepalive is not None:
                await asyncio.wait([keepalive])


class _Connect(websockets.asyncio.client.connect):
    """`websockets`' opening handshake, without following a redirect: every URL is checked
    before it is asked, and a redirect would skip the check (as SPEC.md §15 says of HTTP)."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


async def default_connect(url: str) -> WebSocketConnection:
    """`websockets`, a message capped at 1 MiB, no compression, no redirect followed, the answer
    to its handshake bounded, no proxy (inject a connector to use one)."""
    connection: WebSocketConnection = await _Connect(
        url,
        max_size=MESSAGE_MAX_BYTES,
        # Not offered (permessage-deflate): `websockets` unpacks all that one read of the socket
        # brings before it pauses, so a relay's few compressed bytes could become many messages
        # of 1 MiB held at once. Sealed records, encrypted, do not compress anyway.
        compression=None,
        open_timeout=None,
        close_timeout=_CLOSE_SECONDS,
        proxy=None,
        # The one of every HTTP request (SPEC.md §15): it names no language nor version.
        user_agent_header=USER_AGENT,
        create_connection=_DefaultConnection,
    )
    return connection


def _retrieve(task: asyncio.Future[Any]) -> None:
    """Marks a background task's outcome as seen: its awaiters, if any, see it too."""
    if not task.cancelled():
        task.exception()


def majority(count: int) -> int:
    return count // 2 + 1


class RelayError(Exception):
    """A relay did not answer usably (internal: a read or publication counts it as failed)."""


class ConnectionLostError(RelayError):
    """A relay's connection could not open, or ended: the relay failed for the rest of the call."""


def _name(event_id: str) -> str | bytes:
    """An event id as a read remembers it: itself, of 64 characters at most (a hex32); a longer
    one by its SHA-256, so that the ids a relay makes long never pile up either."""
    if len(event_id) <= 64:
        return event_id
    return sha256(event_id.encode("utf-8", "surrogatepass"))


class _RelayRead(Generic[T]):
    """One relay's read: what its pages that ended kept, and the event ids they held. `dated`, a
    paged read's (SPEC.md §13, Paged reads): only an event with a string id and a safe integer
    `created_at` counts, whatever else it holds; one without them neither pages the read nor is
    kept. A complete read's pages count every event with a string id, dated or not. Of each id,
    the first event of the read is the one kept (SPEC.md §13: once per id, the first of the
    read)."""

    __slots__ = ("dated", "ids", "keep", "kept", "pages")

    def __init__(self, keep: Keep[T], *, dated: bool = False) -> None:
        self.keep = keep
        self.dated = dated
        self.kept: list[T] = []
        self.pages = 0
        self.ids: set[str | bytes] = set()

    def page(self) -> _Page[T]:
        return _Page(self)


class _Page(Generic[T]):
    """One page of a relay's read, as its events come: how many came, the oldest safe integer
    `created_at` of those with a string id, whether one of those that count is new to the read,
    and what it keeps. Of the relay's answer, the first event of each id is the one read: a later
    one of that id (a copy, or a forgery: an id is its event's hash) only counts toward the
    query's 1000 events, nothing of it kept, opened or checked (SPEC.md §13, Limits). None of it
    counts in the read until the page ended (`end`): a page that fails, or that a read on every
    relay cut, adds nothing."""

    __slots__ = ("_ids", "_kept", "_read", "_seen", "count", "new", "oldest")

    def __init__(self, read: _RelayRead[T]) -> None:
        self._read = read
        self.count = 0
        self.oldest: int | None = None
        self.new = False
        # The ids of this answer's events so far, and those of them new to the read.
        self._seen: set[str | bytes] = set()
        self._ids: set[str | bytes] = set()
        self._kept: list[T] = []

    def add(self, value: object) -> None:
        """An event the relay sent: counted, and dropped once the read took what it needs."""
        self.count += 1
        event = _json.as_object(value)
        event_id = event.get("id") if event is not None else None
        if event is None or not isinstance(event_id, str):
            # No event of the read: nothing opens without an id.
            return
        created_at = _json.as_safe_integer(event.get("created_at"))
        if created_at is not None and (self.oldest is None or created_at < self.oldest):
            self.oldest = created_at
        name = _name(event_id)
        if name in self._seen:
            # A later event of an id this answer sent: counted, never read.
            return
        self._seen.add(name)
        if created_at is None and self._read.dated:
            # A paged read: it neither pages the read nor is kept.
            return
        if name in self._read.ids:
            # Of an earlier page: the first event of the read stands.
            return
        self.new = True
        self._ids.add(name)
        kept = self._read.keep(value)
        if kept is not None:
            self._kept.append(kept)

    def end(self) -> None:
        """The page ended: what it held counts in the read."""
        read = self._read
        read.pages += 1
        read.ids |= self._ids
        read.kept.extend(self._kept)


@dataclass
class _Subscription:
    page: _Page[Any]
    done: asyncio.Future[str | None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


class Connection:
    """One relay's connection, shared by every request of a call."""

    def __init__(self, url: str, socket: WebSocketConnection, random: RandomBytes) -> None:
        self.url = url
        self._socket = socket
        self._random = random
        self._subscriptions: dict[str, _Subscription] = {}
        self._oks: dict[str, asyncio.Future[tuple[bool, str]]] = {}
        self._challenge: str | None = None
        # The `OK` of each authentication sent, by key and challenge (NIP-42: once each).
        self._authentications: dict[tuple[str, str], asyncio.Future[tuple[bool, str]]] = {}
        self._closed: BaseException | None = None
        self._reader = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        try:
            while True:
                message = await self._socket.recv()
                if isinstance(message, str):
                    self._dispatch(message)
        except asyncio.CancelledError:
            self._fail(ConnectionLostError(f"{self.url}: closed"))
            raise
        except Exception as error:  # noqa: BLE001 - any end of the socket ends its requests
            self._fail(
                ConnectionLostError(f"{self.url}: the connection ended ({type(error).__name__})")
            )

    def _fail(self, error: BaseException) -> None:
        self._closed = error
        for subscription in self._subscriptions.values():
            if not subscription.done.done():
                subscription.done.set_exception(error)
        for waiter in self._oks.values():
            if not waiter.done():
                waiter.set_exception(error)

    def _dispatch(self, text: str) -> None:
        message = _json.parse_text(text)
        if not isinstance(message, list) or not message or not isinstance(message[0], str):
            return
        kind = message[0]
        if kind == "EVENT" and len(message) >= 3 and isinstance(message[1], str):
            subscription = self._subscriptions.get(message[1])
            if subscription is not None and not subscription.done.done():
                try:
                    subscription.page.add(message[2])
                except Exception as error:  # noqa: BLE001 - the query's failure, not the socket's
                    subscription.done.set_exception(error)
                    return
                if subscription.page.count >= EVENTS_PER_QUERY:
                    subscription.done.set_result(None)
        elif kind == "EOSE" and len(message) >= 2 and isinstance(message[1], str):
            subscription = self._subscriptions.get(message[1])
            if subscription is not None and not subscription.done.done():
                subscription.done.set_result(None)
        elif kind == "CLOSED" and len(message) >= 2 and isinstance(message[1], str):
            subscription = self._subscriptions.get(message[1])
            if subscription is not None and not subscription.done.done():
                reason = message[2] if len(message) >= 3 and isinstance(message[2], str) else ""
                subscription.done.set_result(reason)
        elif kind == "OK" and len(message) >= 3 and isinstance(message[1], str):
            waiter = self._oks.get(message[1])
            if waiter is not None and not waiter.done() and isinstance(message[2], bool):
                reason = message[3] if len(message) >= 4 and isinstance(message[3], str) else ""
                waiter.set_result((message[2], reason))
        elif kind == "AUTH" and len(message) >= 2 and isinstance(message[1], str):
            self._challenge = message[1]

    async def _send(self, message: list[Any]) -> None:
        if self._closed is not None:
            raise self._closed
        try:
            await self._socket.send(_json.dumps(message))
        except Exception as error:
            raise ConnectionLostError(f"{self.url}: could not send") from error

    async def query(
        self, filter_: Mapping[str, Any], key: Secret, now: Callable[[], int], read: _RelayRead[Any]
    ) -> _Page[Any]:
        """One query: its page, once it ended (EOSE, or its 1000th event); RelayError otherwise.
        Its events go to a page of `read` as they come, which counts in `read` once it ended."""
        for attempt in range(2):
            subscription_id = self._random(8).hex()
            subscription = _Subscription(read.page())
            self._subscriptions[subscription_id] = subscription
            try:
                await self._send(["REQ", subscription_id, dict(filter_)])
                try:
                    closed = await subscription.done
                except ConnectionLostError:
                    raise
                except Exception:
                    # What the read made of an event failed: this relay's query only.
                    with contextlib.suppress(RelayError):
                        await self._send(["CLOSE", subscription_id])
                    raise
                if closed is None:
                    with contextlib.suppress(RelayError):
                        await self._send(["CLOSE", subscription_id])
                    subscription.page.end()
                    return subscription.page
            finally:
                del self._subscriptions[subscription_id]
            retry = attempt == 0 and closed.startswith("auth-required:")
            if retry and await self.authenticate(key, now):
                continue
            raise RelayError(f"{self.url} closed the query")
        raise RelayError(f"{self.url} closed the query")

    async def publish(self, event: Event, key: Secret, now: Callable[[], int]) -> bool:
        """Send an event: whether the relay took it (`OK` true)."""
        for attempt in range(2):
            accepted, reason = await self._ok(event)
            if accepted:
                return True
            retry = attempt == 0 and reason.startswith("auth-required:")
            if retry and await self.authenticate(key, now):
                continue
            return False
        return False

    async def _ok(self, event: Event) -> tuple[bool, str]:
        """Publish `event` (an `EVENT` message): the relay's `OK`."""
        waiter: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
        self._oks[event.id] = waiter
        try:
            await self._send(["EVENT", event.to_json()])
            return await waiter
        finally:
            if self._oks.get(event.id) is waiter:
                del self._oks[event.id]

    async def authenticate(self, key: Secret, now: Callable[[], int]) -> bool:
        """NIP-42 with `key` and the last challenge: once per key and challenge, its `OK` shared
        by every request that waits for it. The request that needs it first signs it and sends it
        itself, in that same step, never from a task of its own: a request dropped before (a
        read gone on, SPEC.md §13) sends nothing, and one dropped after leaves the `OK` to the
        others."""
        challenge = self._challenge
        if challenge is None:
            return False
        pubkey = xonly_public_key(key).hex()
        answer = self._authentications.get((pubkey, challenge))
        if answer is None:
            event = sign_event(
                key,
                created_at=now(),
                kind=AUTH_KIND,
                tags=[["relay", self.url], ["challenge", challenge]],
                content="",
                aux=self._random(32),
            )
            answer = asyncio.get_running_loop().create_future()
            answer.add_done_callback(_retrieve)
            self._authentications[(pubkey, challenge)] = answer
            # The relay's `OK` to it, or the connection's end, settles it.
            self._oks[event.id] = answer
            answer.add_done_callback(lambda done: self._forget(event.id, done))
            try:
                # NIP-42: the signed answer to the challenge goes in an AUTH message.
                await self._send(["AUTH", event.to_json()])
            except RelayError as error:
                if not answer.done():
                    answer.set_exception(error)
                raise
        accepted, _reason = await asyncio.shield(answer)
        return accepted

    def _forget(self, event_id: str, waiter: asyncio.Future[tuple[bool, str]]) -> None:
        """An `OK` no longer waited for: one settled."""
        if self._oks.get(event_id) is waiter:
            del self._oks[event_id]

    async def close(self) -> None:
        self._reader.cancel()
        # `wait` never raises what the awaited task raised: a cancellation of this one goes on.
        await asyncio.wait([self._reader])
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_CLOSE_SECONDS):
                await self._socket.close()


RelayState = Literal["complete", "incomplete", "failed", "reading"]
"""What a relay's read was when a read of a list went on: ended, complete or not (for a read that
is not a complete read, any read that ended is "complete"), failed, or still reading."""


def tally(states: Sequence[RelayState]) -> tuple[bool, bool]:
    """Of a read of a list, from what each relay's read was when the read went on (SPEC.md §13):
    whether a majority answered, and whether a majority read completely. Only a relay whose read
    ended (complete or not) answered: one still reading has not, whatever pages it sent (a read on
    every relay keeps them all the same)."""
    wanted = majority(len(states))
    ended = sum(state in ("complete", "incomplete") for state in states)
    complete = sum(state == "complete" for state in states)
    return ended >= wanted, complete >= wanted


@dataclass(frozen=True, slots=True)
class _Gathered(Generic[T]):
    """What each relay's read kept, of the relays that answered and, on every relay, of those
    still reading that sent pages; and what each relay's read was (in the list's order)."""

    answers: dict[str, list[T]]
    states: list[RelayState]


@dataclass(frozen=True, slots=True)
class CompleteRead(Generic[T]):
    """A complete read of a list (SPEC.md §13): what the read of each relay that answered kept,
    whether a majority answered (else the read is no answer: `network`), and whether a majority
    read completely."""

    answers: dict[str, list[T]]
    answered: bool
    complete: bool


def fresh_key(random: RandomBytes) -> Secret:
    """32 fresh random bytes that are a secp256k1 secret key: a key of one read alone."""
    while True:
        candidate = random(32)
        if is_secret_key(candidate):
            return Secret(candidate)


class RelayPool:
    """The connections of one call. Use as `async with RelayPool(...) as pool:`."""

    def __init__(
        self,
        *,
        connect: WebSocketConnect,
        timeout_ms: int,
        random: RandomBytes,
        now: Callable[[], int],
    ) -> None:
        self._connect = connect
        self._timeout = timeout_ms / 1000
        self._random = random
        self._now = now
        self._connections: dict[str, asyncio.Task[Connection]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        # The relays a request to which ran out of time, or whose connection failed, in this
        # call: a read on every relay counts them as failed at once (SPEC.md §13).
        self._failed: set[str] = set()

    async def __aenter__(self) -> RelayPool:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        # Shielded, and waited for to its end: a call cancelled before or while it closes its
        # connections returns once they are closed (each close is bounded), never before, so
        # that nothing it started runs on (SPEC.md §13).
        closing = asyncio.ensure_future(self.close())
        cancelled: asyncio.CancelledError | None = None
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError as error:
                cancelled = error
        if cancelled is not None:
            raise cancelled
        closing.result()

    async def close(self) -> None:
        """Drop the requests still running, close every connection."""
        for task in self._tasks:
            task.cancel()
        connections: list[Connection] = []
        for task in self._connections.values():
            if task.done() and not task.cancelled() and task.exception() is None:
                connections.append(task.result())
            else:
                task.cancel()
        await asyncio.gather(*(connection.close() for connection in connections))
        pending = [*self._tasks, *self._connections.values()]
        if pending:
            await asyncio.wait(pending)
        for task in pending:
            _retrieve(task)
        self._tasks.clear()
        self._connections.clear()

    def _connection(self, url: str) -> asyncio.Task[Connection]:
        task = self._connections.get(url)
        if task is None:

            async def open_connection() -> Connection:
                async with asyncio.timeout(self._timeout):
                    socket = await self._connect(url)
                return Connection(url, socket, self._random)

            task = asyncio.ensure_future(open_connection())
            # A failed connection is that relay's failure for the rest of the call.
            task.add_done_callback(_retrieve)
            self._connections[url] = task
        return task

    async def _request(self, url: str, run: Callable[[Connection], Awaitable[Any]]) -> Any:
        """One request to one relay, the wait for its connection included, within the timeout.
        A timeout, or a connection that could not open or ended, marks the relay failed for the
        rest of the call."""
        try:
            async with asyncio.timeout(self._timeout):
                try:
                    connection = await asyncio.shield(self._connection(url))
                except Exception as error:
                    raise ConnectionLostError(
                        f"{url}: no connection ({type(error).__name__})"
                    ) from error
                return await run(connection)
        except TimeoutError as error:
            self._failed.add(url)
            raise RelayError(f"{url} did not answer in time") from error
        except ConnectionLostError:
            self._failed.add(url)
            raise
        except RelayError:
            raise
        except Exception as error:
            raise RelayError(f"{url} failed ({type(error).__name__})") from error

    def _page(
        self, url: str, filter_: Mapping[str, Any], key: Secret, read: _RelayRead[Any]
    ) -> Awaitable[_Page[Any]]:
        """One query of a relay's read: its page, once it ended."""
        return self._request(url, lambda c: c.query(filter_, key, self._now, read))

    async def _read_one(
        self,
        url: str,
        filter_: Mapping[str, Any],
        key: Secret,
        read: _RelayRead[Any],
        *,
        paged: bool,
    ) -> bool:
        """One relay's read: what it keeps in `read`, page after page when `paged` (SPEC.md
        §13, Paged reads: the next page from the oldest `created_at` of the last page's events
        with a string id and a safe integer `created_at`, a second earlier when none of them was
        new to the read). True once it ended; RelayError when its first page failed."""
        page = await self._page(url, filter_, key, read)
        if not paged:
            return True
        for _ in range(PAGES_MAX - 1):
            if page.oldest is None:
                break
            # Nothing new: the oldest second again would bring the same page.
            until = page.oldest if page.new else page.oldest - 1
            try:
                page = await self._page(url, {**filter_, "until": until}, key, read)
            except RelayError:
                break
        return True

    async def _read_complete_one(
        self, url: str, filter_: Mapping[str, Any], key: Secret, read: _RelayRead[Any]
    ) -> bool:
        """One relay's complete read (SPEC.md §13, Complete reads): pages of 100 events, each
        from the oldest second of the last, until one is not full. Whether it read completely;
        RelayError when its first page failed (the relay failed)."""
        page_filter = {**filter_, "limit": COMPLETE_READ_LIMIT}
        for number in range(1, PAGES_MAX + 1):
            try:
                page = await self._page(url, page_filter, key, read)
            except RelayError:
                if number == 1:
                    raise
                return False
            if page.count > COMPLETE_READ_LIMIT:
                # The relay ignored the limit: what it left out cannot be told.
                return False
            if page.count < COMPLETE_READ_LIMIT:
                return True
            if page.oldest is None or not page.new:
                # Full, and no date to go on from, or one second holds more than a page.
                return False
            page_filter = {**filter_, "limit": COMPLETE_READ_LIMIT, "until": page.oldest}
        # A 20th page that is full: the 21st is never asked.
        return False

    async def _gather(
        self,
        relays: Sequence[str],
        keep: Keep[T],
        read: Callable[[str, _RelayRead[T]], Awaitable[bool]],
        *,
        every_relay: bool,
        complete: bool,
        dated: bool = False,
    ) -> _Gathered[T]:
        """Every relay's read at once, each keeping what `keep` makes of its events (`dated`: a
        paged read's, `_RelayRead`). A relay answered when its read ended (it read completely
        when `read` returns True), failed when it raised; one whose read is still running when
        the read goes on has not answered.

        It goes on as soon as a majority answered (read completely, when `complete`), or once
        every relay ended or failed; a complete read waits for the others' events until then.
        With `every_relay`, it waits for every relay, or one timeout after that majority,
        whichever comes first, and keeps the pages a relay still reading sent by then (SPEC.md
        §13: its events count, it has not answered); the relays that failed earlier in the call
        fail at once. A read no majority can answer (read completely, on every relay) goes on at
        once."""
        wanted = majority(len(relays))
        asked = [url for url in relays if not (every_relay and url in self._failed)]
        reads = {url: _RelayRead(keep, dated=dated) for url in asked}
        tasks = {asyncio.ensure_future(read(url, reads[url])): url for url in asked}
        ended: dict[str, bool] = {}
        failed: set[str] = set()
        loop = asyncio.get_running_loop()
        deadline: float | None = None
        try:
            pending = set(tasks)
            while pending:
                reached = sum(ended.values()) if complete else len(ended)
                if len(ended) + len(pending) < wanted:
                    break
                if reached >= wanted:
                    if not every_relay:
                        break
                    if deadline is None:
                        deadline = loop.time() + self._timeout
                elif every_relay and reached + len(pending) < wanted:
                    break
                timeout = None if deadline is None else max(0.0, deadline - loop.time())
                done, pending = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    # One timeout after the majority: the others are not waited for.
                    break
                for task in done:
                    if task.exception() is None:
                        ended[tasks[task]] = task.result()
                    else:
                        failed.add(tasks[task])
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
                    self._tasks.add(task)
                elif not task.cancelled():
                    task.exception()
        # Copies: a read dropped now adds nothing more.
        answers = {url: list(reads[url].kept) for url in ended}
        states: list[RelayState] = []
        for url in relays:
            if url in ended:
                states.append("complete" if ended[url] else "incomplete")
            elif url in failed or url not in reads:
                states.append("failed")
            else:
                states.append("reading")
                # Still reading: it has not answered, but on every relay the pages it sent by
                # now are kept.
                if every_relay and reads[url].pages > 0:
                    answers[url] = list(reads[url].kept)
        return _Gathered(answers, states)

    async def read(
        self,
        relays: Sequence[str],
        filter_: Mapping[str, Any],
        keep: Keep[T],
        *,
        key: Secret | None = None,
        paged: bool = False,
        every_relay: bool = False,
    ) -> dict[str, list[T]]:
        """Every relay at once: what `keep` made of each one's events, once a majority of them
        answered (with `every_relay`, once every relay did, or one timeout after a majority did;
        then with the pages of a relay still reading too).

        NetworkError when fewer than a majority answered: their read ended, a relay still
        reading never did. `key` authenticates (NIP-42); a key of this read alone when none is
        given.
        """
        auth_key = key if key is not None else fresh_key(self._random)

        def read_one(url: str, read: _RelayRead[T]) -> Awaitable[bool]:
            return self._read_one(url, filter_, auth_key, read, paged=paged)

        gathered = await self._gather(
            relays, keep, read_one, every_relay=every_relay, complete=False, dated=paged
        )
        answered, _complete = tally(gathered.states)
        if not answered:
            ended = sum(state in ("complete", "incomplete") for state in gathered.states)
            raise NetworkError(
                f"Fewer than a majority of the relays answered ({ended} of {len(relays)})"
            )
        return gathered.answers

    async def read_complete(
        self,
        relays: Sequence[str],
        filter_: Mapping[str, Any],
        keep: Keep[T],
        *,
        every_relay: bool = False,
    ) -> CompleteRead[T]:
        """A complete read on every relay at once (SPEC.md §13), with a key of this read alone:
        what `keep` made of each answering relay's events (with `every_relay`, of one still
        reading too), whether a majority answered (their read ended, complete or not), whether a
        majority read completely. Never NetworkError: what the relays sent decides what
        follows."""
        auth_key = fresh_key(self._random)

        def read_one(url: str, read: _RelayRead[T]) -> Awaitable[bool]:
            return self._read_complete_one(url, filter_, auth_key, read)

        gathered = await self._gather(
            relays, keep, read_one, every_relay=every_relay, complete=True
        )
        answered, complete = tally(gathered.states)
        return CompleteRead(answers=gathered.answers, answered=answered, complete=complete)

    async def publish(self, relays: Sequence[str], event: Event, *, key: Secret) -> list[str]:
        """Send `event` to every relay at once: the relays that took it, once a majority did.

        NetworkError as soon as a majority no longer can take it. `key` authenticates.
        """
        wanted = majority(len(relays))
        tasks = {
            asyncio.ensure_future(
                self._request(url, lambda c: c.publish(event, key, self._now))
            ): url
            for url in relays
        }
        took: list[str] = []
        refused = 0
        try:
            pending = set(tasks)
            while pending and len(took) < wanted and len(relays) - refused >= wanted:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception() is None and task.result() is True:
                        took.append(tasks[task])
                    else:
                        refused += 1
        finally:
            # The relays past the majority keep their request until the call is done.
            for task in tasks:
                if not task.done():
                    self._tasks.add(task)
                elif not task.cancelled():
                    task.exception()
        if len(took) < wanted:
            raise NetworkError(
                f"Fewer than a majority of the relays took it ({len(took)} of {len(relays)})"
            )
        return took
