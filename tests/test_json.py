"""JSON as every implementation reads it (SPEC.md §2, §7): the cases where Python's `json` and
JavaScript's `JSON.parse` would differ, and the bound of 64 arrays and objects nested, through
every reader of the package (an HTTP answer, a relay's message, a record, the explorer's
WebSocket)."""

from __future__ import annotations

import asyncio
import json
import math
import random
import secrets
import time
from typing import Any

import pytest

from fakes.http import FakeHttp
from sattleme_checkout import _json
from sattleme_checkout._channel import tolerance_of
from sattleme_checkout._constants import JSON_DEPTH_MAX
from sattleme_checkout._crypto import Secret
from sattleme_checkout._http import Http
from sattleme_checkout._lnurl import pay_request
from sattleme_checkout._records import Opened, open_record, seal
from sattleme_checkout._relay import RelayPool, fresh_key
from sattleme_checkout._waiting import _is_change
from sattleme_checkout.errors import InvalidInvoiceError, NetworkError


def test_the_last_of_duplicate_names_wins() -> None:
    assert _json.parse_text('{"a": 1, "a": 2}') == {"a": 2}
    assert _json.parse_text('{"a": {"b": 1}, "a": {"c": 2}}') == {"a": {"c": 2}}


def test_names_in_another_case_are_other_names() -> None:
    value = _json.parse_text('{"version": 1, "Version": 2, "VERSION": 3}')
    assert value == {"version": 1, "Version": 2, "VERSION": 3}


async def _pay_request(body: bytes) -> Any:
    http = FakeHttp()
    http.route("https://breez.tips/", lambda _url: (200, body))
    return await pay_request(Http(http, 1000), "shop@breez.tips")


async def test_a_duplicate_name_in_an_answer() -> None:
    body = (
        b'{"tag": "withdrawRequest", "tag": "payRequest", "callback": "https://breez.tips/cb",'
        b' "minSendable": 1000, "maxSendable": 0, "maxSendable": 2000, "metadata": "[]"}'
    )
    pay = await _pay_request(body)
    assert (pay.max_sendable, pay.callback) == (2000, "https://breez.tips/cb")
    # The first of two wins nowhere: a later invalid one makes the answer invalid.
    with pytest.raises(InvalidInvoiceError):
        await _pay_request(body.replace(b'"maxSendable": 2000', b'"maxSendable": "2000"'))


async def test_a_name_in_another_case_is_ignored() -> None:
    body = (
        b'{"tag": "payRequest", "Callback": "https://elsewhere.example/cb",'
        b' "callback": "https://breez.tips/cb", "minSendable": 1000, "maxSendable": 2000,'
        b' "metadata": "[]", "CommentAllowed": 255}'
    )
    pay = await _pay_request(body)
    assert pay.callback == "https://breez.tips/cb"
    assert pay.comment_allowed == 0
    with pytest.raises(InvalidInvoiceError):
        await _pay_request(body.replace(b'"callback"', b'"CALLBACK"'))


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", '{"a": NaN}', "[Infinity]"])
def test_not_json(text: str) -> None:
    assert _json.parse_text(text) is None


@pytest.mark.parametrize(
    ("text", "integer"),
    [
        ("500", 500),
        ("500.0", 500),
        ("5e2", 500),
        ("5.00E+2", 500),
        ("-0", 0),
        ("-0.0", 0),
        ("500.5", None),
        ("1e400", None),
        ("true", None),
        ("false", None),
        ('"500"', None),
        ("null", None),
    ],
)
def test_an_integer_is_a_value_whatever_its_spelling(text: str, integer: int | None) -> None:
    assert _json.as_integer(_json.parse_text(text)) == integer


def test_safe_integers() -> None:
    assert _json.as_safe_integer(_json.parse_text("9007199254740991")) == 9_007_199_254_740_991
    assert _json.as_safe_integer(_json.parse_text("9007199254740992")) is None
    assert _json.as_safe_integer(_json.parse_text("-9007199254740992")) is None
    assert _json.as_safe_integer(_json.parse_text("1.7976931348623157e308")) is None


def test_a_tolerance_spelled_as_a_float() -> None:
    assert tolerance_of(_json.parse_text("500.0")) == 500
    assert tolerance_of(_json.parse_text("500.5")) == 0
    assert tolerance_of(_json.parse_text("true")) == 0


