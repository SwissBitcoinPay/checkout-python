"""The merchant's Bitcoin node (SPEC.md §31, The merchant's Bitcoin node), end to end against local
relays and fakes of the Lightning address, of mempool.space and of a node on the merchant's network
(PORTING.md, Steps, 5): every read of an address and of the chain's height, and wait_for_payment's
WebSocket, asked of the node, and never of mempool.space while it answers; its `fallback` when it
does not; the 60 s after a failure; a check's two reads of one explorer; an invalid `bitcoinNode`
read as none; the node in a checkout link's record."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import secrets
from typing import Any

import pytest

from fakes.chain import deposit_address, tx, txid
from fakes.wait import until
from fakes.world import INTERNET_NODE, NODE, NODE_WEBSOCKET, World
from sattleme_checkout import InvoicePendingError, NetworkError, PaymentStatus
from sattleme_checkout._checkout import Fragment, parse_fragment, read_link_value
from sattleme_checkout._constants import MEMPOOL_WEBSOCKET
from sattleme_checkout._crypto import Secret
from sattleme_checkout._derive import link_keys
from sattleme_checkout._http import USER_AGENT
from sattleme_checkout._nip44 import decrypt

ONCE = {"url": NODE, "fallback": "mempool", "payers": False}
NEVER_MEMPOOL = {"url": NODE, "fallback": "none", "payers": False}


def rfc3339(seconds: float) -> str:
    """A time as BlockCypher writes it."""
    moment = datetime.datetime.fromtimestamp(seconds, datetime.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def record_of(world: World, checkout_url: str | None) -> dict[str, Any]:
    """What a link's record holds, as its page opens it."""
    assert checkout_url is not None
    fragment = parse_fragment(checkout_url.split("#", 1)[1])
    assert isinstance(fragment, Fragment)
    keys = link_keys(Secret(bytes.fromhex(fragment.link_key)))
    [event] = [e for e in world.received() if ["d", keys.address] in e["tags"]]
    value: dict[str, Any] = json.loads(decrypt(event["content"], keys.encryption_key))["v"]
    return value


def node_asked(world: World) -> list[str]:
    return world.http.urls(f"{NODE}/")


def mempool_asked(world: World) -> list[str]:
    """What mempool.space's explorer was asked (its rate source left out)."""
    return [
        url
        for url in world.http.urls("https://mempool.space/api/")
        if not url.startswith("https://mempool.space/api/v1/prices")
    ]


def explorers_asked(world: World, since: int = 0) -> list[str]:
    """Each explorer request since the `since`-th HTTP request: `node` or `mempool`, and what."""
    asked = []
    for url in world.http.urls()[since:]:
        if url.startswith(f"{NODE}/api/"):
            asked.append("node " + url.removeprefix(f"{NODE}/api/"))
        elif url in mempool_asked(world):
            asked.append("mempool " + url.removeprefix("https://mempool.space/api/"))
    return asked


async def test_a_node_that_answers_is_the_only_explorer_asked() -> None:
    """The lease and every check: the node, with the User-Agent of every request; paid at 3
    confirmations on it."""
    address = deposit_address()
    world = await World.start(lot=[address], bitcoin_node=ONCE)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="N-1", sats=49_000)
        assert invoice.onchain_address == address
        # The lease: whether the address was paid already, of the node.
        assert node_asked(world) == [f"{NODE}/api/address/{address}/txs"]
        paying = txid()
        world.explorer.add(address, tx(paying, {address: 49_000}))
        assert (await client.get_status(invoice)).state == "unconfirmed"
        world.explorer.confirm(paying, world.explorer.height - 2)
        status = await client.get_status(invoice)
        assert status.to_json() == {
            "state": "paid",
            "method": "onchain",
            "received": "49000",
            "txids": [paying],
        }
        assert mempool_asked(world) == []
        assert [url for url in node_asked(world) if url.endswith("/blocks/tip/height")] == [
            f"{NODE}/api/blocks/tip/height"
        ] * 2
        node_requests = [a for a in world.http.asked if a.url.startswith(f"{NODE}/")]
        assert {a.headers.get("User-Agent") for a in node_requests} == {USER_AGENT}
    finally:
        await world.close()


