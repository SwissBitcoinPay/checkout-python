"""The merchant's Bitcoin node (SPEC.md §31, The merchant's Bitcoin node; §30, the record):
bitcoin-nodes.json through the package's own functions (PORTING.md, Steps, 4): its `urls` through
the check of a node URL and the URLs the node is read at, its `channels` through the reading of
the channel, its `reads` through the explorer's reads (a REST read, the two reads of a check's step
1, and the WebSocket's connections), its `records` through the page's reading of a checkout link's
record, and its `writes` through the making of one."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import re
from typing import Any

import pytest

from fakes.http import FakeHttp
from sattleme_checkout._channel import ActiveChannel, parse_channel
from sattleme_checkout._checkout import link_value, read_link_value
from sattleme_checkout._constants import MEMPOOL, MEMPOOL_WEBSOCKET, NODE_FAILED_RECENTLY_SECONDS
from sattleme_checkout._explorer import CallExplorer, Explorer, ExplorerName, explorers_to_ask
from sattleme_checkout._http import Http
from sattleme_checkout._lnurl import pay_url
from sattleme_checkout._node import BitcoinNode, bitcoin_node_of, parse_node_url, record_node_for
from sattleme_checkout._nostr import check_event
from sattleme_checkout._tasks import both
from sattleme_checkout._urls import https_host, is_checkout_origin, is_relay_url
from sattleme_checkout._waiting import watch_address
from sattleme_checkout.errors import NetworkError
from spec_vectors import vector

NODES: Any = vector("bitcoin-nodes.json")
APP: Any = vector("app/app.json")


def channel_with(case: dict[str, Any]) -> object:
    """The app's channel, valid, with `bitcoinNode` the case's `value` (absent: without it)."""
    value = copy.deepcopy(APP["channel"]["value"]["v"])
    value.pop("bitcoinNode", None)
    if "value" in case:
        value["bitcoinNode"] = copy.deepcopy(case["value"])
    return parse_channel(value, check_event(APP["channel"]["event"]))  # type: ignore[arg-type]


@pytest.mark.parametrize("case", NODES["urls"], ids=lambda c: c["name"])
def test_urls(case: dict[str, Any]) -> None:
    node = parse_node_url(case["url"])
    found = None if node is None else {"rest": node.rest, "webSocket": node.web_socket}
    assert found == case["expected"]
    if node is not None:
        assert node.url == case["url"]


@pytest.mark.parametrize("case", NODES["channels"], ids=lambda c: c["name"])
def test_channels(case: dict[str, Any]) -> None:
    channel = channel_with(case)
    # Valid either way: a node invalid in any way is none (SPEC.md §14).
    assert isinstance(channel, ActiveChannel)
    node = channel.bitcoin_node
    assert (None if node is None else dataclasses.asdict(node)) == case["expected"]
    alone = bitcoin_node_of(case.get("value"))
    assert alone == node


@pytest.mark.parametrize("case", NODES["records"], ids=lambda c: c["name"])
def test_records(case: dict[str, Any]) -> None:
    shown = read_link_value(case["value"])
    # Valid in every case: a node set aside leaves the record valid.
    assert shown is not None
    node = shown.bitcoin_node
    assert (None if node is None else node.to_json()) == case["expected"]


@pytest.mark.parametrize("case", NODES["writes"], ids=lambda c: c["name"])
def test_writes(case: dict[str, Any]) -> None:
    # The channel create_invoice read, then the record of its checkout link.
    channel = channel_with({"value": case["channel"]} if "channel" in case else {})
    assert isinstance(channel, ActiveChannel)
    value = link_value(
        business_name="Sunrise Bakery",
        invoice=None if case["address"] else APP["realInvoice"]["invoice"],
        verify_url=None if case["address"] else "https://breez.tips/lnurlp/verify/x",
        onchain=(
            ("bc1p" + "q" * 58, 150_000_000, 1_791_381_280, 1_791_382_180)
            if case["address"]
            else None
        ),
        price=None,
        note="Order 1042",
        redirect_url=None,
        rest=None,
        node=record_node_for(channel.bitcoin_node),
    )
    assert value["version"] == (2 if case["address"] else 1)
    # Absent, never null, when it has none.
    assert ("bitcoinNode" in value) == (case["expected"] is not None)
    assert value.get("bitcoinNode") == case["expected"]


def test_every_other_url_keeps_its_own_rule() -> None:
    """SPEC.md §15, §9, §30: the node URLs only the node's rule allows (http, or an IPv4 host)
    are no https URL, no checkout origin, no relay; an IP address is never a relay's host nor a
    Lightning address's."""
    node_only = [
        case["url"]
        for case in NODES["urls"]
        if case["expected"] is not None
        and (
            case["url"].startswith("http://")
            or re.match(r"https://[0-9.]+(?::|/|$)", case["url"]) is not None
        )
    ]
    assert len(node_only) > 15
    for url in node_only:
        assert https_host(url) is None, url
        assert not is_checkout_origin(url), url
        assert not is_relay_url("ws" + url[len("http") :]), url
        host = re.match(r"https?://([^/:]+)", url)
        assert host is not None
        if re.fullmatch("[0-9.]+", host.group(1)):
            assert not is_relay_url(f"wss://{host.group(1)}"), url
            assert pay_url(f"sattleme@{host.group(1)}") is None, url


