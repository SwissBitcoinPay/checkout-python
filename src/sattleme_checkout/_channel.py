"""The integration's channel (SPEC.md §14): what the merchant's app gives the integration."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from . import _json
from ._constants import (
    BUSINESS_NAME_MAX,
    CHANNEL_TYPE,
    CHANNEL_VERSION,
    RECORD_KIND,
    RELAY_MOVES,
    TOLERANCE_MAX,
)
from ._crypto import Secret, is_xonly_point
from ._derive import channel_address, channel_encryption_key
from ._key import IntegrationKey
from ._lnurl import pay_url
from ._log import Logger
from ._node import BitcoinNode, bitcoin_node_of
from ._nostr import Event
from ._records import Newer, NewerFormat, Newest, Opened, newest_copy, open_record
from ._relay import RelayPool
from ._text import is_hex32, is_well_formed, trim_ascii_whitespace
from ._urls import is_checkout_origin, relay_list
from .errors import ChannelNotFoundError, RevokedError


@dataclass(frozen=True, slots=True)
class ActiveChannel:
    business_name: str
    lightning_address: str
    registry_key: Secret
    registry_reader: str
    relays: tuple[str, ...] | None
    checkout_origin: str | None
    # Never shown (SPEC.md §27): a customer who knew it could pay that much less.
    tolerance: int = field(repr=False)
    updated_at: int
    # The merchant's Bitcoin node (SPEC.md §31); None: mempool.space.
    bitcoin_node: BitcoinNode | None = None


@dataclass(frozen=True, slots=True)
class RemovedChannel:
    updated_at: int


Channel = ActiveChannel | RemovedChannel


def tolerance_of(value: object) -> int:
    """A tolerance: an integer of basis points from 0 to 1000; anything else is 0 (none)."""
    number = _json.as_integer(value)
    return number if number is not None and 0 <= number <= TOLERANCE_MAX else 0


def checkout_origin_of(value: object) -> str | None:
    """The checkout origin a channel's `checkout` gives; None for none (SPEC.md §30)."""
    checkout = _json.as_object(value)
    origin = checkout.get("origin") if checkout is not None else None
    return origin if isinstance(origin, str) and is_checkout_origin(origin) else None


def is_business_name(value: object) -> bool:
    """A string not empty once ASCII whitespace is trimmed, of 200 code points at most."""
    return (
        isinstance(value, str)
        and trim_ascii_whitespace(value) != ""
        and len(value) <= BUSINESS_NAME_MAX
        and is_well_formed(value)
    )


def version_of(value: object, known: int) -> int | None:
    """A record value's `version`: the known one, or None; raises NewerFormat above it."""
    if _json.is_number(value) and isinstance(value, int | float) and value > known:
        raise NewerFormat
    number = _json.as_integer(value)
    return number if number == known else None


def parse_channel(value: object, event: Event) -> Channel | None:
    """A channel's value (SPEC.md §14): None when invalid, NewerFormat when newer."""
    channel = _json.as_object(value)
    if channel is None:
        return None
    if version_of(channel.get("version"), CHANNEL_VERSION) is None:
        return None
    if _json.as_integer(channel.get("updatedAt")) != event.created_at:
        return None
    if channel.get("status") == "removed":
        return RemovedChannel(updated_at=event.created_at)
    if channel.get("status") != "active" or channel.get("role") != "integration":
        return None
    profile = _json.as_object(channel.get("profile"))
    if profile is None:
        return None
    business_name = profile.get("businessName")
    lightning_address = profile.get("lightningAddress")
    if not is_business_name(business_name) or not isinstance(business_name, str):
        return None
    if not isinstance(lightning_address, str) or pay_url(lightning_address) is None:
        return None
    registry_key, registry_reader = channel.get("registryKey"), channel.get("registryReader")
    if not is_hex32(registry_key) or not is_hex32(registry_reader):
        return None
    if not is_xonly_point(bytes.fromhex(registry_reader)):
        return None
    relays: tuple[str, ...] | None = None
    if "relays" in channel:
        relays = relay_list(channel["relays"])
        if relays is None:
            return None
    return ActiveChannel(
        business_name=business_name,
        lightning_address=lightning_address,
        registry_key=Secret(bytes.fromhex(registry_key)),
        registry_reader=registry_reader,
        relays=relays,
        checkout_origin=checkout_origin_of(channel.get("checkout")),
        tolerance=tolerance_of(channel.get("onchainTolerance")),
        updated_at=event.created_at,
        # Absent or invalid in any way: none, and the channel stays valid.
        bitcoin_node=bitcoin_node_of(channel.get("bitcoinNode")),
    )


@dataclass(frozen=True, slots=True)
class ChannelRead:
    """The active channel, and the merchant's relays: the channel's, else the key's."""

    channel: ActiveChannel
    relays: tuple[str, ...]


async def lookup_channel(
    pool: RelayPool, key: IntegrationKey, relays: Sequence[str], logger: Logger
) -> Channel | bool:
    """The newest valid channel on `relays`; else whether a copy of a newer format was seen.

    NetworkError when fewer than a majority of them answered.
    """
    address = channel_address(key.secret)
    encryption = channel_encryption_key(key.secret)
    answers: dict[str, list[Opened[Channel] | Newer]] = await pool.read(
        relays,
        {"kinds": [RECORD_KIND], "authors": [key.team], "#d": [address]},
        lambda value: open_record(
            value,
            record_type=CHANNEL_TYPE,
            key=encryption,
            address=address,
            author=key.team,
            parse=parse_channel,
        ),
    )
    newest: Newest[Channel] = newest_copy(copy for copies in answers.values() for copy in copies)
    found = newest.newest
    if newest.newest_newer is not None and (
        found is None or newest.newest_newer > found.event.created_at
    ):
        logger.warning(
            "The integration's channel has a newer format than this version of "
            "sattleme-checkout reads: update it"
        )
    if found is None:
        return newest.newest_newer is not None
    return found.value


async def read_channel(pool: RelayPool, key: IntegrationKey, logger: Logger) -> ChannelRead:
    """The integration's channel, active (SPEC.md §14, Reading the channel).

    Read on the key's relays, then where the channel says the merchant's relays moved, 3 times at
    most: only the new list gets the channel's later changes, its removal included.
    """
    read_on: tuple[str, ...] = key.relays
    found = await lookup_channel(pool, key, read_on, logger)
    if isinstance(found, bool):
        raise ChannelNotFoundError(
            "A majority of the relays answered without the integration's channel",
            newer_format=found,
        )
    channel = found
    for _ in range(RELAY_MOVES):
        if not isinstance(channel, ActiveChannel) or channel.relays is None:
            break
        if channel.relays == read_on:
            break
        target = channel.relays
        moved = await lookup_channel(pool, key, target, logger)
        if isinstance(moved, bool):
            # Not copied there yet: the channel read so far stands.
            break
        channel, read_on = moved, target
    if isinstance(channel, RemovedChannel):
        raise RevokedError("The merchant removed the integration")
    return ChannelRead(channel=channel, relays=channel.relays or key.relays)
