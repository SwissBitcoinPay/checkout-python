"""Payment checks of an invoice without an address (SPEC.md §22), through the package's HTTP."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from sattleme_checkout import AsyncClient
from sattleme_checkout._engine import Parts, status_of
from sattleme_checkout._http import Http, HttpResponse
from sattleme_checkout._payment import lightning_part
from sattleme_checkout._stored import InvoiceToCheck, invoice_to_check
from sattleme_checkout.errors import InvalidOrderError, SattlemeError
from spec_vectors import vector

PAYMENTS: Any = vector("payments.json")
INVOICE = PAYMENTS["invoice"]


class Answer:
    def __init__(self, http: dict[str, Any]) -> None:
        self.http = http
        self.asked: list[str] = []

    async def __call__(
        self, url: str, *, headers: Mapping[str, str], socket_timeout: float, max_bytes: int
    ) -> HttpResponse:
        self.asked.append(url)
        assert headers["Accept"] == "application/json"
        body = self.http.get("bodyText")
        data = body.encode() if body is not None else json.dumps(self.http["body"]).encode()
        return HttpResponse(self.http["status"], data)


@pytest.mark.parametrize("case", PAYMENTS["cases"], ids=lambda c: c["name"])
async def test_payment(case: dict[str, Any]) -> None:
    answer = Answer(case["http"])
    http = Http(transport=answer, timeout_ms=1000)
    try:
        result = await lightning_part(
            http,
            bolt11=INVOICE["bolt11"],
            payment_hash=INVOICE["paymentHash"],
            verify_url=INVOICE["verifyUrl"],
            expires_at=INVOICE["expiresAt"],
            now=case["now"],
        )
    except SattlemeError as error:
        found: Any = {"error": error.code}
    else:
        # The status of an invoice without an address: its Lightning part's, as a check gives it.
        stored = InvoiceToCheck(
            ref=None,
            bolt11=INVOICE["bolt11"],
            payment_hash=INVOICE["paymentHash"],
            verify_url=INVOICE["verifyUrl"],
            onchain_address=None,
            sats=21,
            expires_at=INVOICE["expiresAt"],
        )
        parts = Parts(lightning=result, onchain=None, rests=None)
        found = status_of(stored, parts, case["now"]).to_json()
    assert found == case["expected"]
    assert answer.asked == [INVOICE["verifyUrl"]]


# The invoice as another implementation stores it (SPEC.md §3): the vector's, with the fields a
# check needs (§22: `sats`, from its BOLT11 `lnbc210n`; no address), in their own order.
STORED = {
    "expiresAt": INVOICE["expiresAt"],
    "verifyUrl": INVOICE["verifyUrl"],
    "sats": "21",
    "paymentHash": INVOICE["paymentHash"],
    "onchainAddress": None,
    "bolt11": INVOICE["bolt11"],
}


@pytest.mark.parametrize("case", PAYMENTS["cases"], ids=lambda c: c["name"])
async def test_an_invoice_another_implementation_stored(case: dict[str, Any]) -> None:
    """PORTING.md, definition of done, 5: an invoice stored by another implementation is checked
    by `get_status`, the package's own entry, as the vectors say."""
    answer = Answer(case["http"])
    client = AsyncClient(vector("app/app.json")["secretKey"], http=answer, now=lambda: case["now"])
    try:
        found: Any = (await client.get_status(STORED)).to_json()
    except SattlemeError as error:
        found = {"error": error.code}
    assert found == case["expected"]
    assert answer.asked == [INVOICE["verifyUrl"]]


_LEFT_OUT = object()


