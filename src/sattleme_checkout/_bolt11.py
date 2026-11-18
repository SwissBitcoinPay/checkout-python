"""BOLT11 invoices (SPEC.md §19): what the checks need. The signature is not verified."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import _bech32
from ._constants import BOLT11_AMOUNT_MAX_MSAT, BOLT11_EXPIRY_DEFAULT, BOLT11_EXPIRY_WORDS_MAX

_HRP = re.compile("ln(bcrt|tbs|bc|tb)(?:([0-9]+)([munp]?))?")
# More digits than any amount up to 21 million bitcoins has, whatever the multiplier.
_DIGITS_MAX = 22
_MULTIPLIER = {"": 10**11, "m": 10**8, "u": 10**5, "n": 10**2}
_TIMESTAMP_WORDS = 7
_SIGNATURE_WORDS = 104
_TAG_PAYMENT_HASH = 1
_TAG_DESCRIPTION_HASH = 23
_TAG_EXPIRY = 6
_HASH_WORDS = 52


@dataclass(frozen=True, slots=True)
class Bolt11:
    network: str
    amount_msat: int | None
    timestamp: int
    expiry: int
    payment_hash: str
    description_hash: str | None

    @property
    def expires_at(self) -> int:
        return self.timestamp + self.expiry


def _number(words: list[int]) -> int:
    value = 0
    for word in words:
        value = value << 5 | word
    return value


def _hash(words: list[int]) -> str:
    # 52 words are 260 bits: 256 bits of hash, then 4 bits of padding, dropped (as the app does).
    return (_number(words) >> 4).to_bytes(32, "big").hex()


def decode(invoice: object) -> Bolt11 | None:
    """The fields of a BOLT11 invoice; None when it is not one (never repaired)."""
    if not isinstance(invoice, str):
        return None
    decoded = _bech32.decode(invoice, _bech32.Encoding.BECH32)
    if decoded is None:
        return None
    hrp, words = decoded
    match = _HRP.fullmatch(hrp)
    if match is None:
        return None
    network, digits, multiplier = match.group(1), match.group(2), match.group(3)
    amount_msat: int | None = None
    if digits is not None:
        if len(digits) > _DIGITS_MAX or (len(digits) > 1 and digits[0] == "0"):
            return None
        if multiplier == "p":
            if int(digits) % 10:
                return None
            amount_msat = int(digits) // 10
        else:
            amount_msat = int(digits) * _MULTIPLIER[multiplier]
        if amount_msat > BOLT11_AMOUNT_MAX_MSAT:
            return None
    if len(words) < _TIMESTAMP_WORDS + _SIGNATURE_WORDS:
        return None
    timestamp = _number(words[:_TIMESTAMP_WORDS])
    fields = words[_TIMESTAMP_WORDS : len(words) - _SIGNATURE_WORDS]
    payment_hash: str | None = None
    description_hash: str | None = None
    expiry: int | None = None
    position = 0
    while position < len(fields):
        if position + 3 > len(fields):
            return None
        tag = fields[position]
        length = 32 * fields[position + 1] + fields[position + 2]
        data = fields[position + 3 : position + 3 + length]
        if len(data) != length:
            return None
        position += 3 + length
        if tag == _TAG_PAYMENT_HASH and length == _HASH_WORDS and payment_hash is None:
            payment_hash = _hash(data)
        elif tag == _TAG_DESCRIPTION_HASH and length == _HASH_WORDS and description_hash is None:
            description_hash = _hash(data)
        elif tag == _TAG_EXPIRY:
            if length > BOLT11_EXPIRY_WORDS_MAX:
                return None
            expiry = _number(data)
    if payment_hash is None:
        return None
    return Bolt11(
        network=network,
        amount_msat=amount_msat,
        timestamp=timestamp,
        expiry=BOLT11_EXPIRY_DEFAULT if expiry is None else expiry,
        payment_hash=payment_hash,
        description_hash=description_hash,
    )
