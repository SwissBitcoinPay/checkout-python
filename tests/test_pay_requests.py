"""The pay requests of Lightning addresses (SPEC.md §16) against pay-requests.json: each read from
the answer of its pay URL, through the package's HTTP, as `create_invoice` reads it: its callback,
its sendable sats, its `commentAllowed` and the description hash its invoices are checked
against; or the error."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sattleme_checkout._http import Http, HttpResponse
from sattleme_checkout._lnurl import pay_request
from sattleme_checkout.errors import SattlemeError
from spec_vectors import vector

PAY_REQUESTS: Any = vector("pay-requests.json")


@pytest.mark.parametrize("case", PAY_REQUESTS["cases"], ids=lambda c: c["name"])
async def test_a_pay_request(case: dict[str, Any]) -> None:
    http = case["http"]
    asked: list[str] = []

    async def answer(url: str, **_options: object) -> HttpResponse:
        asked.append(url)
        body = http.get("bodyText")
        data = json.dumps(http["body"]).encode() if body is None else body.encode()
        return HttpResponse(http["status"], data)

    try:
        pay = await pay_request(Http(answer, 1000), "shop@breez.tips")
    except SattlemeError as error:
        found: dict[str, Any] = {"error": error.code}
    else:
        found = {
            "callback": pay.callback,
            # SPEC.md §16, Sendable sats: what the order's amount is checked against.
            "sendable": {"min": str(pay.min_sats), "max": str(pay.max_sats)},
            "commentAllowed": pay.comment_allowed,
            # What the invoice check compares an invoice's description hash with.
            "descriptionHash": pay.description_hash,
        }
    assert found == case["expected"]
    assert asked == ["https://breez.tips/.well-known/lnurlp/shop"]
