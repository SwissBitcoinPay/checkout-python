"""BOLT11 (SPEC.md §19), bech32 and bech32m, deposit addresses and payment URIs (SPEC.md §31)."""

from __future__ import annotations

from typing import Any

import pytest

from sattleme_checkout import _bech32, _bolt11
from sattleme_checkout._address import btc_amount, is_deposit_address, payment_uri
from sattleme_checkout._crypto import Secret
from sattleme_checkout._derive import onchain_registry_address
from spec_vectors import vector

BOLT11: Any = vector("bolt11.json")
ADDRESSES: Any = vector("onchain-addresses.json")


def _fields(decoded: _bolt11.Bolt11 | None) -> dict[str, Any] | None:
    if decoded is None:
        return None
    return {
        "network": decoded.network,
        "amountMsat": None if decoded.amount_msat is None else str(decoded.amount_msat),
        "timestamp": decoded.timestamp,
        "expirySeconds": decoded.expiry,
        "paymentHash": decoded.payment_hash,
        "descriptionHash": decoded.description_hash,
    }


@pytest.mark.parametrize("case", BOLT11["cases"], ids=lambda c: c["name"])
def test_bolt11(case: dict[str, Any]) -> None:
    assert _fields(_bolt11.decode(case["invoice"])) == case["expected"]


def _words(value: int, count: int) -> list[int]:
    return [(value >> 5 * (count - 1 - i)) & 31 for i in range(count)]


def _field(tag: int, words: list[int]) -> list[int]:
    """A tagged field; its length is two words, so a field holds 1023 words at most."""
    assert len(words) <= 1023
    return [tag, len(words) // 32, len(words) % 32, *words]


def _description(count: int) -> list[int]:
    """`count` words of description, in as many fields as they take."""
    fields: list[int] = []
    while count > 0:
        fields += _field(13, [7] * min(count, 1023))
        count -= 1023
    return fields


def _invoice(description_words: int) -> str:
    """An invoice with a long description field (BOLT11 has no length limit); never signed."""
    hash_words = _bech32.convert_bits(list(bytes(range(32))), 8, 5, pad=True)
    assert hash_words is not None and len(hash_words) == 52
    words = (
        _words(1791378000, 7)
        + _field(1, hash_words)
        + _description(description_words)
        + _field(6, _words(600, 2))
        + [0] * 104
    )
    return _bech32.encode("lnbc10u", words, _bech32.Encoding.BECH32)


@pytest.mark.parametrize("description_words", [10, 900, 1023, 1500, 4000])
def test_invoices_of_any_length(description_words: int) -> None:
    invoice = _invoice(description_words)
    assert len(invoice) > description_words
    decoded = _bolt11.decode(invoice)
    assert decoded is not None
    assert decoded.amount_msat == 1_000_000
    assert decoded.expiry == 600
    assert decoded.payment_hash == bytes(range(32)).hex()
    assert _bolt11.decode(invoice.upper()) == decoded


def test_an_invoice_longer_than_1023_characters() -> None:
    invoice = _invoice(1200)
    assert len(invoice) > 1023
    assert _bolt11.decode(invoice) is not None
    # One character changed: the checksum fails, whatever the length.
    assert _bolt11.decode(invoice[:-1] + ("q" if invoice[-1] != "q" else "p")) is None


def test_bech32_and_bech32m_are_never_taken_one_for_the_other() -> None:
    words = [1, 2, 3, 4]
    plain = _bech32.encode("bc", words, _bech32.Encoding.BECH32)
    modified = _bech32.encode("bc", words, _bech32.Encoding.BECH32M)
    assert _bech32.decode(plain, _bech32.Encoding.BECH32) == ("bc", words)
    assert _bech32.decode(plain, _bech32.Encoding.BECH32M) is None
    assert _bech32.decode(modified, _bech32.Encoding.BECH32M) == ("bc", words)
    assert _bech32.decode(modified, _bech32.Encoding.BECH32) is None


def test_bolt11_amounts_with_too_many_digits_are_refused() -> None:
    assert _bolt11.decode("lnbc" + "1" * 5000 + "1qqqqqq") is None


@pytest.mark.parametrize("case", ADDRESSES["addresses"], ids=lambda c: c["name"])
def test_deposit_address(case: dict[str, Any]) -> None:
    assert is_deposit_address(case["address"]) is case["valid"]


@pytest.mark.parametrize("case", ADDRESSES["registry"]["cases"], ids=lambda c: c["address"])
def test_registry_address_of_a_deposit_address(case: dict[str, str]) -> None:
    registry_key = Secret(bytes.fromhex(ADDRESSES["registry"]["registryKey"]))
    assert onchain_registry_address(registry_key, case["address"]) == case["registryAddress"]


@pytest.mark.parametrize("case", ADDRESSES["uris"], ids=lambda c: c["name"])
def test_payment_uri(case: dict[str, Any]) -> None:
    assert payment_uri(case["address"], int(case["sats"]), case["bolt11"]) == case["uri"]


@pytest.mark.parametrize("case", ADDRESSES["amounts"], ids=lambda c: c["sats"])
def test_btc_amount(case: dict[str, str]) -> None:
    assert btc_amount(int(case["sats"])) == case["btc"]
