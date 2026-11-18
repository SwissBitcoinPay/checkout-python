"""The merchant's Bitcoin node (SPEC.md §31, The merchant's Bitcoin node).

A node of the merchant's own that serves mempool.space's API (an Umbrel, a server of its own),
which the channel names (`bitcoinNode`, SPEC.md §14): the server then reads the blockchain from it
rather than from mempool.space. Its URL follows a rule of its own, not the https URL's (SPEC.md
§15): `http` is allowed for a host of a local network, and so is an IPv4 address. Nothing is
repaired: a URL that is not in its one spelling is no node URL, and the channel then names none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypeGuard

from . import _json
from ._constants import HOST_MAX, NODE_URL_MAX

Fallback = Literal["mempool", "none"]
"""What the server does when the node does not answer a read: `"mempool"`, the same read of
mempool.space; `"none"`, never mempool.space."""

# `<scheme>://<host>[:<port>][<path>]`: the host up to `:` or `/` (nothing that may start a user
# name, a query or a fragment), a port of 1 to 5 digits without a leading zero, a path of visible
# ASCII. The parts are then checked one by one.
_NODE_URL = re.compile(r"(https?)://([^/:?#@\\\s]+)(?::([1-9][0-9]{0,4}))?(/[\x21-\x7e]*)?")
# An IPv4 address: 4 numbers from 0 to 255, none with a leading zero (`0` alone is one).
_OCTET = "(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_IPV4 = re.compile(rf"{_OCTET}\.{_OCTET}\.{_OCTET}\.{_OCTET}")
# A label of a name: 1 to 63 of `a-z 0-9 -`, neither starting nor ending with `-`.
_LABEL = re.compile("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PATH_REFUSED = re.compile(r"[?#@\\]")


@dataclass(frozen=True, slots=True)
class NodeUrl:
    """A node URL, checked, and where the node is read."""

    url: str
    # `<url>/api`: the `<explorer>` of SPEC.md §31, Explorers.
    rest: str
    # `<url>` with `ws` for its leading `http`, then `/api/v1/ws`.
    web_socket: str
    # A local range of IPv4 addresses, or a name under `.local`.
    local: bool
    # `https`, and a name whose last label is not `local`: what a customer's browser may read.
    internet: bool


@dataclass(frozen=True, slots=True)
class BitcoinNode:
    """The channel's `bitcoinNode`, valid."""

    url: str
    fallback: Fallback
    # Whether the pages of checkout links read the node too (a node of the Internet only).
    payers: bool


@dataclass(frozen=True, slots=True)
class RecordNode:
    """The node a checkout link's record names (SPEC.md §30): its URL and its fallback."""

    url: str
    fallback: Fallback

    def to_json(self) -> dict[str, str]:
        return {"url": self.url, "fallback": self.fallback}


def _ipv4(host: str) -> tuple[int, int, int, int] | None:
    match = _IPV4.fullmatch(host)
    if match is None:
        return None
    a, b, c, d = (int(match.group(i)) for i in range(1, 5))
    return a, b, c, d


def _is_local_ipv4(address: tuple[int, int, int, int]) -> bool:
    """10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10 (RFC 6598, which Tailscale
    uses)."""
    a, b = address[0], address[1]
    return (
        a == 10
        or (a == 172 and 16 <= b <= 31)
        or (a == 192 and b == 168)
        or (a == 100 and 64 <= b <= 127)
    )


def _is_name(host: str) -> bool:
    """2 labels or more, the last starting with a letter, never `localhost` nor under it; 253
    characters at most."""
    if len(host) > HOST_MAX:
        return False
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return False
    return "a" <= labels[-1][0] <= "z" and labels[-1] != "localhost"


def _is_node_path(path: str) -> bool:
    """Empty, or `/` and visible ASCII but `?`, `#`, `@` and `\\`, ending neither with `/` nor
    with `/api` (ignoring ASCII case): the node's URL, not its API's."""
    if path == "":
        return True
    if _PATH_REFUSED.search(path) is not None:
        return False
    return not path.endswith("/") and path[-4:].lower() != "/api"


def parse_node_url(value: object) -> NodeUrl | None:
    """`value` as a node URL (SPEC.md §31, A node URL), with where the node is read; None when it
    is not one. `https`, or `http` for a local host; a host in lowercase, an IPv4 address (never
    in 0.0.0.0/8 nor 127.0.0.0/8) or a name (never `localhost` nor under it); a port from 1 to
    65535; a path ending neither with `/` nor with `/api`; 200 characters at most. So no user
    name nor password, no query, no fragment, no IPv6 address, no whitespace."""
    if not isinstance(value, str) or len(value) > NODE_URL_MAX:
        return None
    match = _NODE_URL.fullmatch(value)
    if match is None:
        return None
    scheme, host, port, path = match.group(1), match.group(2), match.group(3), match.group(4)
    if port is not None and int(port) > 65535:
        return None
    if not _is_node_path(path or ""):
        return None
    address = _ipv4(host)
    if address is not None:
        # Never the machine itself, nor "this network".
        if address[0] in (0, 127):
            return None
        local = _is_local_ipv4(address)
    else:
        if not _is_name(host):
            return None
        local = host.endswith(".local")
    if scheme == "http" and not local:
        return None
    return NodeUrl(
        url=value,
        rest=f"{value}/api",
        web_socket=f"ws{value[len('http') :]}/api/v1/ws",
        local=local,
        internet=scheme == "https" and address is None and not local,
    )


def _is_fallback(value: object) -> TypeGuard[Fallback]:
    return isinstance(value, str) and value in ("mempool", "none")


def bitcoin_node_of(value: object) -> BitcoinNode | None:
    """The channel's `bitcoinNode` (SPEC.md §14, §31): an object whose `url` is a node URL, whose
    `fallback` is exactly `"mempool"` or `"none"` and whose `payers` is a boolean, true only for a
    node of the Internet; its other members ignored. None when it is absent or invalid in any
    way: there is none, the server reads mempool.space, and the channel stays valid."""
    node = _json.as_object(value)
    if node is None:
        return None
    url, fallback, payers = node.get("url"), node.get("fallback"), node.get("payers")
    parsed = parse_node_url(url)
    if parsed is None or not _is_fallback(fallback) or not isinstance(payers, bool):
        return None
    # A link must never make each customer's browser probe its own local network.
    if payers and not parsed.internet:
        return None
    return BitcoinNode(url=parsed.url, fallback=fallback, payers=payers)


def record_node_for(node: BitcoinNode | None) -> RecordNode | None:
    """What a checkout link's record of version 2 holds of the channel's node (SPEC.md §30): its
    `url` and `fallback`, copied as they are, never `payers`; None (the field absent) when the
    channel names no node or its `payers` is false."""
    if node is None or not node.payers:
        return None
    return RecordNode(url=node.url, fallback=node.fallback)


def record_node_of(value: object) -> RecordNode | None:
    """The node a page reads from a record of version 2's `bitcoinNode` (SPEC.md §30, Reading a
    record): an object whose `url` is a node URL of a node of the Internet and whose `fallback`
    is exactly `"mempool"` or `"none"` (its other members ignored); anything else (null included)
    is set aside, the page reading mempool.space, and leaves the record valid."""
    node = _json.as_object(value)
    if node is None:
        return None
    url, fallback = node.get("url"), node.get("fallback")
    parsed = parse_node_url(url)
    if parsed is None or not parsed.internet or not _is_fallback(fallback):
        return None
    return RecordNode(url=parsed.url, fallback=fallback)
