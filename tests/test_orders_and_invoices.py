"""An order's text (SPEC.md §17), the invoice checks (SPEC.md §18)."""

from __future__ import annotations

import random
import time
import urllib.parse
from collections.abc import Mapping
from typing import Any

import pytest

from sattleme_checkout._invoice_checks import check_invoice
from sattleme_checkout._lnurl import (
    InvoiceAnswer,
    PayRequest,
    callback_url,
    comment_fits,
    description_hash,
)
from sattleme_checkout._order import is_ref, label_of
from sattleme_checkout.errors import InvalidInvoiceError, InvalidOrderError
from spec_vectors import vector

TEXT: Any = vector("order-text.json")
CHECKS: Any = vector("invoice-checks.json")


@pytest.mark.parametrize("case", TEXT["refs"], ids=lambda c: repr(c["ref"]))
def test_ref(case: dict[str, Any]) -> None:
    assert is_ref(case["ref"]) is case["valid"]


@pytest.mark.parametrize("case", TEXT["notes"], ids=lambda c: repr(c["note"])[:40])
def test_note(case: dict[str, Any]) -> None:
    expected = case["expected"]
    if "error" in expected:
        with pytest.raises(InvalidOrderError) as raised:
            label_of(case["note"], case["ref"])
        assert raised.value.param == expected["param"]
    else:
        assert label_of(case["note"], case["ref"]) == expected["label"]


@pytest.mark.parametrize(
    "case", TEXT["comments"], ids=lambda c: f"{c['label']}/{c['commentAllowed']}"
)
def test_comment(case: dict[str, Any]) -> None:
    assert comment_fits(case["label"], case["commentAllowed"]) is case["sent"]


def _pay() -> PayRequest:
    pay = CHECKS["payRequest"]
    return PayRequest(
        callback=pay["callback"],
        min_sendable=1000,
        max_sendable=10**11,
        metadata=pay["metadata"],
        comment_allowed=0,
        # As the pay request's read makes it (SPEC.md §16).
        description_hash=description_hash(pay["metadata"]) or "",
    )


@pytest.mark.parametrize("case", CHECKS["cases"], ids=lambda c: c["name"])
def test_invoice_check(case: dict[str, Any]) -> None:
    answer = InvoiceAnswer(pr=case["answer"]["pr"], verify=case["answer"]["verify"])
    expected = case["expected"]

    def run() -> None:
        check_invoice(
            answer,
            _pay(),
            sats=int(case["sats"]),
            duration=case["durationSeconds"],
            now=case["now"],
        )

    if expected.get("valid"):
        run()
    else:
        with pytest.raises(InvalidInvoiceError) as raised:
            run()
        assert raised.value.code == expected["error"]
        assert raised.value.check == expected["check"]


PARAMS = {"amount": "49000000", "expiry": "600", "comment": "Order 1"}


@pytest.mark.parametrize(
    ("callback", "expected"),
    [
        (
            "https://ln.example/cb",
            "https://ln.example/cb?amount=49000000&expiry=600&comment=Order+1",
        ),
        (
            "https://ln.example/cb?id=7&amount=1",
            "https://ln.example/cb?id=7&amount=49000000&expiry=600&comment=Order+1",
        ),
        (
            "https://ln.example/cb?expiry=1&id=7&expiry=2&amount=1&amount=3",
            "https://ln.example/cb?expiry=600&id=7&amount=49000000&comment=Order+1",
        ),
        (
            "https://ln.example/cb?a=&b",
            "https://ln.example/cb?a=&b=&amount=49000000&expiry=600&comment=Order+1",
        ),
    ],
)
def test_callback_url(callback: str, expected: str) -> None:
    """SPEC.md §17 step 5: the callback's own query parameters are kept; each one set as
    `URLSearchParams.set` does."""
    assert callback_url(callback, PARAMS) == expected


def _set_one_by_one(callback: str, params: Mapping[str, str]) -> str:
    """`URLSearchParams.set` of each parameter in turn, as the WHATWG URL standard says it."""
    parts = urllib.parse.urlsplit(callback)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    for name, value in params.items():
        positions = [i for i, (key, _) in enumerate(query) if key == name]
        if positions:
            query[positions[0]] = (name, value)
            query = [pair for i, pair in enumerate(query) if i not in positions[1:]]
        else:
            query.append((name, value))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def test_callback_url_is_set_one_by_one() -> None:
    draw = random.Random(17)
    names = ["amount", "expiry", "comment", "id", "k"]
    for _ in range(2000):
        pairs = [
            (draw.choice(names), draw.choice(["", "1", "x y"])) for _ in range(draw.randrange(8))
        ]
        callback = "https://ln.example/cb?" + urllib.parse.urlencode(pairs)
        params = dict(draw.sample(list(PARAMS.items()), draw.randrange(1, 4)))
        assert callback_url(callback, params) == _set_one_by_one(callback, params)


@pytest.mark.parametrize(
    "callback",
    ["https://ln.example/cb#x", "https://ln.example/cb#", "https://ln.example/cb?k=1#x?amount=1"],
)
def test_a_callbacks_fragment_is_dropped(callback: str) -> None:
    """SPEC.md §17, step 5: from its first `#`, which the parameters added after it would never
    pass to reach the server."""
    kept = "k=1&" if "?k=1" in callback else ""
    assert callback_url(callback, PARAMS) == (
        f"https://ln.example/cb?{kept}amount=49000000&expiry=600&comment=Order+1"
    )


def test_a_callback_repeating_a_parameter_costs_one_pass() -> None:
    """A Lightning address may answer 1 MiB: a callback repeating `amount` some 100 000 times is
    read in one pass (setting the parameters one by one took minutes)."""
    callback = "https://ln.example/cb?" + "amount=1&" * 116_000
    started = time.monotonic()
    assert callback_url(callback, PARAMS) == (
        "https://ln.example/cb?amount=49000000&expiry=600&comment=Order+1"
    )
    assert time.monotonic() - started < 5