@pytest.mark.parametrize(
    ("change", "valid"),
    [
        ({}, True),
        ({"onchainAddress": _LEFT_OUT}, True),
        ({"bolt11": _LEFT_OUT}, False),
        ({"paymentHash": _LEFT_OUT}, False),
        ({"verifyUrl": _LEFT_OUT}, False),
        ({"sats": "0" * 14 + "21"}, True),
        ({"sats": "0" * 15 + "21"}, False),
        ({"sats": "0" * 5000 + "21"}, False),
    ],
    ids=[
        "as stored",
        "no onchainAddress member",
        "no bolt11 member",
        "no paymentHash member",
        "no verifyUrl member",
        "sats of 16 digits",
        "sats of 17 digits, their value 21",
        "sats of 5002 digits",
    ],
)
async def test_a_stored_invoice_has_every_member_the_check_reads(
    change: dict[str, object], valid: bool
) -> None:
    """SPEC.md §22: before asking, `get_status` checks the stored invoice. Every member its rules
    read is there, but `onchainAddress` (SPEC.md §2: one left out is not null); `sats` is a
    string of 1 to 16 digits. Else `invalid_order` (`invoice`), and nothing is asked."""
    stored: dict[str, object] = dict(STORED)
    for name, value in change.items():
        if value is _LEFT_OUT:
            del stored[name]
        else:
            stored[name] = value
    case = PAYMENTS["cases"][0]
    answer = Answer(case["http"])
    client = AsyncClient(vector("app/app.json")["secretKey"], http=answer, now=lambda: case["now"])
    if valid:
        assert (await client.get_status(stored)).to_json() == case["expected"]
        assert answer.asked == [INVOICE["verifyUrl"]]
    else:
        with pytest.raises(InvalidOrderError) as raised:
            await client.get_status(stored)
        assert raised.value.param == "invoice"
        assert answer.asked == []


@pytest.mark.parametrize("case", PAYMENTS["stored"], ids=lambda c: c["name"])
def test_stored_invoices(case: dict[str, Any]) -> None:
    """payments.json, `stored`: invoices as a shop stores them, checked as a payment check
    checks them before it asks anything (SPEC.md §22)."""
    try:
        invoice_to_check(case["invoice"])
    except InvalidOrderError as error:
        found: dict[str, Any] = {"error": error.code, "param": error.param}
    else:
        found = {"valid": True}
    assert found == case["expected"]


@pytest.mark.parametrize("case", PAYMENTS["stored"], ids=lambda c: c["name"])
async def test_stored_invoices_through_get_status(case: dict[str, Any]) -> None:
    """The same through `get_status`: an invalid invoice is refused before anything is asked;
    a valid one is checked (here every request fails: `network`, never `invalid_order`)."""
    asked: list[str] = []

    async def unreachable(url: str, **_options: object) -> HttpResponse:
        asked.append(url)
        raise ConnectionError("no network in the tests")

    async def no_relay(url: str) -> Any:
        asked.append(url)
        raise ConnectionError("no relay in the tests")

    client = AsyncClient(
        vector("app/app.json")["secretKey"],
        http=unreachable,
        websocket=no_relay,
        now=lambda: 1_791_378_300,
    )
    with pytest.raises(SattlemeError) as raised:
        await client.get_status(case["invoice"])
    if case["expected"] == {"valid": True}:
        assert raised.value.code == "network"
        assert asked != []
    else:
        assert isinstance(raised.value, InvalidOrderError)
        assert {"error": raised.value.code, "param": raised.value.param} == case["expected"]
        assert asked == []


# An order paid on-chain only, as SPEC.md §3 stores it: `bolt11`, `paymentHash` and `verifyUrl`
# null, its address and its order's reference (the address of onchain-payments.json's invoice).
ONCHAIN_ONLY = {
    **STORED,
    "bolt11": None,
    "paymentHash": None,
    "verifyUrl": None,
    "onchainAddress": vector("onchain-payments.json")["invoice"]["onchainAddress"],
    "ref": "ORD-1",
}


@pytest.mark.parametrize("left_out", [None, "bolt11", "paymentHash", "verifyUrl", "ref"])
def test_a_stored_invoice_without_a_member_that_is_null_is_refused(left_out: str | None) -> None:
    """SPEC.md §22, §2: `bolt11`, `paymentHash` and `verifyUrl` null are an order paid on-chain
    only; left out, they are not null, whatever the others hold: the invoice is refused. With an
    address, its `ref` is there too."""
    stored = {name: value for name, value in ONCHAIN_ONLY.items() if name != left_out}
    if left_out is None:
        assert invoice_to_check(stored).onchain_address == ONCHAIN_ONLY["onchainAddress"]
        return
    with pytest.raises(InvalidOrderError) as raised:
        invoice_to_check(stored)
    assert raised.value.param == "invoice"