@pytest.mark.parametrize(
    "data",
    [b"\xff", b'"\xc3"', b'"\xed\xa0\x80"', b"\xef\xbb\xbf{}"],
    ids=["invalid byte", "truncated", "a surrogate in UTF-8", "a byte order mark"],
)
def test_bytes_that_are_not_json_in_utf_8(data: bytes) -> None:
    assert _json.parse_bytes(data) is None


def test_writing_is_compact_and_keeps_the_characters() -> None:
    separator = chr(0x2028)  # as JSON.stringify: written as it is, never escaped
    text = _json.dumps({"a": [1, "é\n" + separator], "b": None})
    assert text == '{"a":[1,"é\\n' + separator + '"],"b":null}'
    with pytest.raises(ValueError, match="JSON"):
        _json.dumps(float("nan"))


def test_a_lone_surrogate_is_read_but_never_written_as_utf_8() -> None:
    value = _json.parse_text('"\\ud800"')
    assert value == "\ud800"


def test_a_name_may_hold_u0000() -> None:
    assert _json.parse_text('{"\\u0000a": 1, "a\\u0000": 2}') == {"\x00a": 1, "a\x00": 2}


def test_a_number_is_its_double_whatever_its_length() -> None:
    """SPEC.md §2: beyond the largest double, infinite (`1e400`, an integer of 400 digits); an
    integer of 5 000 digits is a number too, which Python's `int` refuses (4 300 at most); one
    beyond 2^53 is the double nearest to it, as `JSON.parse` reads it."""
    assert _json.parse_text("[" + "9" * 5000 + "]") == [math.inf]
    assert _json.parse_text("[-" + "1" + "0" * 5000 + "]") == [-math.inf]
    assert _json.parse_text("[" + "1" + "0" * 399 + ", 1e400, -1e400]") == [
        math.inf,
        math.inf,
        -math.inf,
    ]
    assert _json.parse_text("[" + "1" + "0" * 308 + "]") == [1e308]
    # 2^53 + 1 is no double: its value is 2^53, beyond the safe integers either way.
    assert _json.as_integer(_json.parse_text("9007199254740993")) == 2**53
    assert _json.as_integer(_json.parse_text("12345678901234567890")) == 12345678901234567168
    assert _json.as_safe_integer(_json.parse_text("9007199254740993")) is None
    # Exact up to 2^53: an int, as it always was.
    assert _json.parse_text("[9007199254740992, -9007199254740991, 0, -0]") == [
        2**53,
        -(2**53) + 1,
        0,
        0,
    ]
    exact = _json.parse_text("[9007199254740992, 1, -0]")
    assert isinstance(exact, list)
    assert [type(value) for value in exact] == [int, int, int]
    # An infinite number is no integer (SPEC.md §2: "an infinite number is none").
    assert _json.as_integer(_json.parse_text("9" * 5000)) is None
    assert _json.is_number(_json.parse_text("9" * 5000))


def _nested(depth: int, inner: str = "1") -> str:
    return "[" * depth + inner + "]" * depth


