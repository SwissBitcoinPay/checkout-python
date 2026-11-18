"""Nostr events' ids (SPEC.md §11) against nostr-events.json: their serialization, numbers
written as integers whatever their spelling, strings as `JSON.stringify` writes them."""

from __future__ import annotations

from typing import Any

import pytest

from sattleme_checkout._nostr import hashed_event, id_of, members_of, serialization
from spec_vectors import vector

EVENTS: Any = vector("nostr-events.json")


@pytest.mark.parametrize("case", EVENTS["cases"], ids=lambda c: c["name"])
def test_an_events_id(case: dict[str, Any]) -> None:
    """The id the check of an event computes (`id_of`, as `hashed_event` does), and the
    serialization it hashes."""
    event = case["event"]
    members = members_of(event)
    assert members is not None
    assert serialization(*members) == case["serialized"]
    assert id_of(event) == case["id"]
    # The check of what a relay sends: that id is its own, any other is not.
    signed = {**event, "id": case["id"], "sig": "00" * 64}
    checked = hashed_event(signed)
    assert checked is not None
    assert checked.created_at == members[1]
    assert hashed_event({**signed, "id": "00" * 32}) is None