@pytest.mark.parametrize(
    "value",
    [
        # What Python's own readings would let through: a line end (`$`), digits of other scripts
        # (`int`), a host's case folded, bytes.
        "http://192.168.1.10\n",
        "https://node.example/mempool\n",
        "http://192.168.1.\u0661\u0660",
        "https://node.example:\u0663\u0660\u0660\u0666",
        "http://\uff11\uff19\uff12.168.1.10",
        "https://NODE.example",
        b"https://node.example",
        None,
        ["https://node.example"],
    ],
)
def test_no_other_spelling_is_a_node_url(value: object) -> None:
    assert parse_node_url(value) is None


NODE = "http://192.168.1.10:3006"
NOW = 1_791_378_000


def explorer_of(case: dict[str, Any]) -> CallExplorer:
    """The explorer of a case: its node failed 1 s inside the 60 s when `failedRecently`."""
    failures: dict[str, int] = {}
    node = case["node"]
    if node is not None and node["failedRecently"]:
        failures[NODE] = NOW - NODE_FAILED_RECENTLY_SECONDS + 1
    bitcoin_node = (
        None if node is None else BitcoinNode(url=NODE, fallback=node["fallback"], payers=False)
    )
    return CallExplorer(Http(FakeHttp(), 1000), bitcoin_node, failures, lambda: NOW)


def name_of(explorer: Explorer) -> ExplorerName:
    names: dict[str, ExplorerName] = {MEMPOOL: "mempool", f"{NODE}/api": "node"}
    return names[explorer.base]


def answers(case: dict[str, Any], name: ExplorerName) -> bool:
    return bool(case["node"]["answers"] if name == "node" else case["mempoolAnswers"])


@pytest.mark.parametrize("case", NODES["reads"], ids=lambda c: c["name"])
def test_reads_the_explorers_asked_first(case: dict[str, Any]) -> None:
    # Every explorer a read may ask; the read stops at the first that answers.
    node = case["node"]
    order = explorers_to_ask(
        None if node is None else node["fallback"],
        failed_recently=node is not None and node["failedRecently"],
    )
    assert order[: len(case["expected"]["asked"])] == case["expected"]["asked"]


@pytest.mark.parametrize("case", NODES["reads"], ids=lambda c: c["name"])
async def test_reads_a_rest_read(case: dict[str, Any]) -> None:
    asked: list[str] = []

    async def run(explorer: Explorer) -> str:
        name = name_of(explorer)
        asked.append(name)
        if not answers(case, name):
            raise NetworkError(f"{name} did not answer")
        return name

    try:
        result = await explorer_of(case).read(run)
    except NetworkError:
        result = "network"
    assert {"asked": asked, "result": result} == case["expected"]


