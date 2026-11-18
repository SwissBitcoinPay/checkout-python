"""The integration's lot of deposit addresses (SPEC.md §31, The lot)."""

from __future__ import annotations

from . import _json
from ._address import is_deposit_address
from ._channel import version_of
from ._constants import LOT_SIZE_MAX, LOT_VERSION
from ._nostr import Event


def parse_lot(value: object, event: Event) -> tuple[str, ...] | None:
    """A lot's addresses; None when invalid; NewerFormat when of a newer format."""
    lot = _json.as_object(value)
    if lot is None:
        return None
    if version_of(lot.get("version"), LOT_VERSION) is None:
        return None
    if _json.as_integer(lot.get("updatedAt")) != event.created_at:
        return None
    addresses = lot.get("addresses")
    if not isinstance(addresses, list) or len(addresses) > LOT_SIZE_MAX:
        return None
    if not all(is_deposit_address(address) for address in addresses):
        return None
    if len(set(addresses)) != len(addresses):
        return None
    return tuple(addresses)
