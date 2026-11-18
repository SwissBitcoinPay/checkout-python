"""An order's earlier invoices (SPEC.md §21) against the vectors: the DeclaredInvoices its records
give, those that may still be paid, and which one step 1 hands out.

The vector's records stand for records that opened (their BOLT11 strings are stand-ins): each is
built as opening leaves it, and run through the functions `read_sale` and `createInvoice` use.
"""

from __future__ import annotations

from typing import Any

import pytest

from sattleme_checkout._bolt11 import Bolt11
from sattleme_checkout._declaration import Declaration, DeclaredPrice, RestOf
from sattleme_checkout._lease import LeaseTerms, SaleRecord
from sattleme_checkout._nostr import Event
from sattleme_checkout._records import Newer, Opened
from sattleme_checkout._sales import (
    SaleRecords,
    earlier_invoices,
    held_on_majority,
    may_still_be_paid,
    pending_outcome,
)
from sattleme_checkout.errors import InvalidInvoiceError, NetworkError, SattlemeError
from sattleme_checkout.models import PaymentStatus
from spec_vectors import vector

EARLIER: Any = vector("earlier-invoices.json")
SATS = 49_000
PRICE = DeclaredPrice(minor=SATS, digits=0, currency="SAT")
SALE = "43984d278c37779e0dd825d0f34582fdbd4c2e141bbe3a3f5d1a21736c21fc7d"
REST_ADDRESS = "bc1p4gmh64rd4m5y5nn8hwfnvtz2mm6lngpmwys5a257klyg03ju0pyqu65xe8"


def _event(event_id: str, created_at: int, d: str) -> Event:
    return Event(event_id, SALE, created_at, 30078, (("d", d),), "", "")


def _declaration(index: int, value: dict[str, Any]) -> Declaration:
    decoded = Bolt11(
        network="bc",
        amount_msat=SATS * 1000,
        timestamp=value["createdAt"],
        expiry=value["expiresAt"] - value["createdAt"],
        payment_hash=value["paymentHash"],
        description_hash=None,
    )
    return Declaration(
        event=_event(f"{index + 1:02x}" * 32, value["declaredAt"], f"hash/{value['paymentHash']}"),
        invoice=value["invoice"],
        decoded=decoded,
        verify_url=f"https://example.com/verify/{value['paymentHash']}",
        price=PRICE,
        note="ORD-1",
        rest=RestOf(REST_ADDRESS, value["expiresAt"]) if value.get("rest") else None,
    )


def _record(value: dict[str, Any]) -> Opened[SaleRecord] | Newer:
    event = _event(value["event"]["id"], value["event"]["createdAt"], f"lease/{value['address']}")
    if value.get("newer"):
        return Newer(event)
    if value.get("withdrawn"):
        return Opened(event, SaleRecord(event, None))
    invoice = value["invoice"]
    terms = LeaseTerms(
        address=value["address"],
        sats=SATS,
        invoice=invoice,
        verify_url=None if invoice is None else "https://example.com/verify",
        created_at=value["createdAt"],
        expires_at=value["expiresAt"],
        price=PRICE,
        note="ORD-1",
        tolerance=0,
        # Absent: not confirmed (SPEC.md §31, Opening it as the sale).
        confirmed=value.get("confirmed") is True,
    )
    return Opened(event, SaleRecord(event, terms))


ERRORS: dict[str, type[SattlemeError]] = {
    "network": NetworkError,
    "invalid_invoice": InvalidInvoiceError,
}


def _status(value: dict[str, Any]) -> PaymentStatus | SattlemeError:
    if "error" in value:
        return ERRORS[value["error"]](f"{value['error']} (vector)")
    received = int(value.get("received", "0"))
    if value["state"] == "open":
        return PaymentStatus.open(0, received, None)
    if value["state"] == "unconfirmed":
        return PaymentStatus.unconfirmed(received, 1, ("a" * 64,))
    assert value["state"] == "paid"
    return PaymentStatus.paid_lightning("00" * 32)


@pytest.mark.parametrize("case", EARLIER["cases"], ids=lambda c: c["name"])
def test_earlier_invoices(case: dict[str, Any]) -> None:
    declarations = [_declaration(i, value) for i, value in enumerate(case.get("declarations", []))]
    records = [_record(value) for value in case.get("records", [])]
    invoices, _leases = earlier_invoices("ORD-1", declarations, records)
    expected = case["expected"]
    assert [
        {
            "paymentHash": e.invoice.payment_hash,
            "onchainAddress": e.invoice.onchain_address,
            "createdAt": e.invoice.created_at,
            "declaredAt": e.invoice.declared_at,
        }
        for e in invoices
    ] == expected["invoices"]

    pending = may_still_be_paid(invoices, case["now"])
    assert [invoices.index(e) for e in pending] == expected["mayBePaid"]
    # Every one that may still be paid is checked, even alone: those `create_invoice` checks.
    assert [invoices.index(e) for e in pending] == expected["checked"]
    relays = case.get("relays", EARLIER["relays"])
    # How many relays of the list sent each record (absent: all of them), by event id.
    sent = {
        **{
            d.event.id: v.get("sentBy", relays)
            for d, v in zip(declarations, case.get("declarations", []), strict=True)
        },
        **{v["event"]["id"]: v.get("sentBy", relays) for v in case.get("records", [])},
    }
    if not pending:
        assert expected["outcome"] is None
        return
    statuses = [_status(case["statuses"][invoices.index(e)]) for e in pending]
    held: list[bool] | SattlemeError = []
    if len(pending) > 1:
        # Read again on every relay: what each relay of the list sent (the first `sentBy` of
        # them sent a record), and whether a majority sent each last record, as the check
        # decides it.
        if case.get("readFails"):
            held = NetworkError("fewer than a majority answered (vector)")
        else:
            again = SaleRecords(
                copies={
                    f"wss://r{index}.example": frozenset(
                        event_id for event_id, count in sent.items() if index < count
                    )
                    for index in range(relays)
                }
            )
            held = held_on_majority(again, pending, relays)
    outcome = pending_outcome(statuses, held)
    if isinstance(outcome, SattlemeError):
        assert expected["outcome"] == {"error": outcome.code}
        return
    kind, index = outcome
    status = statuses[index]
    if kind == "paid":
        assert expected["outcome"] == {"orderPaid": invoices.index(pending[index])}
    elif kind == "failed":
        assert isinstance(status, SattlemeError)
        assert expected["outcome"] == {"error": status.code}
    else:
        assert expected["outcome"] == {"handOut": invoices.index(pending[index])}
