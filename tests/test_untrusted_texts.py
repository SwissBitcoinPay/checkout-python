"""Texts from relays, answers and stored invoices: refused when they are not well-formed (no lone
surrogate, SPEC.md §6) or too long to read, never a crash later."""

from __future__ import annotations

from typing import Any

from sattleme_checkout._channel import is_business_name, parse_channel
from sattleme_checkout._checkout import read_link_value
from sattleme_checkout._nostr import Event
from sattleme_checkout._stored import sats_of
from sattleme_checkout._urls import is_relay_url, relay_list
from spec_vectors import vector

LONE = chr(0xD800)


def _event(created_at: int) -> Event:
    return Event(
        id="0" * 64, pubkey="0" * 64, created_at=created_at, kind=30078, tags=(), content="", sig=""
    )


def _channel(**changes: Any) -> dict[str, Any]:
    # The channel of the app's vector (its keys: no key but the vectors' is in the repository).
    value: dict[str, Any] = dict(vector("app/app.json")["channel"]["value"]["v"])
    value["updatedAt"] = 1000
    value.pop("relays", None)
    value.update(changes)
    return value


def test_a_relay_address_with_a_lone_surrogate() -> None:
    assert is_relay_url("wss://relay.example/café")
    assert not is_relay_url("wss://relay.example/caf" + LONE)
    assert relay_list(["wss://relay.example/" + LONE]) is None


def test_a_channel_with_a_lone_surrogate() -> None:
    assert parse_channel(_channel(), _event(1000)) is not None
    assert not is_business_name("Sunrise " + LONE)
    named = _channel(profile={"businessName": LONE, "lightningAddress": "shop@breez.tips"})
    assert parse_channel(named, _event(1000)) is None
    assert parse_channel(_channel(relays=["wss://relay.example/" + LONE]), _event(1000)) is None


def test_a_checkout_record_with_a_lone_surrogate() -> None:
    record = dict(vector("app/checkout-link.json")["value"])
    assert read_link_value(record) is not None
    record["note"] = "Order " + LONE
    assert read_link_value(record) is None


def test_an_amount_of_many_zeros() -> None:
    """SPEC.md §22: 1 to 16 digits, a leading zero allowed; more is none, whatever its value
    (and never an `int` of 5 000 digits, which Python refuses)."""
    assert sats_of("0" * 11 + "49000") == 49_000
    assert sats_of("0" * 12 + "49000") is None
    assert sats_of("0" * 5000 + "49000") is None
    assert sats_of("0" * 5000) is None
    assert sats_of("1" * 17) is None
