"""Deposit addresses and payment URIs (SPEC.md §31, Deposit addresses, Payment URIs)."""

from __future__ import annotations

from typing import TypeGuard

from . import _bech32

_LENGTH = 62
_PROGRAM_BYTES = 32
_SATS_PER_BTC = 100_000_000


def is_deposit_address(value: object) -> TypeGuard[str]:
    """A taproot address of Bitcoin's main network, in lowercase: `bc1p` and 58 more characters.

    The bech32m encoding (BIP-350) of `bc`, witness version 1 and a 32-byte program; another
    network, version or length, bech32 instead of bech32m, upper case, or a padding with bits that
    are not zero is not one.
    """
    if not isinstance(value, str) or len(value) != _LENGTH or value.lower() != value:
        return False
    decoded = _bech32.decode(value, _bech32.Encoding.BECH32M)
    if decoded is None:
        return False
    hrp, words = decoded
    if hrp != "bc" or not words or words[0] != 1:
        return False
    program = _bech32.convert_bits(words[1:], 5, 8, pad=False)
    return program is not None and len(program) == _PROGRAM_BYTES


def btc_amount(sats: int) -> str:
    """`sats` in bitcoins, exact: no trailing zero in the fraction, no fraction when whole."""
    whole, fraction = divmod(sats, _SATS_PER_BTC)
    digits = f"{fraction:08d}".rstrip("0")
    return f"{whole}.{digits}" if digits else str(whole)


def payment_uri(address: str | None, sats: int, bolt11: str | None) -> str:
    """What the shop's page shows as one QR code (BIP-21 with `lightning=`, or `lightning:`)."""
    if address is None:
        if bolt11 is None:
            raise ValueError("a payment URI needs an address or an invoice")
        return f"lightning:{bolt11}"
    uri = f"bitcoin:{address}?amount={btc_amount(sats)}"
    return uri if bolt11 is None else f"{uri}&lightning={bolt11}"
