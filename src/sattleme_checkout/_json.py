"""JSON as every implementation reads and writes it (SPEC.md §2).

Reading, as JavaScript's `JSON.parse` reads it, of every JSON the package reads (an HTTP answer,
a relay's message, a record, the explorer's WebSocket): RFC 8259 only (Python's `json` also takes
`NaN` and `Infinity`, refused here), the last of duplicate names wins, a name may hold any
character (U+0000 included), and a string a lone surrogate. A number is its value as an IEEE 754
double, rounded to the nearest, whatever its spelling and its length: `500`, `500.0` and `5e2` are
the same number, an integer of 400 digits is infinite as `1e400` is, and one of 5 000 digits is a
number too (Python's `int` refuses more than 4 300). `true` and `false` are never numbers,
although Python's `bool` is an `int`. One bound that `JSON.parse` does not have: a text in which
more than 64 arrays and objects nest one in another is not JSON, counted before it is parsed.

Writing: compact, UTF-8, names in the order given; as `JSON.stringify` writes it.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from itertools import accumulate
from typing import Any, NoReturn

from ._constants import JSON_DEPTH_MAX, SAFE_INTEGER_MAX
from ._text import decode_utf8

# A string once its escaped quotes are out; what is not a bracket; what a bracket does to depth.
_STRING = re.compile(r'"[^"]*"')
_NOT_BRACKET = re.compile(r"[^\[\]{}]+")
_DEPTH_CHANGE = {"[": 1, "{": 1, "]": -1, "}": -1}


def _refuse_constant(name: str) -> NoReturn:
    raise ValueError(f"not JSON: {name}")


def _integer(literal: str) -> int | float:
    """The value of an integer literal: its double (SPEC.md §2). An `int` while it is exact (from
    −2^53 to 2^53, where every integer is a double), else the double itself, infinite beyond the
    largest one; `float` reads a literal of any length, rounded to the nearest."""
    value = float(literal)
    return int(value) if abs(value) <= SAFE_INTEGER_MAX + 1 else value


_DECODER = json.JSONDecoder(parse_int=_integer, parse_constant=_refuse_constant)


def nests_within(text: str, depth: int = JSON_DEPTH_MAX) -> bool:
    """Whether no more than `depth` arrays and objects nest one in another in the JSON `text`, its
    strings left out (what it says of a text that is not JSON does not matter: the parser refuses
    it). A text holding no more `[` and `{` than that cannot (most do: a relay's event holds a few),
    and is not scanned. Else every step runs in C, at about the parser's own cost whatever the
    text: escaped backslashes, then escaped quotes, taken out (each run of backslashes pairs from
    its start, as a string's escapes do), every quote left opens or closes a string."""
    if text.count("[") + text.count("{") <= depth:
        return True
    plain = text.replace("\\\\", "").replace('\\"', "")
    brackets = _NOT_BRACKET.sub("", _STRING.sub("", plain))
    return max(accumulate(map(_DEPTH_CHANGE.__getitem__, brackets)), default=0) <= depth


def loads(text: str) -> object:
    """Parse JSON text (SPEC.md §2); raises ValueError when it is not JSON."""
    if not nests_within(text):
        raise ValueError(f"not JSON: more than {JSON_DEPTH_MAX} arrays and objects nest")
    return _DECODER.decode(text)


def parse_text(text: str) -> object | None:
    """The JSON value of `text`; None when it is not JSON (never repaired)."""
    try:
        return loads(text)
    except (ValueError, RecursionError):
        return None


def parse_bytes(data: bytes) -> object | None:
    """The JSON value of UTF-8 `data`; None when it is not UTF-8 or not JSON."""
    text = decode_utf8(data)
    return None if text is None else parse_text(text)


def dumps(value: object) -> str:
    """Compact JSON, non-ASCII characters as they are, as `JSON.stringify` writes it."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def is_object(value: object) -> bool:
    """A JSON object: not null, not an array."""
    return isinstance(value, dict)


def as_object(value: object) -> Mapping[str, Any] | None:
    """`value` when it is a JSON object, else None."""
    return value if isinstance(value, dict) else None


def as_integer(value: object) -> int | None:
    """The integer a JSON number holds (any spelling); None for anything else, booleans included,
    and an infinite number (SPEC.md §2: none)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def as_safe_integer(value: object) -> int | None:
    """The integer a JSON number holds when it is within ±(2^53 − 1); else None."""
    number = as_integer(value)
    if number is None or abs(number) > SAFE_INTEGER_MAX:
        return None
    return number


def is_number(value: object) -> bool:
    """Whether `value` is a JSON number (a boolean is not)."""
    return isinstance(value, int | float) and not isinstance(value, bool)
