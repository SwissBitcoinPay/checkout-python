"""Bech32 (BIP-173) and bech32m (BIP-350), from the references of the two BIPs.

No length limit here: BOLT11 invoices have none (SPEC.md §19), and each caller bounds what it
reads. Bech32 and bech32m are never taken one for the other: each decoding names its constant.
"""

from __future__ import annotations

from enum import Enum

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_CHARSET_INDEX = {char: index for index, char in enumerate(CHARSET)}
_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


class Encoding(Enum):
    BECH32 = 1
    BECH32M = 0x2BC830A3


def _polymod(values: list[int]) -> int:
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = (checksum & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            checksum ^= _GENERATOR[i] if (top >> i) & 1 else 0
    return checksum


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def decode(text: str, encoding: Encoding) -> tuple[str, list[int]] | None:
    """The human-readable part (lowercase) and the data words (checksum removed), or None.

    Either all lowercase or all uppercase, characters 33 to 126 only, nothing around it.
    """
    if any(not 33 <= ord(char) <= 126 for char in text):
        return None
    if text.lower() != text and text.upper() != text:
        return None
    text = text.lower()
    separator = text.rfind("1")
    if separator < 1 or separator + 7 > len(text):
        return None
    hrp, data_part = text[:separator], text[separator + 1 :]
    words: list[int] = []
    for char in data_part:
        index = _CHARSET_INDEX.get(char)
        if index is None:
            return None
        words.append(index)
    if _polymod(_hrp_expand(hrp) + words) != encoding.value:
        return None
    return hrp, words[:-6]


def encode(hrp: str, words: list[int], encoding: Encoding) -> str:
    values = _hrp_expand(hrp) + words
    polymod = _polymod([*values, 0, 0, 0, 0, 0, 0]) ^ encoding.value
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[word] for word in words + checksum)


def convert_bits(data: list[int], from_bits: int, to_bits: int, *, pad: bool) -> list[int] | None:
    """Regroup bits (BIP-173's `convertbits`); None when the padding is not zero bits."""
    accumulator = 0
    bits = 0
    out: list[int] = []
    max_value = (1 << to_bits) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            return None
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            out.append((accumulator >> bits) & max_value)
    if pad:
        if bits:
            out.append((accumulator << (to_bits - bits)) & max_value)
    elif bits >= from_bits or ((accumulator << (to_bits - bits)) & max_value):
        return None
    return out