async def test_a_redirect_of_the_node_is_a_failure_and_mempool_space_is_asked() -> None:
    """SPEC.md §15: no redirect is followed; a redirect is a failure of the node."""
    world = await World.start(bitcoin_node=ONCE)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="N-2", sats=49_000)
        before = len(world.http.asked)
        world.http.route(f"{NODE}/api/", lambda _url: (302, b""))
        assert (await client.get_status(invoice)).state == "open"
        assert sorted(explorers_asked(world, before)) == sorted(
            [
                "mempool blocks/tip/height",
                f"mempool address/{invoice.onchain_address}/txs",
                "node blocks/tip/height",
                f"node address/{invoice.onchain_address}/txs",
            ]
        )
    finally:
        await world.close()


async def test_fallback_mempool_reads_mempool_space_and_network_when_both_fail() -> None:
    address = deposit_address()
    world = await World.start(lot=[address], bitcoin_node=ONCE)
    try:
        # A server outside the merchant's network: no answer of the node.
        world.http.down.add(f"{NODE}/")
        client = world.client()
        invoice = await client.create_invoice(ref="F-1", sats=49_000)
        assert invoice.onchain_address == address
        assert explorers_asked(world) == [
            f"node address/{address}/txs",
            f"mempool address/{address}/txs",
        ]
        world.explorer.add(address, tx(txid(), {address: 49_000}, height=world.explorer.height - 2))
        status = await client.get_status(invoice)
        assert (status.state, status.method) == ("paid", "onchain")
        world.http.down.add("https://mempool.space/api/")
        with pytest.raises(NetworkError):
            await client.get_status(invoice)
    finally:
        await world.close()


async def test_a_node_that_failed_is_asked_after_mempool_space_for_60_s_in_the_call() -> None:
    """Two addresses, the first paid already (skipped), the second leased: the node fails the
    first's read, so the second's is asked of mempool.space first (SPEC.md §31, `fallback`)."""
    first, second = deposit_address(), deposit_address()
    world = await World.start(lot=[first, second], bitcoin_node=ONCE)
    try:
        world.http.down.add(f"{NODE}/")
        world.explorer.add(first, tx(txid(), {first: 1000}, height=1))

        def random(size: int) -> bytes:
            # The shuffle's draws keep the lot's order: the paid one first.
            return b"\xff" * 4 if size == 4 else secrets.token_bytes(size)

        invoice = await world.client(random=random).create_invoice(ref="F-2", sats=49_000)
        assert invoice.onchain_address == second
        assert explorers_asked(world) == [
            f"node address/{first}/txs",
            f"mempool address/{first}/txs",
            f"mempool address/{second}/txs",
        ]
        # Another call: the node first again.
        before = len(world.http.asked)
        await world.client().get_status(invoice)
        assert explorers_asked(world, before)[:2] == [
            f"node address/{second}/txs",
            "node blocks/tip/height",
        ]
    finally:
        await world.close()


async def test_a_checks_two_reads_come_from_one_explorer() -> None:
    """The node gives the address's transactions and fails the chain's height: both are asked
    of mempool.space, never one from each."""
    world = await World.start(bitcoin_node=ONCE)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="O-1", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        world.explorer.add(address, tx(txid(), {address: 49_000}, height=world.explorer.height - 2))
        world.http.route(f"{NODE}/api/blocks/tip/height", lambda _url: (500, b"down"))
        before = len(world.http.asked)
        assert (await client.get_status(invoice)).state == "paid"
        assert sorted(explorers_asked(world, before)) == sorted(
            [
                "mempool blocks/tip/height",
                f"mempool address/{address}/txs",
                "node blocks/tip/height",
                f"node address/{address}/txs",
            ]
        )
    finally:
        await world.close()