@pytest.mark.parametrize("failing", ["transactions", "height"])
@pytest.mark.parametrize("case", NODES["reads"], ids=lambda c: c["name"])
async def test_reads_step_1_one_read_of_two_requests(case: dict[str, Any], failing: str) -> None:
    """The address's transactions and the chain's height, of one explorer: when either fails,
    both are asked of the next one, never one from each."""
    asked: list[str] = []

    async def request(name: ExplorerName, what: str) -> str:
        asked.append(f"{name} {what}")
        await asyncio.sleep(0)
        if not answers(case, name) and what == failing:
            raise NetworkError(f"{name} failed")
        return name

    async def run(explorer: Explorer) -> str:
        transactions, height = await both(
            request(name_of(explorer), "transactions"), request(name_of(explorer), "height")
        )
        assert transactions == height
        return transactions

    try:
        result = await explorer_of(case).read(run)
    except NetworkError:
        result = "network"
    assert result == case["expected"]["result"]
    assert asked == [
        f"{name} {what}"
        for name in case["expected"]["asked"]
        for what in ("transactions", "height")
    ]


class _Socket:
    """A WebSocket in memory that answers: one message, then nothing until it is closed."""

    def __init__(self, answered: asyncio.Event) -> None:
        self._answered = answered
        self._sent = False

    async def send(self, message: str) -> None:
        pass

    async def recv(self) -> str:
        if not self._sent:
            self._sent = True
            return '{"block": {}}'
        self._answered.set()
        await asyncio.Event().wait()
        raise AssertionError  # pragma: no cover - never woken

    async def close(self) -> None:
        pass


@pytest.mark.parametrize("case", NODES["reads"], ids=lambda c: c["name"])
async def test_reads_a_websockets_connection(case: dict[str, Any]) -> None:
    """A connection that fails, or is lost before any message, goes to the next explorer at once;
    every one failing, the watch waits before it connects again (`network`)."""
    urls = {MEMPOOL_WEBSOCKET: "mempool", "ws://192.168.1.10:3006/api/v1/ws": "node"}
    asked: list[str] = []
    connected: list[str] = []
    ended = asyncio.Event()

    async def connect(url: str) -> Any:
        name: ExplorerName = urls[url]  # type: ignore[assignment]
        asked.append(name)
        if not answers(case, name):
            raise ConnectionRefusedError(f"{name} did not answer")
        connected.append(name)
        return _Socket(ended)

    async def sleep(_seconds: float) -> None:
        ended.set()
        await asyncio.Event().wait()

    watch = asyncio.ensure_future(
        watch_address(
            connect,
            explorer_of(case),
            "bc1p" + "q" * 58,
            lambda: None,
            open_timeout=1,
            sleep=sleep,
        )
    )
    try:
        async with asyncio.timeout(5):
            await ended.wait()
    finally:
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch
    result = connected[0] if connected else "network"
    assert {"asked": asked, "result": result} == case["expected"]


async def test_a_node_that_failed_is_asked_after_mempool_space_for_60_s() -> None:
    """Less than 60 s after the node failed a read, by the call's clock, mempool.space first;
    from 60 s on, the node first again; a failure of mempool.space alone changes nothing."""
    clock = {"now": NOW}
    failures: dict[str, int] = {}
    explorer = CallExplorer(
        Http(FakeHttp(), 1000),
        BitcoinNode(url=NODE, fallback="mempool", payers=False),
        failures,
        lambda: clock["now"],
    )
    names = [source.name for source in explorer.sources()]
    assert names == ["node", "mempool"]
    explorer.node_failed()
    assert failures == {NODE: NOW}
    clock["now"] = NOW + 59
    assert [source.name for source in explorer.sources()] == ["mempool", "node"]
    clock["now"] = NOW + 60
    assert [source.name for source in explorer.sources()] == ["node", "mempool"]

    async def mempool_fails(chain: Explorer) -> str:
        if name_of(chain) == "mempool":
            raise NetworkError("mempool.space did not answer")
        return "node"

    assert await explorer.read(mempool_fails) == "node"
    assert failures == {NODE: NOW}
