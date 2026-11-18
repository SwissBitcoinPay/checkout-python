"""What the relays hold of a merchant's registry, opened as the merchant's app or a sale opens it:
for the integration tests to check what an implementation wrote."""

from __future__ import annotations

from typing import Any

from fakes.world import World
from sattleme_checkout._derive import (
    channel_encryption_key,
    onchain_registry_address,
    seen_keys,
)
from sattleme_checkout._lease import open_sale_record
from sattleme_checkout._records import Opened
from sattleme_checkout._witness import open_witness


def leases_of(world: World, ref: str, address: str) -> list[Any]:
    """The sale's records at the registry address of `address`, newest first, opened."""
    sale = world.sale(ref)
    registry_address = onchain_registry_address(world.merchant.registry_key, address)
    found = []
    ids: set[str] = set()
    for url in world.relay_urls:
        for event in world.held(url, sale.author):
            if event.tag("d") == registry_address and event.id not in ids:
                ids.add(event.id)
                opened = open_sale_record(
                    event.to_json(),
                    sale=sale,
                    registry=world.merchant.registry,
                    registry_address=registry_address,
                )
                assert isinstance(opened, Opened)
                found.append(opened)
    return sorted(found, key=lambda o: -o.event.created_at)


def taken_records(world: World, ref: str, address: str) -> list[Any]:
    """Every record of the sale at the registry address of `address` the relays took, oldest
    first, opened."""
    sale = world.sale(ref)
    registry_address = onchain_registry_address(world.merchant.registry_key, address)
    found = []
    for value in world.received():
        if value["pubkey"] == sale.author and ["d", registry_address] in value["tags"]:
            opened = open_sale_record(
                value,
                sale=sale,
                registry=world.merchant.registry,
                registry_address=registry_address,
            )
            assert isinstance(opened, Opened)
            found.append(opened)
    return sorted(found, key=lambda o: o.event.created_at)


def witnesses_of(world: World) -> list[Any]:
    seen = seen_keys(world.merchant.key.secret)
    key = channel_encryption_key(world.merchant.key.secret)
    found: dict[str, Any] = {}
    for url in world.relay_urls:
        for event in world.held(url, seen.author):
            opened = open_witness(event.to_json(), seen, key)
            assert isinstance(opened, Opened)
            found[opened.event.id] = opened.value
    return list(found.values())