async def test_fallback_none_leases_no_address_while_the_node_is_down() -> None:
    """`network` for an order paid on-chain only, a Lightning invoice alone otherwise;
    mempool.space never asked."""
    world = await World.start(bitcoin_node=NEVER_MEMPOOL)
    try:
        world.http.down.add(f"{NODE}/")
        world.lightning.max_sendable = 1_000_000
        with pytest.raises(NetworkError):
            await world.client().create_invoice(ref="X-1", sats=2_000_000)
        world.lightning.max_sendable = 4_000_000_000
        invoice = await world.client().create_invoice(ref="X-2", sats=49_000)
        assert invoice.onchain_address is None
        assert invoice.bolt11 is not None
        assert mempool_asked(world) == []
        assert node_asked(world) != []
    finally:
        await world.close()


async def test_fallback_none_is_network_never_expired_then_paid_once_the_node_answers() -> None:
    world = await World.start(bitcoin_node=NEVER_MEMPOOL)
    try:
        world.lightning.max_sendable = 1_000_000
        client = world.client()
        invoice = await client.create_invoice(ref="X-3", sats=2_000_000)
        address = invoice.onchain_address
        assert address is not None
        world.http.down.add(f"{NODE}/")
        # Paid while the lease ran, which no check saw: BlockCypher tells it in time.
        paying = txid()
        world.explorer.add(
            address, tx(paying, {address: 2_000_000}, height=world.explorer.height - 5)
        )
        world.blockcypher.received[paying] = rfc3339(invoice.created_at + 60)
        world.now = invoice.expires_at + 3600
        for _ in range(2):
            with pytest.raises(NetworkError):
                await client.get_status(invoice)
            # A new invoice of the order: refused while its earlier one cannot be told.
            with pytest.raises(NetworkError):
                await client.create_invoice(ref="X-3", sats=2_000_000)
            world.now += 86_400
        assert mempool_asked(world) == []
        world.http.down.discard(f"{NODE}/")
        status = await client.get_status(invoice)
        assert status.to_json() == {
            "state": "paid",
            "method": "onchain",
            "received": "2000000",
            "txids": [paying],
        }
    finally:
        await world.close()


@pytest.mark.parametrize(
    "bitcoin_node",
    [
        pytest.param(
            {"url": "http://node.example", "fallback": "mempool", "payers": False},
            id="http to a public host",
        ),
        pytest.param(
            {"url": NODE, "fallback": "Mempool", "payers": False},
            id="a fallback of another spelling",
        ),
        pytest.param({"url": NODE, "fallback": "none"}, id="payers absent"),
        pytest.param(
            {"url": INTERNET_NODE, "fallback": "none", "payers": "true"},
            id="payers not a boolean",
        ),
        pytest.param(
            {"url": NODE, "fallback": "none", "payers": True}, id="payers true for a local node"
        ),
        pytest.param(None, id="null"),
    ],
)
async def test_an_invalid_bitcoin_node_reads_mempool_space(bitcoin_node: Any) -> None:
    """The channel still valid; the node never asked."""
    address = deposit_address()
    world = await World.start(lot=[address], bitcoin_node=bitcoin_node)
    try:
        world.http.route("http://node.example/api/", world.explorer.at("http://node.example/api"))
        world.http.route(f"{INTERNET_NODE}/api/", world.explorer.at(f"{INTERNET_NODE}/api"))
        client = world.client()
        invoice = await client.create_invoice(ref="I-1", sats=49_000, checkout=True)
        assert invoice.onchain_address == address
        assert "bitcoinNode" not in record_of(world, invoice.checkout_url)
        status: PaymentStatus = await client.get_status(invoice)
        assert status.state == "open"
        assert sorted(explorers_asked(world)) == sorted(
            [
                "mempool blocks/tip/height",
                f"mempool address/{address}/txs",
                f"mempool address/{address}/txs",
            ]
        )
        assert not [u for u in world.http.urls() if "node.example" in u or u.startswith(NODE)]
    finally:
        await world.close()


