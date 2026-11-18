"""NIP-44 v2 against the official vectors of the pinned copy (vectors/nip44/)."""

from __future__ import annotations

from typing import Any

import pytest

from sattleme_checkout import _json, _nip44
from sattleme_checkout._crypto import Secret, xonly_public_key
from sattleme_checkout._nostr import sign_event
from sattleme_checkout._records import Opened, open_record
from spec_vectors import vector

NIP44: Any = vector("nip44/nip44.vectors.json")


@pytest.mark.parametrize(("length", "padded"), NIP44["calc_padded_len"])
def test_padded_length(length: int, padded: int) -> None:
    assert _nip44.padded_length(length) == padded


@pytest.mark.parametrize("case", NIP44["get_conversation_key"])
def test_conversation_key(case: dict[str, str]) -> None:
    key = _nip44.conversation_key(Secret(bytes.fromhex(case["sec1"])), bytes.fromhex(case["pub2"]))
    assert key.reveal().hex() == case["conversation_key"]


@pytest.mark.parametrize("case", NIP44["invalid_get_conversation_key"], ids=lambda c: c["note"])
def test_invalid_conversation_key(case: dict[str, str]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - any refusal
        _nip44.conversation_key(Secret(bytes.fromhex(case["sec1"])), bytes.fromhex(case["pub2"]))


def test_message_keys() -> None:
    key = Secret(bytes.fromhex(NIP44["get_message_keys"]["conversation_key"]))
    for case in NIP44["get_message_keys"]["keys"]:
        chacha_key, chacha_nonce, hmac_key = _nip44._message_keys(key, bytes.fromhex(case["nonce"]))
        assert chacha_key.reveal().hex() == case["chacha_key"]
        assert chacha_nonce.hex() == case["chacha_nonce"]
        assert hmac_key.reveal().hex() == case["hmac_key"]


@pytest.mark.parametrize("case", NIP44["encrypt_decrypt"])
def test_encrypt_decrypt(case: dict[str, str]) -> None:
    key = Secret(bytes.fromhex(case["conversation_key"]))
    payload = _nip44.encrypt(case["plaintext"], key, bytes.fromhex(case["nonce"]))
    assert payload == case["payload"]
    assert _nip44.decrypt(payload, key) == case["plaintext"]


@pytest.mark.parametrize("case", NIP44["invalid_decrypt"], ids=lambda c: c["note"])
def test_invalid_decrypt(case: dict[str, str]) -> None:
    key = Secret(bytes.fromhex(case["conversation_key"]))
    with pytest.raises(_nip44.Nip44Error):
        _nip44.decrypt(case["payload"], key)
    newer = case["note"].startswith("unknown encryption version")
    if newer:
        with pytest.raises(_nip44.Nip44NewerVersionError):
            _nip44.decrypt(case["payload"], key)


@pytest.mark.parametrize("case", NIP44["encrypt_decrypt"])
def test_the_length_of_a_payload(case: dict[str, str]) -> None:
    """What a size check counts (a lease's, SPEC.md §17 step 6): the payload of a plaintext of n
    bytes, whatever its key and nonce, without encrypting it."""
    length = len(case["plaintext"].encode("utf-8"))
    assert _nip44.payload_length(length) == len(case["payload"])


@pytest.mark.parametrize("length", [1, 31, 32, 33, 256, 257, 40_960, 40_961, 65_535])
def test_the_length_of_a_payload_at_each_padding(length: int) -> None:
    key = Secret(bytes.fromhex(NIP44["get_message_keys"]["conversation_key"]))
    payload = _nip44.encrypt("a" * length, key, bytes(32))
    assert _nip44.payload_length(length) == len(payload)


def test_plaintext_bounds() -> None:
    key = Secret(bytes.fromhex(NIP44["get_message_keys"]["conversation_key"]))
    with pytest.raises(ValueError, match="65535"):
        _nip44.encrypt("", key, bytes(32))
    with pytest.raises(ValueError, match="65535"):
        _nip44.encrypt("a" * 65_536, key, bytes(32))
    longest = "a" * 65_535
    assert _nip44.decrypt(_nip44.encrypt(longest, key, bytes(32)), key) == longest


def test_a_plaintext_keeps_its_leading_byte_order_mark() -> None:
    """UTF-8 is decoded strictly (SPEC.md §6): a byte order mark is a character like any other."""
    key = Secret(bytes(range(32)))
    assert _nip44.decrypt(_nip44.encrypt("﻿{}", key, bytes(32)), key) == "﻿{}"


def test_no_record_opens_whose_plaintext_starts_with_a_byte_order_mark() -> None:
    """SPEC.md §12: such a plaintext is not JSON, even signed by the record's author."""
    signer = Secret(bytes([3]) * 32)
    key = Secret(bytes([4]) * 32)
    address = "ab" * 32
    author = xonly_public_key(signer).hex()

    def event(prefix: str) -> dict[str, object]:
        plaintext = (prefix + _json.dumps({"t": "t", "v": 1})).ljust(256)
        return sign_event(
            signer,
            created_at=1_791_378_000,
            kind=30078,
            tags=[["d", address]],
            content=_nip44.encrypt(plaintext, key, bytes(32)),
            aux=bytes(32),
        ).to_json()

    def opened(value: dict[str, object]) -> object:
        return open_record(
            value, record_type="t", key=key, address=address, author=author, parse=lambda v, _: v
        )

    record = opened(event(""))
    assert isinstance(record, Opened)
    assert record.value == 1
    assert opened(event("﻿")) is None