def _objects(depth: int) -> str:
    """`depth` arrays and objects nested in all, half of each."""
    return '{"a":' * (depth // 2) + _nested(depth - depth // 2) + "}" * (depth // 2)


def test_at_most_64_arrays_and_objects_nested() -> None:
    assert JSON_DEPTH_MAX == 64
    assert _json.parse_text(_nested(64)) == json.loads(_nested(64))
    assert _json.parse_text(_nested(65)) is None
    assert _json.parse_text(_objects(64)) == json.loads(_objects(64))
    assert _json.parse_text(_objects(65)) is None
    assert _json.parse_text(_nested(100_000)) is None
    # Side by side, never one in another: any number of them.
    assert _json.parse_text("[" + ",".join([_nested(64)] * 50) + "]") is None
    assert _json.parse_text("[" + ",".join([_nested(63)] * 50) + "]") is not None


def _depth(text: str) -> int:
    """The most arrays and objects nested in a JSON text, outside its strings: read character
    by character, as SPEC.md §2 counts them."""
    level = deepest = 0
    in_string = escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            level += 1
            deepest = max(deepest, level)
        elif character in "]}":
            level -= 1
    return deepest


def test_the_depth_counted_without_reading_each_character() -> None:
    """`nests_within` takes escapes out instead of reading the text character by character: the
    same verdict for any JSON, its strings full of brackets, quotes and backslashes."""
    draw = random.Random(2)
    pieces = ["[", "]", "{", "}", '"', "\\", "\\\\", '\\"', "a", "\N{NO-BREAK SPACE}"]

    def text() -> str:
        return "".join(draw.choice(pieces) for _ in range(draw.randrange(6)))

    def value(depth: int) -> object:
        """Arrays and objects nested `depth` deep, strings beside each, and more of them."""
        if depth == 0:
            return text()
        inner = [value(depth - 1)] + [
            text() if draw.random() < 0.7 else [text()] for _ in range(draw.randrange(3))
        ]
        draw.shuffle(inner)
        if draw.random() < 0.5:
            return inner
        return {text() + str(index): item for index, item in enumerate(inner)}

    for depth in [*range(JSON_DEPTH_MAX - 3, JSON_DEPTH_MAX + 4), *range(0, 90, 7)] * 20:
        written = json.dumps(value(depth), ensure_ascii=draw.random() < 0.5)
        assert _depth(written) in (depth, depth + 1)
        assert _json.nests_within(written) is (_depth(written) <= JSON_DEPTH_MAX), written


def test_no_bracket_inside_a_string_counts() -> None:
    inside = "[" * 100 + "{" * 100 + '\\"' + "]" * 3 + "\\\\"
    text = f'[{json.dumps(inside)}, "{inside}", {_nested(62)}]'
    assert _json.parse_text(text) == json.loads(text)
    assert _json.parse_text('{"a\\"[[[": ' + _nested(63) + "}") == {
        'a"[[[': json.loads(_nested(63))
    }
    assert _json.parse_text('{"a\\"[[[": ' + _nested(64) + "}") is None


@pytest.mark.parametrize(
    "text",
    [
        "[01]",
        "[1,]",
        '{"a":1,}',
        "[1]\N{NO-BREAK SPACE}",
        "\N{NO-BREAK SPACE}[1]",
        "\N{ZERO WIDTH NO-BREAK SPACE}[1]",
        "",
        "[",
        "]",
        "[1]]",
    ],
)
def test_nothing_else_is_json(text: str) -> None:
    assert _json.parse_text(text) is None


def _pay_request_body(extra: str) -> bytes:
    return (
        '{"tag": "payRequest", "callback": "https://breez.tips/cb", "minSendable": 1000,'
        f' "maxSendable": 2000, "metadata": "[]", "extra": {extra}}}'
    ).encode()


@pytest.mark.parametrize(
    ("extra", "read"),
    [
        ("1" + "0" * 5000, True),
        ("1e400", True),
        (_nested(JSON_DEPTH_MAX - 1), True),
        (_nested(JSON_DEPTH_MAX), False),
        (_nested(20_000), False),
    ],
    ids=["5001 digits", "1e400", "64 nested", "65 nested", "20001 nested"],
)
async def test_an_http_answer(extra: str, read: bool) -> None:
    """A pay request (SPEC.md §16) whose other member is a long number, or arrays nested: read,
    or no JSON (network)."""
    if read:
        assert (await _pay_request(_pay_request_body(extra))).max_sendable == 2000
    else:
        with pytest.raises(NetworkError):
            await _pay_request(_pay_request_body(extra))


class _Messages:
    """A relay's socket that answers a query with the messages given, as they are written."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = messages
        self._inbox: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, message: str) -> None:
        value = json.loads(message)
        if value[0] == "REQ":
            for text in self._messages:
                self._inbox.put_nowait(text.replace("SUB", value[1]))
            self._inbox.put_nowait(json.dumps(["EOSE", value[1]]))

    async def recv(self) -> str:
        return await self._inbox.get()

    async def close(self) -> None:
        return None


async def test_a_relays_message() -> None:
    """An event in a message whose `extra` member nests 62 arrays (64 in all, with the message
    and the event) is read; one nesting 63 is no message; one holding a number of 5 000 digits
    is read."""
    messages = [
        '["EVENT","SUB",{"id":"a","created_at":5,"extra":' + _nested(62) + "}]",
        '["EVENT","SUB",{"id":"b","created_at":5,"extra":' + _nested(63) + "}]",
        '["EVENT","SUB",{"id":"c","created_at":5,"extra":' + "7" * 5000 + "}]",
    ]

    async def connect(_url: str) -> _Messages:
        return _Messages(messages)

    async with RelayPool(
        connect=connect, timeout_ms=2000, random=secrets.token_bytes, now=lambda: 0
    ) as pool:
        answers = await pool.read(["wss://a.example"], {"kinds": [1]}, lambda value: value)
    kept = answers["wss://a.example"]
    assert [event["id"] for event in kept] == ["a", "c"]  # type: ignore[index]
    assert kept[1]["extra"] == math.inf  # type: ignore[index]


@pytest.mark.parametrize(
    ("depth", "opens"),
    [(JSON_DEPTH_MAX - 2, True), (JSON_DEPTH_MAX - 1, False)],
    ids=["64 nested", "65 nested"],
)
def test_a_record(depth: int, opens: bool) -> None:
    """A record's plaintext, `{"t": …, "v": {"x": …}}`: two objects around what `x` nests."""
    signer, key, address = (
        fresh_key(secrets.token_bytes),
        Secret(secrets.token_bytes(32)),
        "ab" * 32,
    )
    event = seal(
        record_type="test",
        value={"x": json.loads(_nested(depth))},
        signer=signer,
        key=key,
        address=address,
        created_at=1_791_378_000,
        random=secrets.token_bytes,
    )
    opened = open_record(
        event.to_json(),
        record_type="test",
        key=key,
        address=address,
        author=event.pubkey,
        parse=lambda value, _event: value,
    )
    assert isinstance(opened, Opened) is opens


@pytest.mark.parametrize(
    ("message", "change"),
    [
        ('{"address-transactions": [], "extra": ' + _nested(JSON_DEPTH_MAX - 1) + "}", True),
        ('{"address-transactions": [], "extra": ' + _nested(JSON_DEPTH_MAX) + "}", False),
        ('{"address-transactions": [], "extra": ' + "9" * 5000 + "}", True),
    ],
    ids=["64 nested", "65 nested", "5000 digits"],
)
def test_the_explorers_websocket(message: str, change: bool) -> None:
    assert _is_change(message) is change
    assert _is_change(message.encode()) is change


_MIB = 1 << 20
# A string of escapes, and of escapes each followed by a non-ASCII character: a reader indexing
# characters from the string's start at each escape is quadratic (9 s and 130 s, in another port).
_ESCAPES = {"escapes": "\\u0000", "escapes and é": "\\u0000é"}


def _escaped(piece: str, room: int) -> str:
    """A JSON string of `piece` repeated, of 1 MiB less `room` bytes of UTF-8."""
    return '"' + piece * ((_MIB - room) // len(piece.encode())) + '"'


@pytest.mark.parametrize("piece", _ESCAPES.values(), ids=_ESCAPES.keys())
async def test_reading_an_answer_costs_time_linear_in_its_size(piece: str) -> None:
    """SPEC.md §13, Limits: reading what a server sends costs time linear in its size. A pay
    request whose metadata is 1 MiB of escapes is read within 2 s (Python's parser: a few ms)."""
    metadata = _escaped(piece, 256)
    body = _pay_request_body("0").replace(b'"metadata": "[]"', b'"metadata": ' + metadata.encode())
    assert len(body) <= _MIB
    started = time.monotonic()
    pay = await _pay_request(body)
    assert time.monotonic() - started < 2
    assert pay.metadata == json.loads(metadata)


@pytest.mark.parametrize("piece", _ESCAPES.values(), ids=_ESCAPES.keys())
async def test_reading_a_relays_message_costs_time_linear_in_its_size(piece: str) -> None:
    """SPEC.md §13, Limits: an event whose content is 1 MiB of escapes, in a relay's message, is
    read within 2 s."""
    content = _escaped(piece, 256)
    message = '["EVENT","SUB",{"id":"a","created_at":5,"content":' + content + "}]"
    assert len(message.encode()) <= _MIB

    async def connect(_url: str) -> _Messages:
        return _Messages([message])

    async with RelayPool(
        connect=connect, timeout_ms=10_000, random=secrets.token_bytes, now=lambda: 0
    ) as pool:
        started = time.monotonic()
        answers = await pool.read(["wss://a.example"], {"kinds": [1]}, lambda value: value)
        assert time.monotonic() - started < 2
    [event] = answers["wss://a.example"]
    assert event["content"] == json.loads(content)  # type: ignore[index]


@pytest.mark.parametrize(
    "inside",
    ['\\"', "\\\\", "[{", '\\"[', "\\u0000é"],
    ids=["escaped quotes", "escaped backslashes", "brackets", "quotes and brackets", "escapes"],
)
def test_the_depth_of_a_text_costs_time_linear_in_its_size(inside: str) -> None:
    """The depth counted before parsing (`nests_within`) is linear too, whatever a string of
    1 MiB holds."""
    text = "[" + _escaped(inside, 64) + "]"
    started = time.monotonic()
    assert _json.nests_within(text)
    assert _json.parse_text(text) is not None
    assert time.monotonic() - started < 2