async def test_a_wait_watches_the_nodes_websocket_never_mempool_spaces() -> None:
    world = await World.start(bitcoin_node=ONCE)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="W-1", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        heard: list[PaymentStatus] = []
        waiting = asyncio.ensure_future(
            client.wait_for_payment(invoice, interval_ms=60_000, on_status=heard.append)
        )
        await until(lambda: world.node_socket.tracked() == [address] and bool(heard))
        assert world.node_socket.messages == [
            {"action": "want", "data": ["blocks"]},
            {"track-address": address},
        ]
        transaction = tx(txid(), {address: 49_000}, height=world.explorer.height - 2)
        world.explorer.add(address, transaction)
        await world.node_socket.push({"address-transactions": [transaction]})
        status = await asyncio.wait_for(waiting, 10)
        assert (status.state, status.method) == ("paid", "onchain")
        assert world.socket.opened == 0
        assert MEMPOOL_WEBSOCKET not in world.connections
        assert mempool_asked(world) == []
    finally:
        await world.close()


@pytest.mark.parametrize("failure", ["refused", "lost before any message"])
async def test_fallback_mempool_watches_mempool_spaces_websocket_when_the_nodes_fails(
    failure: str,
) -> None:
    world = await World.start(bitcoin_node=ONCE)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="W-2", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        if failure == "refused":
            world.http.down.add(f"{NODE}/")
        else:
            # Accepted, then closed: the REST reads of the node still answer.
            world.node_socket.refuse = True
        heard: list[PaymentStatus] = []
        waiting = asyncio.ensure_future(
            client.wait_for_payment(invoice, interval_ms=60_000, on_status=heard.append)
        )
        # The node's WebSocket fails: mempool.space's at once.
        await until(lambda: world.socket.tracked() == [address] and bool(heard))
        assert world.connections[NODE_WEBSOCKET] == 1
        transaction = tx(txid(), {address: 49_000}, height=world.explorer.height - 2)
        world.explorer.add(address, transaction)
        await world.socket.push({"address-transactions": [transaction]})
        status = await asyncio.wait_for(waiting, 10)
        assert (status.state, status.method) == ("paid", "onchain")
    finally:
        await world.close()


async def test_fallback_none_watches_the_nodes_websocket_alone_again_while_it_is_down() -> None:
    world = await World.start(bitcoin_node=NEVER_MEMPOOL)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="W-3", sats=49_000)
        world.http.down.add(f"{NODE}/")
        waiting = asyncio.ensure_future(client.wait_for_payment(invoice, interval_ms=250))
        # Refused, tried again after 1 s.
        await until(lambda: world.connections.get(NODE_WEBSOCKET, 0) >= 2)
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting
        assert world.socket.opened == 0
        assert MEMPOOL_WEBSOCKET not in world.connections
        assert mempool_asked(world) == []
    finally:
        await world.close()


async def test_a_wait_asks_the_node_after_mempool_space_within_60_s_of_its_failure() -> None:
    """In every check of one wait_for_payment, and its WebSocket: one call (SPEC.md §31)."""
    world = await World.start(bitcoin_node=ONCE)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="W-4", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        world.http.down.add(f"{NODE}/")
        heard: list[PaymentStatus] = []
        mark = len(world.http.asked)

        def asked() -> list[str]:
            nonlocal mark
            kinds = [kind.split(" ")[0] for kind in explorers_asked(world, mark)]
            mark = len(world.http.asked)
            return kinds

        waiting = asyncio.ensure_future(
            client.wait_for_payment(invoice, interval_ms=60_000, on_status=heard.append)
        )
        # The first check: the node, then mempool.space. Its WebSocket: the node's refused,
        # mempool.space's connected, which starts a check, of mempool.space first.
        await until(lambda: len(heard) == 2)
        assert asked() == ["node", "node", "mempool", "mempool", "mempool", "mempool"]
        assert world.connections[NODE_WEBSOCKET] == 1
        # mempool.space failing within the 60 s: the node after it, which answers.
        world.http.down.discard(f"{NODE}/")
        world.http.down.add("https://mempool.space/api/")
        await world.socket.push({"block": {"height": world.explorer.height}})
        await until(lambda: len(heard) == 3)
        assert asked() == ["mempool", "mempool", "node", "node"]
        assert heard[-1].state == "open"
        # 60 s after its failure: the node first again.
        world.now += 60
        world.http.down.discard("https://mempool.space/api/")
        await world.socket.push({"block": {"height": world.explorer.height}})
        await until(lambda: len(heard) == 4)
        assert asked() == ["node", "node"]
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting
    finally:
        await world.close()


