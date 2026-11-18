"""The rest of an on-chain underpayment (SPEC.md §31): keys, rest records, acceptance records,
the declarations of rest invoices, and when a check asks for a rest invoice."""

from __future__ import annotations

from typing import Any

import pytest

from sattleme_checkout._crypto import Secret
from sattleme_checkout._declaration import DeclaredPrice, Registry, open_declaration
from sattleme_checkout._derive import rest_keys, rest_reader, sale_keys
from sattleme_checkout._engine import Held
from sattleme_checkout._lease import LeaseTerms
from sattleme_checkout._nostr import Event
from sattleme_checkout._onchain import RestInvoice, status_rest
from sattleme_checkout._records import Newer, Opened
from sattleme_checkout._rest import LinkTerms, open_acceptance, open_rest_record, should_ask
from sattleme_checkout._sales import Lease
from sattleme_checkout._stored import InvoiceToCheck
from spec_vectors import vector

RESTS: Any = vector("onchain-rests.json")
KEY = Secret(bytes.fromhex(RESTS["integrationKey"]))


@pytest.mark.parametrize(
    "case", RESTS["keys"], ids=lambda c: f"{c['ref']} {c['address'][:10]} {c['expiresAt']}"
)
def test_rest_keys(case: dict[str, Any]) -> None:
    keys = rest_keys(KEY, case["ref"], case["address"], case["expiresAt"])
    assert keys.key.reveal().hex() == case["restKey"]
    assert keys.secret.reveal().hex() == case["restSecret"]
    assert keys.author == case["restAuthor"]
    assert keys.encryption_key.reveal().hex() == case["restEncryptionKey"]
    assert keys.acceptance_address == case["acceptanceAddress"]
    for record in case["records"]:
        assert keys.record_address(record["paymentHash"]) == record["address"]
    reader = rest_reader(keys.key, keys.author)
    assert reader.encryption_key == keys.encryption_key
    assert reader.acceptance_address == keys.acceptance_address


def _reader(section: dict[str, Any]) -> Any:
    return rest_reader(Secret(bytes.fromhex(section["restKey"])), section["restAuthor"])


@pytest.mark.parametrize("case", RESTS["records"]["cases"], ids=lambda c: c["name"])
def test_rest_records(case: dict[str, Any]) -> None:
    section = RESTS["records"]
    link = section["link"]
    terms = LinkTerms(int(link["sats"]), link["createdAt"], link["expiresAt"])
    opened = open_rest_record(case["event"], _reader(section), terms)
    expected = case["expected"]
    if expected is None:
        assert opened is None
    elif expected == "newer":
        assert isinstance(opened, Newer)
    else:
        assert isinstance(opened, Opened)
        shown = opened.value
        assert {
            "invoice": shown.invoice,
            "paymentHash": shown.payment_hash,
            "verifyUrl": shown.verify_url,
            "sats": str(shown.sats),
        } == expected


@pytest.mark.parametrize("case", RESTS["acceptances"]["cases"], ids=lambda c: c["name"])
def test_acceptance_records(case: dict[str, Any]) -> None:
    section = RESTS["acceptances"]
    opened = open_acceptance(case["event"], _reader(section), int(section["link"]["sats"]))
    expected = case["expected"]
    if expected is None:
        assert opened is None
    elif expected == "newer":
        assert isinstance(opened, Newer)
    else:
        assert isinstance(opened, Opened)
        assert str(opened.value) == expected["received"]


@pytest.mark.parametrize("case", RESTS["declarations"]["cases"], ids=lambda c: c["name"])
def test_rest_declarations(case: dict[str, Any]) -> None:
    section = RESTS["declarations"]
    registry = Registry(
        key=Secret(bytes.fromhex(section["registryKey"])), reader=section["registryReader"]
    )
    opened = open_declaration(case["event"], sale=sale_keys(KEY, section["ref"]), registry=registry)
    expected = case["expected"]
    if expected is None:
        assert opened is None
        return
    assert isinstance(opened, Opened)
    value = opened.value
    rest = None if value.rest is None else value.rest.to_json()
    assert {"invoice": value.invoice, "note": value.note, "rest": rest} == expected


ADDRESS = "bc1px22v5j99gdwhxqyelg2p6tdcfajjd3njhzn22c2gura2dvxg2xlqw2wte4"


def _lease(lease: dict[str, Any]) -> Lease:
    """The sale's lease of the address, of the vector's `sats` and `expiresAt`."""
    terms = LeaseTerms(
        address=ADDRESS,
        sats=int(lease["sats"]),
        invoice=None,
        verify_url=None,
        created_at=lease["expiresAt"] - 900,
        expires_at=lease["expiresAt"],
        price=DeclaredPrice(minor=int(lease["sats"]), digits=0, currency="SAT"),
        note="ORD-1",
        tolerance=0,
        confirmed=True,
    )
    return Lease(Event("a" * 64, "b" * 64, terms.created_at, 30078, (), "", ""), terms)


@pytest.mark.parametrize("case", RESTS["asking"], ids=lambda c: c["name"])
def test_asking(case: dict[str, Any]) -> None:
    sats, expires_at, now = int(case["sats"]), case["expiresAt"], case["now"]
    received = int(case["received"])
    rests = [
        RestInvoice(
            bolt11=r["id"],
            payment_hash=r["paymentHash"],
            verify_url="https://breez.tips/v",
            sats=int(r["sats"]),
            created_at=r["createdAt"],
            expires_at=r["expiresAt"],
        )
        for r in case["rests"]
    ]
    paid = frozenset(r["paymentHash"] for r in case["rests"] if r["paid"])
    # `received` counts what came on-chain and what rest invoices paid.
    rest = status_rest(
        address=ADDRESS,
        sats=sats,
        expires_at=expires_at,
        received=received,
        now=now,
        rests=rests,
        paid=paid,
    )
    expected_rest = case["expected"]["rest"]
    if expected_rest is None:
        assert rest is None
    else:
        assert rest is not None
        assert {"sats": str(rest.sats), "invoice": rest.bolt11} == expected_rest
    # Whether the sale's lease of the address is this invoice's, as a check tells it.
    held = Held(
        stored=InvoiceToCheck(
            ref="ORD-1",
            bolt11=None,
            payment_hash=None,
            verify_url=None,
            onchain_address=ADDRESS,
            sats=sats,
            expires_at=expires_at,
        ),
        ref="ORD-1",
        address=ADDRESS,
    )
    lease = case["lease"]
    ask = should_ask(
        rest_sats=None if rest is None else rest.sats,
        rest_bolt11=None if rest is None else rest.bolt11,
        expires_at=expires_at,
        now=now,
        lease_matches=lease is not None and held.leased_by(_lease(lease)),
        sendable=(int(case["sendable"]["min"]), int(case["sendable"]["max"])),
    )
    expected_ask = case["expected"]["ask"]
    if expected_ask is None:
        assert ask is None
    else:
        assert ask is not None
        assert {"sats": str(ask.sats), "expirySeconds": ask.expiry} == expected_ask
