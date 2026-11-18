"""Text rules every implementation applies alike (SPEC.md §2, §6).

Python's own `str.strip()`, `str.lower()`, `\\s` and lenient base64 decoders follow other rules
than the specification's: these functions are the only ones the package uses.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import TypeGuard

_UPPER_TO_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_LIGHTNING_ADDRESS_SPACE = " \t\n\r"
_CONTROL = re.compile("[\u0000-\u001f\u007f]")
_HEX32 = re.compile("[0-9a-f]{64}")
_HEX64 = re.compile("[0-9a-f]{128}")
_BASE64URL = re.compile("[A-Za-z0-9_-]*")
_BASE64 = re.compile("(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")

# ECMAScript's whitespace (`\s`), which the merchant's app checks relay addresses with (SPEC.md §9).
ECMASCRIPT_SPACE = "".join(
    chr(code_point)
    for code_point in (
        *range(0x09, 0x0E),
        0x20,
        0xA0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        0xFEFF,
    )
)


def ascii_lower(text: str) -> str:
    """`text` with A-Z lowered to a-z and nothing else changed (never Unicode case mapping)."""
    return text.translate(_UPPER_TO_LOWER)


def trim_spaces(text: str) -> str:
    """`text` without its leading and trailing spaces (U+0020): a note's trimming."""
    return text.strip(" ")


def trim_ascii_whitespace(text: str) -> str:
    """`text` without leading and trailing space, tab, line feed and carriage return."""
    return text.strip(_LIGHTNING_ADDRESS_SPACE)


def has_control(text: str) -> bool:
    """Whether `text` holds a control character: U+0000 to U+001F, or U+007F."""
    return _CONTROL.search(text) is not None


def decode_utf8(data: bytes) -> str | None:
    """The text of UTF-8 `data`, decoded strictly; None when it is not valid UTF-8."""
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def encode_utf8(text: str) -> bytes | None:
    """The UTF-8 of `text`; None when it holds a lone surrogate, which UTF-8 cannot carry."""
    try:
        return text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None


def is_well_formed(text: str) -> bool:
    """Whether `text` is well-formed Unicode: no lone surrogate (a JSON escape `\\ud800` makes
    one), which UTF-8 cannot carry (SPEC.md §6)."""
    return encode_utf8(text) is not None


def utf8_length(text: str) -> int | None:
    """The length of `text` in UTF-8 bytes; None when it is not well-formed."""
    data = encode_utf8(text)
    return None if data is None else len(data)


def is_hex32(value: object) -> TypeGuard[str]:
    """Whether `value` is 64 lowercase hex characters (32 bytes)."""
    return isinstance(value, str) and _HEX32.fullmatch(value) is not None


def is_hex64(value: object) -> TypeGuard[str]:
    """Whether `value` is 128 lowercase hex characters (64 bytes)."""
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def base64url_encode(data: bytes) -> str:
    """Base64url without padding (RFC 4648 §5)."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def base64url_decode(text: str) -> bytes | None:
    """Strict base64url without padding: only the canonical encoding of some bytes; else None."""
    if _BASE64URL.fullmatch(text) is None or len(text) % 4 == 1:
        return None
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    # Re-encoding refuses non-zero unused trailing bits: only one spelling of the bytes is valid.
    return data if base64url_encode(data) == text else None


def base64_encode(data: bytes) -> str:
    """Base64 with padding (RFC 4648 §4)."""
    return base64.b64encode(data).decode("ascii")


def base64_decode(text: str) -> bytes | None:
    """Strict padded base64: only the canonical encoding of some bytes; else None."""
    if _BASE64.fullmatch(text) is None:
        return None
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if base64_encode(data) == text else None
