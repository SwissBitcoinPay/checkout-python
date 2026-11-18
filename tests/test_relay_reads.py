"""Complete and paged reads (SPEC.md §13) against the vectors, and the reads on every relay.

A scripted relay answers each page of a read as the vector says (its events as
`[id, created_at]`, or "fails"): the pages the read asks, whether it is complete, what it keeps.
The vectors that give what each relay's read was (`lists`, `onEveryRelay`) go through `tally`,
which the pool decides with, and run again end to end against local relays, through the pool and
the client's read of the leases of deposit addresses (PORTING.md, steps 4 and 5).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Sequence
from typing import Any

import pytest

from fakes.relay import Relay, Relays
from sattleme_checkout._constants import COMPLETE_READ_LIMIT, PAGES_MAX, RECORD_KIND
from sattleme_checkout._crypto import Secret
from sattleme_checkout._declaration import Registry
from sattleme_checkout._derive import onchain_registry_address
from sattleme_checkout._leasing import LeaseRead, read_leases
from sattleme_checkout._nostr import Event
from sattleme_checkout._relay import RelayPool, RelayState, majority, tally
from sattleme_checkout.errors import NetworkError
from spec_vectors import vector

READS: Any = vector("relay-reads.json")


def test_the_constants_are_the_vectors() -> None:
    assert READS["limit"] == COMPLETE_READ_LIMIT
    assert READS["pages"] == PAGES_MAX


class Scripted:
    """A relay's socket that answers the n-th query with the n-th page of a script (a list of
    events, or "fails": CLOSED), and records each query's filter; `delay` before each answer,
    `mute` never answers, `gate` answers once it is set; `asked` is set once `asks` queries came."""

    def __init__(
        self,
        pages: Sequence[Any],
        *,
        delay: float = 0.0,
        mute: bool = False,
        gate: asyncio.Event | None = None,
        asked: asyncio.Event | None = None,
        asks: int = 1,
    ) -> None:
        self.pages = list(pages)
        self.filters: list[dict[str, Any]] = []
        self.delay = delay
        self.mute = mute
        self.gate = gate
        self.asked = asked
        self.asks = asks
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        self._later: set[asyncio.Task[None]] = set()

    async def send(self, message: str) -> None:
        value = json.loads(message)
        if value[0] != "REQ":
            return
        subscription, filter_ = value[1], value[2]
        self.filters.append(filter_)
        if self.asked is not None and len(self.filters) >= self.asks:
            self.asked.set()
        if self.mute:
            return
        page = self.pages[len(self.filters) - 1] if len(self.filters) <= len(self.pages) else []
        if self.gate is None:
            asyncio.get_running_loop().call_later(self.delay, self._answer, subscription, page)
            return
        task = asyncio.ensure_future(self._after_gate(self.gate, subscription, page))
        self._later.add(task)
        task.add_done_callback(self._later.discard)

    async def _after_gate(self, gate: asyncio.Event, subscription: str, page: Any) -> None:
        await gate.wait()
        await asyncio.sleep(self.delay)
        self._answer(subscription, page)

    def _answer(self, subscription: str, page: Any) -> None:
        if page == "fails":
            self._inbox.put_nowait(json.dumps(["CLOSED", subscription, "error: fails"]))
            return
        # ("unended", events): its events, and no EOSE.
        ended = not (isinstance(page, tuple) and page[0] == "unended")
        for event in page if ended else page[1]:
            self._inbox.put_nowait(json.dumps(["EVENT", subscription, event]))
        if ended:
            self._inbox.put_nowait(json.dumps(["EOSE", subscription]))

    async def recv(self) -> str:
        return await self._inbox.get()

    async def close(self) -> None:
        return None


def _pool(sockets: dict[str, Scripted], timeout_ms: int = 2000) -> RelayPool:
    async def connect(url: str) -> Scripted:
        return sockets[url]

    return RelayPool(
        connect=connect, timeout_ms=timeout_ms, random=secrets.token_bytes, now=lambda: 0
    )


def _events(page: Any) -> Any:
    if page == "fails":
        return page
    return [{"id": event_id, "created_at": created_at} for event_id, created_at in page]


FILTER = {"kinds": [30078], "#d": ["a"]}


def _all(value: object) -> object:
    """Keeps every event a relay sends (once per id)."""
    return value


@pytest.mark.parametrize("case", READS["cases"], ids=lambda c: c["name"])
async def test_complete_reads(case: dict[str, Any]) -> None:
    socket = Scripted([_events(page) for page in case["pages"]])
    async with _pool({"wss://a.example": socket}) as pool:
        read = await pool.read_complete(["wss://a.example"], FILTER, _all)
    expected = case["expected"]
    assert [f.get("until") for f in socket.filters] == expected["untils"]
    assert all(f["limit"] == COMPLETE_READ_LIMIT for f in socket.filters)
    assert all(
        {k: v for k, v in f.items() if k not in ("limit", "until")} == FILTER
        for f in socket.filters
    )
    assert read.answered is expected["answered"]
    assert read.complete is expected["complete"]
    assert len(read.answers.get("wss://a.example", [])) == expected["kept"]


@pytest.mark.parametrize("case", READS["paged"], ids=lambda c: c["name"])
async def test_paged_reads(case: dict[str, Any]) -> None:
    """relay-reads.json, `paged`: a paged read (a sale's declarations) of one relay answering
    each page as the vector says: the `until` of each page asked, whether the relay answered (a
    read of one relay: else no majority answered, `network`), and how many events it keeps."""
    socket = Scripted([_events(page) for page in case["pages"]])
    expected = case["expected"]
    async with _pool({"wss://a.example": socket}) as pool:
        try:
            answers = await pool.read(["wss://a.example"], FILTER, _all, paged=True)
        except NetworkError:
            answered, kept = False, 0
        else:
            answered, kept = True, len(answers["wss://a.example"])
    assert [f.get("until") for f in socket.filters] == expected["untils"]
    assert all({k: v for k, v in f.items() if k != "until"} == FILTER for f in socket.filters)
    assert answered is expected["answered"]
    assert kept == expected["kept"]


@pytest.mark.parametrize("case", READS["onEveryRelay"], ids=lambda c: c["name"])
def test_reads_on_every_relay(case: dict[str, Any]) -> None:
    """relay-reads.json, `onEveryRelay`: from what each relay's read was when a read on every
    relay went on, whether a majority answered and whether a majority read completely, as the
    pool decides it for both reads (`tally`): a relay still reading never answered."""
    answered, complete = tally(case["relays"])
    assert {"answered": answered, "complete": complete} == case["expected"]


@pytest.mark.parametrize("case", READS["lists"], ids=lambda c: c["name"])
def test_what_a_read_of_a_list_counts(case: dict[str, Any]) -> None:
    """relay-reads.json, `lists`: whether a majority of the list read completely, as the pool
    counts it (`tally`) from each relay's read."""
    _answered, complete = tally(case["relays"])
    assert complete is case["expected"]


async def test_a_complete_read_goes_on_at_a_complete_majority() -> None:
    """Not on every relay: a mute relay slows nothing once a majority read completely."""
    sockets = {
        "wss://a.example": Scripted([[]]),
        "wss://b.example": Scripted([[]]),
        "wss://c.example": Scripted([], mute=True),
    }
    async with _pool(sockets, timeout_ms=5000) as pool:
        started = time.monotonic()
        read = await pool.read_complete(list(sockets), FILTER, _all)
        assert time.monotonic() - started < 1
    assert read.complete
    assert set(read.answers) == {"wss://a.example", "wss://b.example"}


async def test_a_complete_read_waits_for_the_events_of_an_incomplete_relay() -> None:
    """Not on every relay, a majority not yet complete: every relay's events are waited for."""
    late = [{"id": f"{i:064x}", "created_at": 5} for i in range(COMPLETE_READ_LIMIT + 1)]
    sockets = {
        "wss://a.example": Scripted([[]]),
        "wss://b.example": Scripted([late], delay=0.3),
        "wss://c.example": Scripted(["fails"]),
    }
    async with _pool(sockets) as pool:
        read = await pool.read_complete(list(sockets), FILTER, _all)
    assert read.answered
    assert not read.complete
    assert len(read.answers["wss://b.example"]) == COMPLETE_READ_LIMIT + 1


async def test_a_read_on_every_relay_sees_what_a_late_relay_holds() -> None:
    """A relay hides a record, another one that holds it answers late but in time: the read on
    every relay still sees it, one timeout after the majority at most."""
    record = {"id": "e" * 64, "created_at": 9}
    sockets = {
        "wss://a.example": Scripted([[], []]),
        "wss://b.example": Scripted([[], []]),
        "wss://c.example": Scripted([[record], [record]], delay=0.4),
    }
    async with _pool(sockets, timeout_ms=1000) as pool:
        read = await pool.read_complete(list(sockets), FILTER, _all, every_relay=True)
        answers = await pool.read(list(sockets), FILTER, _all, every_relay=True)
    assert read.complete
    assert read.answers["wss://c.example"] == [record]
    assert answers["wss://c.example"] == [record]


async def test_a_mute_relay_delays_a_read_on_every_relay_one_timeout_at_most() -> None:
    sockets = {
        "wss://a.example": Scripted([[], []]),
        "wss://b.example": Scripted([[], []]),
        "wss://c.example": Scripted([], mute=True),
    }
    async with _pool(sockets, timeout_ms=600) as pool:
        started = time.monotonic()
        read = await pool.read_complete(list(sockets), FILTER, _all, every_relay=True)
        first = time.monotonic() - started
        # It ran out of time in this call: not waited for again.
        started = time.monotonic()
        answers = await pool.read(list(sockets), FILTER, _all, every_relay=True)
        second = time.monotonic() - started
    assert read.complete
    assert 0.5 < first < 1.0
    assert second < 0.3
    assert set(answers) == {"wss://a.example", "wss://b.example"}
    assert len(sockets["wss://c.example"].filters) == 1


async def test_a_read_on_every_relay_goes_on_once_no_majority_can_read_completely() -> None:
    full = [{"id": f"{i:064x}", "created_at": 5} for i in range(COMPLETE_READ_LIMIT + 1)]
    sockets = {
        "wss://a.example": Scripted([full]),
        "wss://b.example": Scripted(["fails"]),
        "wss://c.example": Scripted([], mute=True),
    }
    async with _pool(sockets, timeout_ms=5000) as pool:
        started = time.monotonic()
        read = await pool.read_complete(list(sockets), FILTER, _all, every_relay=True)
        assert time.monotonic() - started < 1
    assert not read.answered
    assert not read.complete


@pytest.mark.parametrize(
    ("pages", "untils", "kept"),
    [
        # A new event without a date, and dated ones seen before: one second earlier.
        ([[["a", 1000], ["b", 990]], [["b", 990], ["c", "x"]], []], [None, 990, 989], ["a", "b"]),
        # Undated, or by an id that is no string: they neither page the read nor are kept.
        ([[["a", 1000], ["b", 2**53], ["c", "900"], [None, 800]], []], [None, 1000], ["a"]),
        # A page without an event dated by a safe integer ends the read.
        ([[["a", 1000]], [["b", 1.5e300]], [["c", 900]]], [None, 1000], ["a"]),
    ],
    ids=["new but undated", "undated or without id", "nothing dated"],
)
async def test_a_paged_read_pages_on_the_raw_id_and_created_at(
    pages: list[Any], untils: list[int | None], kept: list[str]
) -> None:
    """SPEC.md §13, Paged reads: only an event's string `id` and safe integer `created_at` page
    the read, whatever else it holds; an event without them is not kept."""
    socket = Scripted([_events(page) for page in pages])
    async with _pool({"wss://a.example": socket}) as pool:
        answers = await pool.read(["wss://a.example"], FILTER, _all, paged=True)
    assert [f.get("until") for f in socket.filters] == untils
    assert [event["id"] for event in answers["wss://a.example"]] == kept  # type: ignore[index]


async def test_a_relay_still_reading_has_not_answered_whatever_pages_it_sent() -> None:
    """SPEC.md §13, Reads on every relay: only a relay whose read ended answered. a's read ends
    incomplete (it ignores the limit), b fails, c sent a full first page and is still reading its
    second when the read goes on (a majority can no longer read completely): fewer than a
    majority answered, though the pages of c are kept."""
    over = [{"id": f"{i:064x}", "created_at": 5} for i in range(COMPLETE_READ_LIMIT + 1)]
    first = [{"id": f"{i:064x}", "created_at": 1000 - i} for i in range(200, 300)]
    # a and b answer once c asked its second page: c's first page has ended by then.
    second_asked = asyncio.Event()
    sockets = {
        "wss://a.example": Scripted([over], gate=second_asked),
        "wss://b.example": Scripted(["fails"], gate=second_asked),
        # Its second page never comes: still reading, whatever the timeout.
        "wss://c.example": Scripted([first, ("unended", [])], asked=second_asked, asks=2),
    }
    async with _pool(sockets, timeout_ms=60_000) as pool:
        read = await pool.read_complete(list(sockets), FILTER, _all, every_relay=True)
    assert len(sockets["wss://c.example"].filters) == 2
    assert not read.answered
    assert not read.complete
    assert read.answers["wss://c.example"] == first
    assert set(read.answers) == {"wss://a.example", "wss://c.example"}


async def test_a_read_on_every_relay_keeps_the_pages_a_relay_still_reading_sent() -> None:
    """Cut one timeout after the majority: the pages the late relay sent are kept, not the one
    it is still sending (each of its pages in time)."""
    first = [{"id": f"{i:064x}", "created_at": 1000 - i} for i in range(COMPLETE_READ_LIMIT)]
    second = [{"id": f"{i:064x}", "created_at": 800} for i in range(1000, 1050)]
    sockets = {
        "wss://a.example": Scripted([[]]),
        "wss://b.example": Scripted([[]]),
        "wss://c.example": Scripted([first, second], delay=0.3),
    }
    async with _pool(sockets, timeout_ms=450) as pool:
        # c answers its first page at 0.3 s, its second at 0.6 s: past the cut at 0.45 s.
        started = time.monotonic()
        read = await pool.read_complete(list(sockets), FILTER, _all, every_relay=True)
        assert time.monotonic() - started < 0.55
    assert read.complete
    assert len(read.answers["wss://c.example"]) == COMPLETE_READ_LIMIT


async def test_a_read_on_every_relay_keeps_nothing_of_a_page_still_coming() -> None:
    """Cut one timeout after the majority while a relay sends a page: the events of that page
    that came by then are not kept, only its pages that ended."""
    first = [{"id": f"{i:064x}", "created_at": 1000 - i} for i in range(COMPLETE_READ_LIMIT)]
    coming = [{"id": f"{i:064x}", "created_at": 800} for i in range(1000, 1050)]
    sockets = {
        "wss://a.example": Scripted([[]]),
        "wss://b.example": Scripted([[]]),
        "wss://c.example": Scripted([first, ("unended", coming)]),
    }
    async with _pool(sockets, timeout_ms=300) as pool:
        read = await pool.read_complete(list(sockets), FILTER, _all, every_relay=True)
    assert len(sockets["wss://c.example"].filters) == 2
    assert read.complete
    assert read.answers["wss://c.example"] == first


async def test_a_full_page_whose_new_events_have_no_date_goes_on_from_its_dated_one() -> None:
    """SPEC.md §13: a full page holding an event with a string id not seen before, dated or not,
    and one with a safe integer `created_at`, asks the next page from the oldest of those, even
    when that dated one was seen before."""
    first = [{"id": f"{i:064x}", "created_at": 1000 - i} for i in range(COMPLETE_READ_LIMIT)]
    undated = [{"id": f"{i:064x}", "created_at": 2**53 + 2} for i in range(1000, 1099)]
    socket = Scripted([first, [first[-1], *undated], []])
    async with _pool({"wss://a.example": socket}) as pool:
        read = await pool.read_complete(["wss://a.example"], FILTER, _all)
    assert [f.get("until") for f in socket.filters] == [None, 901, 901]
    assert read.answered
    assert read.complete
    assert len(read.answers["wss://a.example"]) == 199


async def test_a_full_page_of_events_seen_before_ends_a_read_incomplete() -> None:
    """Whether dated or not: no event with a string id not seen before (one second holds more
    events than a page)."""
    first = [{"id": f"{i:064x}", "created_at": 1000} for i in range(COMPLETE_READ_LIMIT)]
    socket = Scripted([first, first, []])
    async with _pool({"wss://a.example": socket}) as pool:
        read = await pool.read_complete(["wss://a.example"], FILTER, _all)
    assert [f.get("until") for f in socket.filters] == [None, 1000]
    assert read.answered
    assert not read.complete
    assert len(read.answers["wss://a.example"]) == COMPLETE_READ_LIMIT


async def test_fewer_than_a_majority_on_every_relay_is_no_answer() -> None:
    sockets = {
        "wss://a.example": Scripted([[]]),
        "wss://b.example": Scripted(["fails"]),
        "wss://c.example": Scripted(["fails"]),
    }
    async with _pool(sockets) as pool:
        with pytest.raises(NetworkError):
            await pool.read(list(sockets), FILTER, _all, every_relay=True)


# ---- End to end, against local relays (PORTING.md, steps 4 and 5) ------------------------------
#
# Each case's relays are local relays (`fakes.relay`, over WebSocket), each made to read as its
# state says, and read by the client's read of the leases of a deposit address (`read_leases`: a
# complete read, on every relay for a lease's confirmation) through the pool, which tells a relay
# still reading, one whose read failed and one whose read ended apart itself. By state:
# - complete: holds 3 events at the lease's address (one page, not full); or 250, one second each
#   (it pages: 100, 100, then 52); or caps its answers at 40 (a page that is not full: complete,
#   SPEC.md §13 relies on relays that honor the limit);
# - incomplete: holds 150 events of one second (a full page, then the same page: no new event);
#   or 2 100 of one second each (a 20th page that is full); or never answers its second page
#   (it runs out of time);
# - failed: refuses connections; or accepts them and never answers (its first page runs out of
#   time);
# - reading, when the read goes on: holds 2 100 events and answers each page a quarter of the
#   timeout late (its read would last five timeouts); or never answers, when the read goes on
#   before its first page could run out of time. A relay still reading that the read waits for is
#   one that fails at its timeout (relay-reads.json): it never answers.

E2E_TIMEOUT_MS = 2000
"""relayTimeoutMs of these reads: ample for a local relay on a busy machine."""
SLOW_PAGE_SECONDS = E2E_TIMEOUT_MS / 4 / 1000
LATE_COMPLETE_SECONDS = 0.4
"""How late the relays that read completely answer each page when a read goes on as soon as a
majority of them did, so that every other relay's read has ended (or failed) as its case says."""
NOW = 1_791_378_000
REGISTRY = Registry(key=Secret(bytes(range(32))), reader="ab" * 32)
ADDRESS = "bc1px22v5j99gdwhxqyelg2p6tdcfajjd3njhzn22c2gura2dvxg2xlqw2wte4"
LEASE_D = onchain_registry_address(REGISTRY.key, ADDRESS)


def _hold(relay: Relay, count: int, *, one_second: bool = False) -> None:
    """`count` events at the lease's address, anybody's (none opens as a lease: a read pages
    through them all the same), the newest at NOW; of one second each, or all of NOW."""
    for index in range(count):
        relay.store(
            Event(
                id=f"{index:064x}",
                pubkey=f"{index:064x}",
                created_at=NOW if one_second else NOW - index,
                kind=RECORD_KIND,
                tags=(("d", LEASE_D),),
                content="",
                sig="00" * 64,
            )
        )


def _few(relay: Relay) -> None:
    _hold(relay, 3)


def _pages(relay: Relay) -> None:
    _hold(relay, 250)


def _caps(relay: Relay) -> None:
    _hold(relay, 250)
    relay.cap = 40


def _one_second(relay: Relay) -> None:
    _hold(relay, 150, one_second=True)


def _twenty_pages(relay: Relay) -> None:
    _hold(relay, 2100)


def _stalls_at_page_2(relay: Relay) -> None:
    _hold(relay, 250)
    relay.stalls = lambda filter_: "until" in filter_


def _refuses_connections(relay: Relay) -> None:
    relay.down = True


def _mute(relay: Relay) -> None:
    relay.mute = True


def _slow_pager(relay: Relay) -> None:
    _hold(relay, 2100)
    _late(relay, SLOW_PAGE_SECONDS)


def _late(relay: Relay, seconds: float) -> None:
    """Each query answered `seconds` late."""
    relay.late = lambda _filter: True
    relay.late_by = seconds


MAKERS = {
    "few": _few,
    "pages": _pages,
    "caps": _caps,
    "one-second": _one_second,
    "twenty-pages": _twenty_pages,
    "stalls-at-page-2": _stalls_at_page_2,
    "refuses-connections": _refuses_connections,
    "mute": _mute,
    "slow-pager": _slow_pager,
}


def _turn(choices: Sequence[str], case: int, relay: int) -> str:
    """One of `choices`, in turn from case to case and relay to relay: the relays of one state
    are made in each of its ways."""
    return choices[(case + relay) % len(choices)]


async def _read_leases_on(
    makers: Sequence[tuple[str, float]], *, every_relay: bool
) -> tuple[LeaseRead, float, list[Relay]]:
    """Local relays made by `makers` (each with how late it answers each query), and the read
    of the leases of ADDRESS on them, on every relay or not: what it gave, how long it took, and
    the relays."""
    relays = Relays()
    names = [f"{maker}-{index}.test" for index, (maker, _late) in enumerate(makers)]
    urls = await relays.start(*names)
    try:
        for url, (maker, late) in zip(urls, makers, strict=True):
            relay = relays[url]
            MAKERS[maker](relay)
            if late:
                _late(relay, late)
        pool = RelayPool(
            connect=relays.connect,
            timeout_ms=E2E_TIMEOUT_MS,
            random=secrets.token_bytes,
            now=lambda: NOW,
        )
        async with pool:
            started = time.monotonic()
            read = await read_leases(pool, REGISTRY, [ADDRESS], urls, NOW, every_relay=every_relay)
            elapsed = time.monotonic() - started
        return read, elapsed, [relays[url] for url in urls]
    finally:
        await relays.close()


def _list_makers(index: int, case: dict[str, Any]) -> list[tuple[str, float]]:
    """A `lists` case's local relays. Complete: the read goes on as soon as a majority read
    completely, the others dropped; so that each of those has ended as the case says by then,
    they end at once and the relays that read completely answer late. Not complete: the read
    waits for every relay."""
    expected = case["expected"]
    makers: list[tuple[str, float]] = []
    for relay, state in enumerate(case["relays"]):
        ways: tuple[str, ...]
        if state == "complete":
            maker = _turn(("few", "pages", "caps"), index, relay)
            makers.append((maker, LATE_COMPLETE_SECONDS if expected else 0.0))
        elif state == "incomplete":
            ways = (
                ("one-second",) if expected else ("one-second", "stalls-at-page-2", "twenty-pages")
            )
            makers.append((_turn(ways, index, relay), 0.0))
        else:
            ways = ("refuses-connections",) if expected else ("mute", "refuses-connections")
            makers.append((_turn(ways, index, relay), 0.0))
    return makers


LISTS = list(enumerate(READS["lists"]))


@pytest.mark.parametrize(("index", "case"), LISTS, ids=[c["name"] for _i, c in LISTS])
@pytest.mark.parametrize("every_relay", [False, True])
async def test_a_read_of_a_list_end_to_end(
    index: int, case: dict[str, Any], every_relay: bool
) -> None:
    """relay-reads.json, `lists`, end to end: a complete read of a list of local relays, each
    reading as its case says, is complete when a majority read completely (on every relay or
    not)."""
    read, _elapsed, _relays = await _read_leases_on(
        _list_makers(index, case), every_relay=every_relay
    )
    assert read.complete is case["expected"]


def _goes_on_with(states: Sequence[RelayState]) -> str | None:
    """When the pool goes on with a complete read on every relay while these are its relays'
    states (SPEC.md §13): once no relay is still reading ("settled"); or, with some still
    reading, a timeout after a majority read completely ("grace"), or as soon as a majority can
    no longer read completely ("at once"). Otherwise (None) it never does: it waits for the
    relays still reading, until each ends or fails (relay-reads.json: such a case tests the count
    alone)."""
    needed = majority(len(states))
    if "reading" not in states:
        return "settled"
    if states.count("complete") >= needed:
        return "grace"
    if states.count("complete") + states.count("reading") < needed:
        return "at once"
    return None


def _every_relay_makers(index: int, case: dict[str, Any]) -> list[tuple[str, float]]:
    """An `onEveryRelay` case's local relays: a relay that is not still reading has ended or
    failed when the read goes on (before the timeout after a majority read completely, or at
    once); a relay still reading is a slow one when the read goes on a timeout after the
    majority, else one that never answers (still reading at once, or failing at its timeout when
    the read waits for it)."""
    when = _goes_on_with(case["relays"])
    makers: list[tuple[str, float]] = []
    for relay, state in enumerate(case["relays"]):
        if state == "complete":
            ways: tuple[str, ...] = ("few", "pages", "caps")
        elif state == "incomplete":
            ways = ("one-second", "stalls-at-page-2", "twenty-pages")
            if when != "settled":
                ways = ("one-second", "twenty-pages")
        elif state == "failed":
            ways = (
                ("refuses-connections", "mute") if when == "settled" else ("refuses-connections",)
            )
        else:
            ways = ("slow-pager",) if when == "grace" else ("mute",)
        makers.append((_turn(ways, index, relay), 0.0))
    return makers


EVERY_RELAY = list(enumerate(READS["onEveryRelay"]))


def test_the_end_to_end_reads_make_every_kind_of_relay_and_case() -> None:
    """Every way of making a relay of each state is used, and the cases are of every kind: the
    read going on once settled, a timeout after the majority, at once, and the cases it waits
    in."""
    used = {maker for i, c in LISTS for maker, _late in _list_makers(i, c)}
    used |= {maker for i, c in EVERY_RELAY for maker, _late in _every_relay_makers(i, c)}
    assert used == set(MAKERS)
    kinds = {_goes_on_with(c["relays"]) for c in READS["onEveryRelay"]}
    assert kinds == {"settled", "grace", "at once", None}


@pytest.mark.parametrize(("index", "case"), EVERY_RELAY, ids=[c["name"] for _i, c in EVERY_RELAY])
async def test_a_read_on_every_relay_end_to_end(index: int, case: dict[str, Any]) -> None:
    """relay-reads.json, `onEveryRelay`, end to end: the read of a lease's confirmation on local
    relays, each reading as its case says, gives what the case expects. A relay still reading
    that the read waits for (a case that tests the count alone) is one that fails at its
    timeout: the read goes on once it failed, a relay still reading having answered no more than
    one that failed."""
    when = _goes_on_with(case["relays"])
    read, elapsed, relays = await _read_leases_on(
        _every_relay_makers(index, case), every_relay=True
    )
    assert {"answered": read.answered, "complete": read.complete} == case["expected"]
    timeout = E2E_TIMEOUT_MS / 1000
    reading = [
        relay for relay, state in zip(relays, case["relays"], strict=True) if state == "reading"
    ]
    if when == "grace":
        # The relays still reading were waited for a timeout after the majority, not until
        # their read ended.
        assert elapsed >= timeout - 0.05
        assert all(len(relay.requests) < PAGES_MAX for relay in reading)
    elif when == "at once":
        # Before a mute relay's first page could run out of time: it was still reading.
        assert elapsed < timeout - 0.1
    elif when is None:
        # Waited for until the relays still reading failed at their timeout: asked one page,
        # never answered, and asked nothing more.
        assert elapsed >= timeout - 0.05
        assert all([message[0] for message in relay.messages] == ["REQ"] for relay in reading)
