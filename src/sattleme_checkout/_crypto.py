"""The cryptographic primitives, and the one type that holds a secret.

- SHA-256 and HMAC-SHA256: the standard library (`hashlib`, `hmac`).
- HKDF-SHA256 and ChaCha20 (IETF, 12-byte nonce): `cryptography` (OpenSSL).
- secp256k1 (BIP-340 signatures, x-only keys, `lift_x`, ECDH): `btclib_secp256k1`, bindings to
  Bitcoin Core's libsecp256k1, constant time for every operation on a secret.

Never the curve written here: every operation on a point or a scalar is libsecp256k1's.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import NoReturn

from btclib_secp256k1 import ecdh as _ecdh
from btclib_secp256k1 import keys as _keys
from btclib_secp256k1 import ssa as _ssa
from btclib_secp256k1 import xonly as _xonly
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand

_ZERO_COUNTER = bytes(4)


class Secret:
    """Bytes that must never be shown: a key, or a key derived from one.

    Its `repr` and `str` say nothing of it, and it refuses to be pickled or copied into a
    message, so that a log, an exception, an error tracker's capture of a frame's local
    variables or a debugger never shows it. Python cannot wipe memory: what this protects
    against is showing it, not reading the process's memory.
    """

    __slots__ = ("__value",)

    def __init__(self, value: bytes) -> None:
        self.__value = bytes(value)

    def reveal(self) -> bytes:
        """The bytes, for a cryptographic operation or a record that carries them by design."""
        return self.__value

    def __repr__(self) -> str:
        return "<secret>"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        return "<secret>"

    def __reduce__(self) -> NoReturn:
        raise TypeError("a secret is never serialized")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return hmac.compare_digest(self.__value, other.__value)

    def __hash__(self) -> int:
        raise TypeError("a secret is not hashable")


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hmac_sha256(key: Secret, data: bytes) -> bytes:
    return hmac.new(key.reveal(), data, hashlib.sha256).digest()


def macs_equal(a: bytes, b: bytes) -> bool:
    """Whether two MACs are equal, compared in constant time."""
    return hmac.compare_digest(a, b)


def hkdf(key: Secret, salt: str, info: str) -> Secret:
    """HKDF-SHA256 (RFC 5869), 32 bytes, salt and info as UTF-8 (SPEC.md §2)."""
    return Secret(
        HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt.encode("utf-8"),
            info=info.encode("utf-8"),
        ).derive(key.reveal())
    )


def hkdf_extract(salt: bytes, key: Secret) -> Secret:
    """HKDF-Extract with SHA-256: the NIP-44 conversation key of a shared x."""
    return Secret(HKDF.extract(hashes.SHA256(), salt, key.reveal()))


def hkdf_expand(key: Secret, info: bytes, length: int) -> bytes:
    """HKDF-Expand with SHA-256: the NIP-44 message keys."""
    return HKDFExpand(algorithm=hashes.SHA256(), length=length, info=info).derive(key.reveal())


def chacha20(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """ChaCha20 of RFC 8439 (32-byte key, 12-byte nonce), from block counter 0.

    `cryptography`'s ChaCha20 takes 16 bytes: the 32-bit block counter in little-endian, then
    the 12-byte nonce. NIP-44 starts at counter 0.
    """
    if len(key) != 32 or len(nonce) != 12:
        raise ValueError("ChaCha20 takes a 32-byte key and a 12-byte nonce")
    encryptor = Cipher(algorithms.ChaCha20(key, _ZERO_COUNTER + nonce), mode=None).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def is_secret_key(value: bytes) -> bool:
    """Whether 32 bytes are a secp256k1 secret key: from 1 to the curve's order minus 1."""
    return len(value) == 32 and bool(_keys.prvkey_verify(value))


def is_xonly_point(x: bytes) -> bool:
    """Whether 32 bytes are the x coordinate of a point of secp256k1 (BIP-340 `lift_x`)."""
    return len(x) == 32 and bool(_xonly.pubkey_verify(x))


def xonly_public_key(secret: Secret) -> bytes:
    """The BIP-340 x-only public key of a secret key."""
    public, _parity = _xonly.from_prvkey(secret.reveal())
    return bytes(public)


def schnorr_sign(message: bytes, secret: Secret, aux: bytes) -> bytes:
    """A BIP-340 signature of a 32-byte message, with 32 bytes of auxiliary randomness."""
    if len(message) != 32 or len(aux) != 32:
        raise ValueError("BIP-340 signs 32 bytes with 32 bytes of auxiliary randomness")
    return bytes(_ssa.sign(message, secret.reveal(), aux))


def schnorr_verify(message: bytes, public_key: bytes, signature: bytes) -> bool:
    """Whether a BIP-340 signature of a 32-byte message verifies under an x-only key."""
    if len(message) != 32 or len(public_key) != 32 or len(signature) != 64:
        return False
    if not is_xonly_point(public_key):
        return False
    try:
        return bool(_ssa.verify(message, public_key, signature))
    except ValueError:
        return False


def ecdh_x(secret: Secret, public_x: bytes) -> Secret:
    """The x coordinate of secret × lift_x(public_x), in constant time (NIP-44: not hashed).

    libsecp256k1's default ECDH hashes the point; NIP-44 takes x as it is. `shared_point` runs
    `secp256k1_ecdh` with a hash function that copies the coordinates out.
    """
    if not is_xonly_point(public_x):
        raise ValueError("not a point of secp256k1")
    if not is_secret_key(secret.reveal()):
        raise ValueError("not a secret key")
    return Secret(bytes(_ecdh.shared_point(b"\x02" + public_x, secret.reveal()))[1:])
