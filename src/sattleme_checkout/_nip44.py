"""NIP-44 v2 encryption (https://github.com/nostr-protocol/nips/blob/master/44.md, SPEC.md §12)."""

from __future__ import annotations

from ._crypto import (
    Secret,
    chacha20,
    ecdh_x,
    hkdf_expand,
    hkdf_extract,
    hmac_sha256,
    macs_equal,
)
from ._text import base64_decode, base64_encode, decode_utf8, encode_utf8

PAYLOAD_MIN = 132
PAYLOAD_MAX = 87_472
DATA_MIN = 99
DATA_MAX = 65_603
PLAINTEXT_MAX = 65_535
VERSION = 2


class Nip44Error(ValueError):
    """A payload that does not decrypt (never says why to a relay's sender)."""


class Nip44NewerVersionError(Nip44Error):
    """A payload of another version than 2: a newer format (SPEC.md §12)."""


def conversation_key(secret: Secret, public_x: bytes) -> Secret:
    """The conversation key of a secret key and an x-only public key (ECDH, then HKDF-extract)."""
    return hkdf_extract(b"nip44-v2", ecdh_x(secret, public_x))


def padded_length(length: int) -> int:
    if length <= 32:
        return 32
    next_power = 1 << (length - 1).bit_length()
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * ((length - 1) // chunk + 1)


def _message_keys(key: Secret, nonce: bytes) -> tuple[Secret, bytes, Secret]:
    keys = hkdf_expand(key, nonce, 76)
    return Secret(keys[:32]), keys[32:44], Secret(keys[44:])


def fits(plaintext: str) -> bool:
    """Whether NIP-44 encrypts `plaintext`: 1 to 65 535 bytes of UTF-8."""
    data = encode_utf8(plaintext)
    return data is not None and 1 <= len(data) <= PLAINTEXT_MAX


def payload_length(length: int) -> int:
    """The length of the payload `encrypt` makes of a plaintext of `length` bytes (1 to 65 535):
    base64 of its version, nonce, padded plaintext and MAC, whatever the key and the nonce."""
    return 4 * -(-(1 + 32 + 2 + padded_length(length) + 32) // 3)


def encrypt(plaintext: str, key: Secret, nonce: bytes) -> str:
    """Encrypt `plaintext` (1 to 65 535 bytes of UTF-8) under a conversation key and a nonce."""
    data = encode_utf8(plaintext)
    if data is None or not 1 <= len(data) <= PLAINTEXT_MAX:
        raise ValueError("NIP-44 encrypts 1 to 65535 bytes of UTF-8")
    if len(nonce) != 32:
        raise ValueError("NIP-44 takes a 32-byte nonce")
    padded = len(data).to_bytes(2, "big") + data + bytes(padded_length(len(data)) - len(data))
    chacha_key, chacha_nonce, hmac_key = _message_keys(key, nonce)
    ciphertext = chacha20(chacha_key.reveal(), chacha_nonce, padded)
    mac = hmac_sha256(hmac_key, nonce + ciphertext)
    return base64_encode(bytes([VERSION]) + nonce + ciphertext + mac)


def decrypt(payload: str, key: Secret) -> str:
    """Decrypt a payload; Nip44NewerVersionError for another version, Nip44Error otherwise."""
    if not payload or payload[0] == "#":
        raise Nip44NewerVersionError("unknown version")
    if not PAYLOAD_MIN <= len(payload) <= PAYLOAD_MAX:
        raise Nip44Error("invalid payload length")
    data = base64_decode(payload)
    if data is None:
        raise Nip44Error("invalid base64")
    if not DATA_MIN <= len(data) <= DATA_MAX:
        raise Nip44Error("invalid data length")
    if data[0] != VERSION:
        raise Nip44NewerVersionError("unknown version")
    nonce, ciphertext, mac = data[1:33], data[33:-32], data[-32:]
    chacha_key, chacha_nonce, hmac_key = _message_keys(key, nonce)
    if not macs_equal(hmac_sha256(hmac_key, nonce + ciphertext), mac):
        raise Nip44Error("invalid MAC")
    padded = chacha20(chacha_key.reveal(), chacha_nonce, ciphertext)
    length = int.from_bytes(padded[:2], "big")
    # As NIP-44's reference implementations: the lengths are checked, not the padding's bytes.
    if length < 1 or len(padded) != 2 + padded_length(length):
        raise Nip44Error("invalid padding")
    text = decode_utf8(padded[2 : 2 + length])
    if text is None:
        raise Nip44Error("invalid UTF-8")
    return text
