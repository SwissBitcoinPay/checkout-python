"""The integration key (SPEC.md §8) and relay lists (SPEC.md §9)."""

from __future__ import annotations

import pickle
from typing import Any

import pytest

from sattleme_checkout._key import parse_key
from sattleme_checkout._urls import is_relay_url, relay_list
from sattleme_checkout.errors import InvalidKeyError
from spec_vectors import vector

KEYS: Any = vector("integration-keys.json")
RELAYS: Any = vector("relay-urls.json")


@pytest.mark.parametrize("case", KEYS["cases"], ids=lambda c: c["name"])
def test_integration_key(case: dict[str, Any]) -> None:
    expected = case["expected"]
    if "error" in expected:
        with pytest.raises(InvalidKeyError) as raised:
            parse_key(case["key"])
        assert raised.value.code == expected["error"]
        assert case["key"] not in str(raised.value)
        return
    key = parse_key(case["key"])
    assert key.team == expected["teamPublicKey"]
    assert key.secret.reveal().hex() == expected["integrationKey"]
    assert list(key.relays) == expected["relays"]


@pytest.mark.parametrize("case", RELAYS["cases"], ids=lambda c: repr(c["url"]))
def test_relay_url(case: dict[str, Any]) -> None:
    assert is_relay_url(case["url"]) is case["valid"]


def test_relay_lists() -> None:
    assert relay_list(["wss://a.example", "wss://b.example"]) == (
        "wss://a.example",
        "wss://b.example",
    )
    assert relay_list([]) is None
    assert relay_list(["wss://a.example", "wss://a.example"]) is None
    assert relay_list([f"wss://r{i}.example" for i in range(10)]) is not None
    assert relay_list([f"wss://r{i}.example" for i in range(11)]) is None
    assert relay_list("wss://a.example") is None
    assert relay_list([["wss://a.example"]]) is None


def test_relay_address_length_counts_code_points() -> None:
    host = "wss://relay.example/"
    assert is_relay_url(host + "é" * (120 - len(host)))
    assert not is_relay_url(host + "é" * (121 - len(host)))


def test_the_key_never_shows() -> None:
    key = parse_key(KEYS["cases"][0]["key"])
    secret_hex = key.secret.reveal().hex()
    for shown in (repr(key), str(key), f"{key}", repr(key.secret), str(key.secret)):
        assert secret_hex not in shown
        assert "17wserpSaW" not in shown
    with pytest.raises(TypeError):
        pickle.dumps(key)
    with pytest.raises(TypeError):
        pickle.dumps(key.secret)


def test_a_refused_key_leaves_no_trace_in_the_traceback() -> None:
    text = KEYS["cases"][0]["key"][: -len("cmVsYXkuZXhhbXBsZQ")] + "x"
    with pytest.raises(InvalidKeyError) as raised:
        parse_key(text)
    tb = raised.value.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename.endswith("_key.py"):
            assert text not in tb.tb_frame.f_locals.values()
        tb = tb.tb_next