async def test_a_waits_websocket_follows_the_node_the_last_channel_read_names() -> None:
    """The merchant names a node while the wait runs: the next check reads it, and the wait
    watches the node's WebSocket instead of mempool.space's."""
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="W-5", sats=49_000)
        address = invoice.onchain_address
        assert address is not None
        heard: list[PaymentStatus] = []
        waiting = asyncio.ensure_future(
            client.wait_for_payment(invoice, interval_ms=60_000, on_status=heard.append)
        )
        await until(lambda: world.socket.tracked() == [address] and bool(heard))
        world.publish_channel(bitcoin_node=ONCE)
        await world.socket.push({"block": {"height": world.explorer.height}})
        await until(lambda: world.node_socket.tracked() == [address])
        # mempool.space's closed.
        await until(lambda: not world.socket.connections)
        transaction = tx(txid(), {address: 49_000}, height=world.explorer.height - 2)
        world.explorer.add(address, transaction)
        await world.node_socket.push({"address-transactions": [transaction]})
        status = await asyncio.wait_for(waiting, 10)
        assert (status.state, status.method) == ("paid", "onchain")
        assert (world.connections[MEMPOOL_WEBSOCKET], world.connections[NODE_WEBSOCKET]) == (1, 1)
    finally:
        await world.close()


async def test_a_links_record_holds_the_nodes_url_and_fallback_with_payers_true() -> None:
    """Never `payers`, nor the channel's other members; a new link for invoice_pending too."""
    node = {"url": INTERNET_NODE, "fallback": "none", "payers": True, "name": "Shop node"}
    world = await World.start(bitcoin_node=node)
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="L-1", sats=49_000, checkout=True)
        record = record_of(world, invoice.checkout_url)
        assert (record["version"], record["onchainAddress"]) == (2, invoice.onchain_address)
        assert record["bitcoinNode"] == {"url": INTERNET_NODE, "fallback": "none"}
        assert list(record)[-1] == "bitcoinNode"
        shown = read_link_value(record)
        assert shown is not None
        assert shown.bitcoin_node is not None
        assert shown.bitcoin_node.to_json() == {"url": INTERNET_NODE, "fallback": "none"}
        # The link made again for invoice_pending holds it too.
        with pytest.raises(InvoicePendingError) as pending:
            await client.create_invoice(ref="L-1", sats=49_000, checkout=True)
        again = pending.value.checkout_url
        assert again is not None
        assert again != invoice.checkout_url
        assert record_of(world, again)["bitcoinNode"] == {"url": INTERNET_NODE, "fallback": "none"}
    finally:
        await world.close()


@pytest.mark.parametrize(
    "bitcoin_node",
    [
        pytest.param(
            {"url": INTERNET_NODE, "fallback": "mempool", "payers": False}, id="payers false"
        ),
        pytest.param({"url": NODE, "fallback": "mempool", "payers": False}, id="a local node"),
        pytest.param(None, id="no node"),
    ],
)
async def test_a_links_record_holds_no_node_without_payers(bitcoin_node: Any) -> None:
    world = await World.start(bitcoin_node=bitcoin_node)
    try:
        world.http.route(f"{INTERNET_NODE}/api/", world.explorer.at(f"{INTERNET_NODE}/api"))
        invoice = await world.client().create_invoice(ref="L-2", sats=49_000, checkout=True)
        record = record_of(world, invoice.checkout_url)
        assert record["version"] == 2
        assert "bitcoinNode" not in record
    finally:
        await world.close()


async def test_a_links_record_of_version_1_holds_no_node() -> None:
    """An order without an address (below the on-chain floor): Lightning only."""
    world = await World.start(
        bitcoin_node={"url": INTERNET_NODE, "fallback": "mempool", "payers": True}
    )
    try:
        invoice = await world.client().create_invoice(ref="L-3", sats=999, checkout=True)
        assert invoice.onchain_address is None
        record = record_of(world, invoice.checkout_url)
        assert record["version"] == 1
        assert "bitcoinNode" not in record
    finally:
        await world.close()
